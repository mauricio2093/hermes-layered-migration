"""Canalizacion local de pruebas del candidato (sin GitHub Actions).

Suites (todas OBLIGATORIAS salvo que se diga):
  upstream-layer2     tests de la Capa 2 y los que tocan los commits propios
  upstream-core       estado SQLite, Telegram, comandos, plugins, migraciones de config
  hermes-control      la suite completa del plugin contra el codigo candidato
  smoke-*             import del gateway, `config check`, `plugins list` y
                      migracion de una COPIA restaurada de state.db (Capa 3)

Un fallo solo se perdona si es PREEXISTENTE y se demuestra: el mismo test se
re-ejecuta sobre el tag upstream puro (suites upstream) o sobre el codigo de
produccion (hermes-control) y tambien falla alli. Lo demas es regresion.

Nada se reduce en silencio: lo no ejecutado aparece como SKIPPED con motivo.
Todo corre con memoria acotada (systemd-run --scope) y nice: el gateway
productivo comparte las 6 GB del equipo.
"""

from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .config import Config
from .proc import Runner
from .status import Verdict, worst

UPSTREAM_CORE = ("tests/hermes_state", "tests/gateway/test_telegram*.py", "tests/hermes_cli/test_commands.py",
                 "tests/hermes_cli/test_config_migration*.py", "tests/hermes_cli/test_*plugin*.py")
LAYER2 = ("tests/layer2",)
EATMYDATA = shutil.which("eatmydata")
#: tope de tests "inestables" aceptables por suite: el mayor de los dos
FLAKY_MAX = 5
FLAKY_MAX_RATIO = 0.01


@dataclass
class Suite:
    name: str
    required: bool = True
    verdict: Verdict = Verdict.IN_PROGRESS
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_ids: list = field(default_factory=list)
    preexisting: list = field(default_factory=list)
    regressions: list = field(default_factory=list)
    #: fallaron y pasaron al repetirlos en el candidato: se informan, no se ocultan
    flaky: list = field(default_factory=list)
    internal_error: bool = False
    duration_s: float = 0.0
    detail: str = ""
    tail: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["verdict"] = self.verdict.value
        d["failed_ids"] = self.failed_ids[:100]
        return d


def wrapper(cfg: Config) -> list[str]:
    if shutil.which("systemd-run") and os.environ.get("HERMES_UPDATER_NO_SCOPE") != "1":
        return ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={cfg.policy.test_memory_max}",
                "nice", "-n", "10"]
    return ["nice", "-n", "10"]


def parse_junit(path: Path) -> dict:
    out = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "failed_ids": [], "passed_ids": [],
           "internal_error": False}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return out
    for tc in root.iter("testcase"):
        kinds = {c.tag for c in tc}
        if tc.get("classname") == "pytest" and tc.get("name") == "internal":
            out["internal_error"] = True      # pytest murio: la suite quedo a medias
            continue
        if "failure" in kinds:
            out["failed"] += 1
            out["failed_ids"].append(_nodeid(tc))
        elif "error" in kinds:
            out["errors"] += 1
            out["failed_ids"].append(_nodeid(tc))
        elif "skipped" in kinds:
            out["skipped"] += 1
        else:
            out["passed"] += 1
            out["passed_ids"].append(_nodeid(tc))
    # un test que falla en la llamada y otra vez al desmontarse sale dos veces
    out["failed_ids"] = list(dict.fromkeys(out["failed_ids"]))
    return out


def _nodeid(tc) -> str:
    """classname 'tests.gateway.test_x.TestY' + name -> 'tests/gateway/test_x.py::TestY::name'."""
    cls = tc.get("classname") or ""
    if not cls:
        # error de COLECCION: pytest deja classname vacio y pone el modulo en name
        # ('tests.hermes_cli.test_x'); el objetivo es el modulo entero.
        return (tc.get("name") or "").replace(".", "/") + ".py"
    parts = cls.split(".")
    # el modulo es el ultimo trozo que empieza por test_
    idx = max((i for i, p in enumerate(parts) if p.startswith("test")), default=len(parts) - 1)
    mod = "/".join(parts[: idx + 1]) + ".py"
    rest = parts[idx + 1:]
    return "::".join([mod, *rest, tc.get("name") or ""])


def expand(repo: Path, patterns: Sequence[str]) -> list[str]:
    out = []
    for pat in patterns:
        hits = sorted(glob.glob(str(repo / pat)))
        out.extend(str(Path(h).relative_to(repo)) for h in hits)
    return list(dict.fromkeys(out))


def run_pytest(cfg: Config, runner: Runner, *, python: str, cwd: Path, targets: Sequence[str],
               junit: Path, env: Optional[dict] = None, timeout: Optional[int] = None) -> tuple[dict, object]:
    # basetemp en disco, junto al junit: /tmp es tmpfs (RAM) en este equipo
    # eatmydata: las bases de los tests son desechables. En el disco mecanico de
    # produccion cada sentencia DDL de SQLite espera a un fsync (~37 ms) y la
    # suite upstream-core de v0.22.2 no terminaba en 60 min; con eatmydata el
    # mismo fichero pasa de 73 s a 3 s. Solo afecta a este proceso de pytest.
    nofsync = [EATMYDATA] if EATMYDATA and Path(EATMYDATA).is_file() else []
    argv = [*wrapper(cfg), *nofsync, python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-o", "addopts=-m 'not integration and not live'", "--continue-on-collection-errors",
            f"--junitxml={junit}",
            f"--basetemp={junit.with_suffix('.tmp')}", *targets]
    base_env = {"PYTHONDONTWRITEBYTECODE": "1", "HERMES_UPDATER_UNDER_TEST": "1"}
    if env:
        base_env.update(env)
    r = runner.run(argv, cwd=cwd, env=base_env, timeout=timeout or cfg.policy.test_timeout_s)
    res = parse_junit(junit)
    return res, r


def _fill(s: Suite, res: dict, r) -> None:
    s.passed, s.failed, s.errors, s.skipped = res["passed"], res["failed"], res["errors"], res["skipped"]
    s.failed_ids = res["failed_ids"]
    s.internal_error = res["internal_error"]
    s.duration_s = r.duration_s
    s.tail = r.tail(3000)


def recheck(cfg: Config, runner: Runner, s: Suite, *, python: str, cwd: Path, junit: Path,
            env: Optional[dict] = None) -> None:
    """Repite UNA vez los fallidos en el propio candidato. Los que pasan son
    inestables (carga del equipo, tiempos): quedan en ``s.flaky`` y en el
    detalle, no como regresion. Si la repeticion no ejecuta nada, no se
    descarta ninguno."""
    if not s.failed_ids:
        return
    res, _ = run_pytest(cfg, runner, python=python, cwd=cwd, targets=s.failed_ids, junit=junit, env=env,
                        timeout=cfg.policy.test_timeout_s)
    if res["passed"] + res["failed"] + res["errors"] == 0:
        return
    still = set(res["failed_ids"])
    passed = set(res["passed_ids"])
    # solo es inestable lo que la repeticion vio PASAR con ese mismo id; un id que
    # no aparece (no se pudo seleccionar) sigue contando como fallo
    flaky = [t for t in s.failed_ids if t in passed and t not in still]
    ran = s.passed + s.failed + s.errors
    if len(flaky) > max(FLAKY_MAX, int(ran * FLAKY_MAX_RATIO)):
        # Demasiados para ser inestabilidad: algo del ENTORNO de la suite (p. ej.
        # el limite de descriptores del servicio, 2026-10-19: 940 "Too many open
        # files") los tumbo en cascada. No se descarta ninguno.
        s.detail += (f" | {len(flaky)} fallaron y pasaron al repetirlos: demasiados para ser inestabilidad"
                     f" (maximo {max(FLAKY_MAX, int(ran * FLAKY_MAX_RATIO))}); se cuentan como fallos")
        return
    s.flaky = flaky
    s.failed_ids = [t for t in s.failed_ids if t not in s.flaky]
    if s.flaky:
        s.detail += f" | {len(s.flaky)} inestables (fallaron y pasaron al repetirlos): " + ", ".join(s.flaky[:10])


def classify(cfg: Config, runner: Runner, s: Suite, *, python: str, baseline_cwd: Path,
             junit: Path, env: Optional[dict] = None) -> None:
    """Re-ejecuta los fallidos en la linea base. Fallan alli tambien ->
    preexistentes. Pasan alli -> regresion del candidato."""
    if not s.failed_ids:
        return
    res, r = run_pytest(cfg, runner, python=python, cwd=baseline_cwd, targets=s.failed_ids,
                        junit=junit, env=env, timeout=cfg.policy.test_timeout_s)
    base_failed = set(res["failed_ids"])
    ran = res["passed"] + res["failed"] + res["errors"]
    for t in s.failed_ids:
        if t in base_failed:
            s.preexisting.append(t)
        else:
            s.regressions.append(t)
    if ran == 0:
        # la linea base no pudo ejecutarlos: no hay prueba de que sean preexistentes
        s.regressions, s.preexisting = list(s.failed_ids), []
        s.detail += " | la linea base no ejecuto los fallidos: se cuentan como regresion"


def _finish(s: Suite, r) -> None:
    if r.timed_out:
        s.verdict, s.detail = Verdict.FAIL, f"timeout tras {r.duration_s:.0f}s"
    elif s.internal_error:
        s.verdict = Verdict.FAIL
        s.detail = (f"pytest INTERNALERROR: la suite no termino ({s.passed} ok antes de morir)" + s.detail)
    elif s.passed + s.failed + s.errors == 0:
        s.verdict, s.detail = Verdict.FAIL, "no se ejecuto ningun test (" + (r.tail(300).strip() or "sin salida") + ")"
    elif s.regressions:
        s.verdict = Verdict.FAIL
        s.detail = f"{len(s.regressions)} regresiones" + s.detail
    else:
        s.verdict = Verdict.PASS
        s.detail = (f"{s.passed} ok, {s.skipped} skip"
                    + (f", {len(s.preexisting)} fallos preexistentes (tambien fallan en la linea base)"
                       if s.preexisting else "") + s.detail)


def upstream_suite(cfg: Config, runner: Runner, name: str, patterns: Sequence[str], *, python: str,
                   cand_repo: Path, pure_target: Optional[Path], out: Path) -> Suite:
    s = Suite(name)
    targets = expand(cand_repo, patterns)
    if not targets:
        s.verdict, s.detail = Verdict.FAIL, f"ningun test encontrado para {patterns}"
        return s
    res, r = run_pytest(cfg, runner, python=python, cwd=cand_repo, targets=targets,
                        junit=out / f"{name}.xml", env={"PYTHONPATH": str(cand_repo)})
    _fill(s, res, r)
    recheck(cfg, runner, s, python=python, cwd=cand_repo, junit=out / f"{name}-recheck.xml",
            env={"PYTHONPATH": str(cand_repo)})
    if pure_target is not None:
        classify(cfg, runner, s, python=python, baseline_cwd=pure_target, junit=out / f"{name}-baseline.xml",
                 env={"PYTHONPATH": str(pure_target)})
    else:
        s.regressions = list(s.failed_ids)
    _finish(s, r)
    return s


def hermes_control_suite(cfg: Config, runner: Runner, *, python: str, cand_repo: Path, out: Path) -> Suite:
    s = Suite("hermes-control")
    repo = cfg.hermes_control_repo
    if not (repo / "tests").is_dir():
        s.verdict, s.detail = Verdict.BLOCKED, f"no existe {repo}/tests"
        return s
    pp = lambda hermes: f"{repo / 'hermes_plugins'}:{hermes}"
    res, r = run_pytest(cfg, runner, python=python, cwd=repo, targets=["tests"], junit=out / "hermes-control.xml",
                        env={"PYTHONPATH": pp(cand_repo)})
    _fill(s, res, r)
    recheck(cfg, runner, s, python=python, cwd=repo, junit=out / "hermes-control-recheck.xml",
            env={"PYTHONPATH": pp(cand_repo)})
    classify(cfg, runner, s, python=python, baseline_cwd=repo, junit=out / "hermes-control-baseline.xml",
             env={"PYTHONPATH": pp(cfg.prod_repo)})
    _finish(s, r)
    return s


def smoke(cfg: Config, runner: Runner, *, python: str, cand_repo: Path, restored_home: Optional[Path],
          out: Path) -> list[Suite]:
    """Smoke no destructivo. Con la Capa 3 RESTAURADA del backup, nunca con la viva:
    asi tambien prueba que el backup sirve para arrancar el candidato."""
    suites = []
    env = {"PYTHONPATH": str(cand_repo)}

    s = Suite("smoke-imports")
    r = runner.run([python, "-c", "import gateway.run, hermes_cli.main, hermes_state, "
                    "plugins.platforms.telegram.adapter; print('ok')"], cwd=cand_repo, env=env, timeout=300)
    s.verdict = Verdict.PASS if r.ok and "ok" in r.out else Verdict.FAIL
    s.detail = "gateway, cli, estado y adaptador Telegram importan" if r.ok else r.tail(800)
    suites.append(s)

    if restored_home is None:
        for n in ("smoke-config-check", "smoke-plugins", "smoke-db-migration"):
            suites.append(Suite(n, verdict=Verdict.BLOCKED, detail="sin HERMES_HOME restaurado del backup"))
        return suites
    henv = {**env, "HERMES_HOME": str(restored_home), "HERMES_UPDATER_UNDER_TEST": "1",
            # el smoke nunca debe hablar con Telegram ni con un LLM
            "HERMES_DISABLE_NETWORK": "1"}

    s = Suite("smoke-config-check")
    r = runner.run([python, str(cand_repo / "hermes_cli" / "main.py"), "config", "check"],
                   cwd=cand_repo, env=henv, timeout=300)
    # la salida lista nombres de variables; no se guarda (podria crecer con valores)
    s.verdict = Verdict.PASS if r.ok else Verdict.FAIL
    s.detail = f"config check rc={r.code}"
    suites.append(s)

    s = Suite("smoke-plugins")
    r = runner.run([python, str(cand_repo / "hermes_cli" / "main.py"), "plugins", "list", "--plain", "--no-bundled"],
                   cwd=cand_repo, env=henv, timeout=300)
    enabled = [l for l in r.out.splitlines() if "hermes_control" in l]
    s.verdict = Verdict.PASS if r.ok and enabled and "enabled" in enabled[0] else Verdict.FAIL
    s.detail = (enabled[0].strip()[:200] if enabled else "hermes_control no aparece") + f" (rc={r.code})"
    suites.append(s)

    s = Suite("smoke-db-migration")
    db = restored_home / "state.db"
    before = _db_facts(db)
    code = ("import sys; from pathlib import Path; from hermes_state import SessionDB; "
            "d=SessionDB(db_path=Path(sys.argv[1])); d.close() if hasattr(d,'close') else None; print('opened')")
    r = runner.run([python, "-c", code, str(db)], cwd=cand_repo, env=henv, timeout=600)
    after = _db_facts(db)
    ok = r.ok and after.get("integrity") == "ok" and after.get("sessions") == before.get("sessions") \
        and after.get("messages") == before.get("messages")
    s.verdict = Verdict.PASS if ok else Verdict.FAIL
    s.detail = (f"schema {before.get('schema')} -> {after.get('schema')}, sesiones {before.get('sessions')} -> "
                f"{after.get('sessions')}, mensajes {before.get('messages')} -> {after.get('messages')}, "
                f"integrity {after.get('integrity')}") + ("" if r.ok else " | " + r.tail(500))
    suites.append(s)
    return suites


def _db_facts(db: Path) -> dict:
    try:
        c = sqlite3.connect(str(db))
        try:
            f = {"integrity": c.execute("PRAGMA integrity_check").fetchone()[0]}
            for k, q in (("schema", "SELECT max(version) FROM schema_version"),
                         ("sessions", "SELECT count(*) FROM sessions"), ("messages", "SELECT count(*) FROM messages")):
                try:
                    f[k] = c.execute(q).fetchone()[0]
                except sqlite3.Error:
                    f[k] = None
            return f
        finally:
            c.close()
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def pure_target_worktree(runner: Runner, cand_repo: Path, tag: str, dest: Path) -> Optional[Path]:
    r = runner.run(["git", "-C", str(cand_repo), "worktree", "add", "--detach", "-f", str(dest), f"refs/tags/{tag}"],
                   timeout=600)
    return dest if r.ok else None


def run_all(cfg: Config, runner: Runner, *, python: str, cand_repo: Path, target_tag: Optional[str],
            carried_tests: Sequence[str], restored_home: Optional[Path], out: Path) -> list[Suite]:
    out.mkdir(parents=True, exist_ok=True)
    pure = pure_target_worktree(runner, cand_repo, target_tag, out / "pure-target") if target_tag else None
    suites = [
        upstream_suite(cfg, runner, "upstream-layer2", [*LAYER2, *carried_tests], python=python,
                       cand_repo=cand_repo, pure_target=None, out=out),
        upstream_suite(cfg, runner, "upstream-core", UPSTREAM_CORE, python=python,
                       cand_repo=cand_repo, pure_target=pure, out=out),
        hermes_control_suite(cfg, runner, python=python, cand_repo=cand_repo, out=out),
        *smoke(cfg, runner, python=python, cand_repo=cand_repo, restored_home=restored_home, out=out),
    ]
    return suites


def overall(suites: Sequence[Suite]) -> Verdict:
    return worst(s.verdict for s in suites if s.required)
