"""Despliegue controlado de un candidato APROBADO, pruebas posteriores y rollback.

Orden (cada paso deja rastro en el store ANTES de actuar):

  precondiciones  HEAD productivo == origen aprobado, rama y arbol limpios,
                  candidato intacto, backup pre-update intacto, lock igual,
                  recursos, gateway sano ANTES (si no, no se podria juzgar despues)
  punto rollback  HEAD viejo, copia del venv, snapshot consistente de las bases,
                  copia de unit + drop-ins
  codigo          git fetch del clon de staging + merge --ff-only (sin reescribir
                  nada: el candidato desciende del HEAD aprobado)
  dependencias    solo las lineas del lock que difieren, --no-deps, y luego el
                  editable: el venv queda como el que se probo
  servicio        UN reinicio; espera activo + PID estable + sin reinicios
  post-pruebas    version, logs, plugins, persistencia, config, capas externas
  rollback        (si algo falla) suave: HEAD viejo + venv viejo + unit vieja,
                  un reinicio, verificacion. La base se conserva (runbook:
                  hard nunca es automatico). Si la recuperacion falla, se para
                  todo y se reporta como INCIDENTE.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import backup as backup_mod
from . import verify
from .config import Config
from .proc import Runner
from .status import Verdict, worst


# --- servicio -------------------------------------------------------------------

class SystemdUser:
    """El gateway real. Solo verbos de usuario: nunca sudo."""

    def __init__(self, runner: Runner, unit: str):
        self.runner, self.unit = runner, unit

    def _show(self, prop: str) -> str:
        r = self.runner.run(["systemctl", "--user", "show", self.unit, "-p", prop, "--value"], timeout=30)
        return r.out.strip()

    def is_active(self) -> bool:
        return self.runner.run(["systemctl", "--user", "is-active", "--quiet", self.unit], timeout=30).ok

    def main_pid(self) -> int:
        v = self._show("MainPID")
        return int(v) if v.isdigit() else 0

    def restarts(self) -> int:
        v = self._show("NRestarts")
        return int(v) if v.isdigit() else 0

    def exec_start(self) -> str:
        return self._show("ExecStart")

    def restart(self) -> bool:
        return self.runner.run(["systemctl", "--user", "restart", self.unit], timeout=180).ok

    def daemon_reload(self) -> bool:
        return self.runner.run(["systemctl", "--user", "daemon-reload"], timeout=60).ok

    def journal_since(self, ts: float) -> str:
        r = self.runner.run(["journalctl", "--user", "-u", self.unit, "--since", f"@{int(ts)}", "-o", "cat",
                             "--no-pager"], timeout=60, keep=200_000)
        return r.out


def wait_healthy(svc, *, timeout_s: float, stable_s: float, sleep=time.sleep, clock=time.monotonic) -> tuple[bool, str]:
    """Activo, con el MISMO PID durante stable_s y sin reinicios nuevos."""
    deadline = clock() + timeout_s
    base_restarts = svc.restarts()
    while clock() < deadline:
        if svc.is_active():
            pid = svc.main_pid()
            t0 = clock()
            ok = True
            while clock() - t0 < stable_s:
                sleep(1)
                if not svc.is_active() or svc.main_pid() != pid or svc.restarts() != base_restarts:
                    ok = False
                    break
            if ok and pid:
                return True, f"activo, PID {pid} estable {stable_s:.0f}s, sin reinicios"
        sleep(2)
    return False, f"no sano tras {timeout_s:.0f}s (activo={svc.is_active()}, pid={svc.main_pid()}, reinicios={svc.restarts()})"


# --- utilidades --------------------------------------------------------------------

def _git(runner: Runner, repo: Path, *a, timeout=600):
    return runner.run(["git", "-C", str(repo), *a], timeout=timeout,
                      env={"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}, keep=None)


def snapshot_databases(hermes_home: Path, dest: Path) -> dict:
    """Copia consistente (Connection.backup) de TODAS las bases vivas."""
    dest.mkdir(parents=True, exist_ok=True)
    out = {}
    for rel in verify.live_databases(hermes_home):
        src = hermes_home / rel
        dst = dest / verify.flat(rel)
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        os.chmod(dst, 0o600)
        out[rel] = verify.sha256_file(dst)
    return out


def db_facts(path: Path) -> dict:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            f = {"quick_check": c.execute("PRAGMA quick_check").fetchone()[0]}
            for k, q in (("schema", "SELECT max(version) FROM schema_version"),
                         ("sessions", "SELECT count(*) FROM sessions"),
                         ("messages", "SELECT count(*) FROM messages")):
                try:
                    f[k] = c.execute(q).fetchone()[0]
                except sqlite3.Error:
                    f[k] = None
            return f
        finally:
            c.close()
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def content_digest(hermes_home: Path, dirs=("plugins", "scripts")) -> dict:
    out = {}
    for d in dirs:
        base = hermes_home / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                out[str(p.relative_to(hermes_home))] = verify.sha256_file(p)
    return out


EXTERNAL_LAYERS = ("plugins", "scripts", "patches", "anchors", "voice-samples", "skills", "memories", "cron")


@dataclass
class Step:
    name: str
    verdict: Verdict
    detail: str = ""

    def to_dict(self):
        return {"name": self.name, "verdict": self.verdict.value, "detail": self.detail}


@dataclass
class DeployResult:
    steps: list = field(default_factory=list)
    rollback_point: dict = field(default_factory=dict)
    deployed: bool = False
    post_ok: Optional[bool] = None
    rolled_back: Optional[bool] = None
    recovery_ok: Optional[bool] = None

    def add(self, name, verdict, detail=""):
        s = Step(name, Verdict(verdict), detail)
        self.steps.append(s)
        return s

    @property
    def verdict(self) -> Verdict:
        return worst(s.verdict for s in self.steps)

    def to_dict(self):
        return {"steps": [s.to_dict() for s in self.steps], "rollback_point": self.rollback_point,
                "deployed": self.deployed, "post_ok": self.post_ok, "rolled_back": self.rolled_back,
                "recovery_ok": self.recovery_ok}


# --- precondiciones ------------------------------------------------------------

def preconditions(cfg: Config, runner: Runner, payload: dict, svc) -> list[Step]:
    steps = []
    def add(n, ok, d):
        steps.append(Step(n, Verdict.PASS if ok else Verdict.BLOCKED, d))
    head = _git(runner, cfg.prod_repo, "rev-parse", "HEAD").out.strip()
    add("source_head", head == payload["source"]["commit"],
        f"HEAD productivo {head[:10]} {'==' if head == payload['source']['commit'] else '!='} origen aprobado {payload['source']['commit'][:10]}")
    br = _git(runner, cfg.prod_repo, "rev-parse", "--abbrev-ref", "HEAD").out.strip()
    add("branch", br == cfg.prod_branch, f"rama {br}")
    dirty = _git(runner, cfg.prod_repo, "status", "--porcelain", "--untracked-files=no").out.strip()
    add("clean_tree", not dirty, "arbol limpio" if not dirty else "cambios sin commit en produccion")
    srepo = Path(payload["candidate"]["repo"])
    c = payload["candidate"]["commit"]
    got = _git(runner, srepo, "rev-parse", f"{payload['candidate']['branch']}").out.strip() if srepo.exists() else ""
    parent = _git(runner, srepo, "rev-parse", f"{c}^1").out.strip() if got else ""
    add("candidate_intact", got == c and parent == payload["source"]["commit"],
        f"candidato {c[:10]} {'intacto y desciende del origen' if got == c and parent == payload['source']['commit'] else 'ALTERADO o ausente'}")
    lock = Path(payload["candidate"]["venv"]).parent / "requirements.lock"
    lock_ok = lock.is_file() and verify.sha256_file(lock) == payload["candidate"]["lock_sha256"]
    add("lock_intact", lock_ok, "lock de dependencias probado" if lock_ok else "lock alterado o ausente")
    ok, why = backup_mod.verify_preupdate_still_valid(Path(payload["backup"]["preupdate_dir"]))
    man = Path(payload["backup"]["preupdate_dir"]) / "MANIFEST.sha256"
    same = man.is_file() and verify.sha256_file(man) == payload["backup"]["manifest_sha256"]
    add("preupdate_backup", ok and same, why if not (ok and not same) else "MANIFEST cambiado desde la aprobacion")
    free_gb = shutil.disk_usage(cfg.prod_repo).free / 2**30
    add("disk", free_gb >= cfg.policy.min_free_disk_gb, f"{free_gb:.1f} GiB libres")
    add("gateway_before", svc.is_active(), "gateway activo antes de desplegar" if svc.is_active()
        else "gateway NO activo antes de desplegar: no se podria verificar despues")
    return steps


# --- despliegue ------------------------------------------------------------------

def deploy(cfg: Config, runner: Runner, payload: dict, svc, *, persist: Callable[[str, dict], None],
           ts: Optional[str] = None, sleep=time.sleep, clock=time.monotonic) -> DeployResult:
    """``persist(state, data)`` registra la transicion en el store ANTES de cada fase."""
    res = DeployResult()
    for s in preconditions(cfg, runner, payload, svc):
        res.steps.append(s)
    if res.verdict != Verdict.PASS:
        return res

    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    pre = Path(payload["backup"]["preupdate_dir"])
    venv = cfg.prod_repo / ".venv"
    venv_bak = cfg.prod_repo.parent / f".hermes-venv-rollback-{ts}"
    rp = {"old_head": payload["source"]["commit"], "branch": cfg.prod_branch, "venv": str(venv),
          "venv_backup": str(venv_bak), "systemd_copy": str(pre / "systemd"),
          "db_snapshot": str(pre / f"db-pre-deploy-{ts}"), "ts": ts,
          "db_before": db_facts(cfg.hermes_home / "state.db"),
          "content_before": content_digest(cfg.hermes_home),
          "exec_start_before": svc.exec_start()}
    res.rollback_point = rp
    persist("CONTROLLED_DEPLOYMENT", {"rollback_point": rp, "phase": "rollback_point"})

    cp = runner.run(["cp", "-a", "--reflink=auto", str(venv), str(venv_bak)], timeout=900)
    if not cp.ok:
        res.add("venv_backup", Verdict.FAIL, cp.tail(300))
        return res
    res.add("venv_backup", Verdict.PASS, str(venv_bak))
    try:
        rp["db_snapshot_sha"] = snapshot_databases(cfg.hermes_home, Path(rp["db_snapshot"]))
        res.add("db_snapshot", Verdict.PASS, f"{len(rp['db_snapshot_sha'])} bases copiadas (Connection.backup)")
    except sqlite3.Error as exc:
        res.add("db_snapshot", Verdict.FAIL, str(exc))
        shutil.rmtree(venv_bak, ignore_errors=True)
        return res
    persist("CONTROLLED_DEPLOYMENT", {"rollback_point": rp, "phase": "code"})

    # codigo: a partir de aqui produccion cambia -> cualquier fallo = rollback
    res.deployed = True
    cand = payload["candidate"]
    f = _git(runner, cfg.prod_repo, "fetch", "-q", cand["repo"], f"{cand['branch']}")
    m = _git(runner, cfg.prod_repo, "merge", "--ff-only", cand["commit"]) if f.ok else f
    if not m.ok:
        res.add("code", Verdict.FAIL, "fast-forward imposible: " + m.tail(300))
        return _rollback_and_verify(cfg, runner, svc, res, sleep=sleep, clock=clock, persist=persist)
    res.add("code", Verdict.PASS, f"{payload['source']['commit'][:10]} -> {cand['commit'][:10]} (fast-forward)")

    # dependencias: exactamente lo probado
    py = str(cfg.prod_python)
    fr = runner.run([py, "-m", "pip", "freeze", "--all", "--exclude-editable"], timeout=300, keep=None)
    lock_lines = {l.strip() for l in (Path(cand["venv"]).parent / "requirements.lock").read_text().splitlines()
                  if l.strip() and not l.startswith("#")}
    have = {l.strip() for l in fr.out.splitlines() if l.strip()}
    todo = sorted(l for l in lock_lines - have if "==" in l)
    if todo:
        req = Path(rp["db_snapshot"]).parent / f"deps-delta-{ts}.txt"
        req.write_text("\n".join(todo) + "\n")
        ins = runner.run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--no-deps", "-r", str(req)],
                         timeout=cfg.policy.test_timeout_s, env={"PIP_NO_INPUT": "1"})
        if not ins.ok:
            res.add("dependencies", Verdict.FAIL, "pip fallo: " + ins.tail(400))
            return _rollback_and_verify(cfg, runner, svc, res, sleep=sleep, clock=clock, persist=persist)
    ed = runner.run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--no-deps", "-e",
                     str(cfg.prod_repo)], timeout=900, env={"PIP_NO_INPUT": "1"})
    if not ed.ok:
        res.add("dependencies", Verdict.FAIL, "reinstalacion editable fallida: " + ed.tail(400))
        return _rollback_and_verify(cfg, runner, svc, res, sleep=sleep, clock=clock, persist=persist)
    res.add("dependencies", Verdict.PASS, f"{len(todo)} paquetes al lock probado")

    persist("SERVICE_RECOVERY", {"phase": "restart"})
    restart_at = time.time()
    rp["restart_at"] = restart_at
    if not svc.restart():
        res.add("restart", Verdict.FAIL, "systemctl restart fallo")
        return _rollback_and_verify(cfg, runner, svc, res, sleep=sleep, clock=clock, persist=persist)
    ok, why = wait_healthy(svc, timeout_s=cfg.policy.service_ready_timeout_s, stable_s=cfg.policy.service_stable_s,
                           sleep=sleep, clock=clock)
    res.add("service_recovery", Verdict.PASS if ok else Verdict.FAIL, why)
    if not ok:
        return _rollback_and_verify(cfg, runner, svc, res, sleep=sleep, clock=clock, persist=persist)

    persist("POST_UPDATE_TESTS", {"phase": "post"})
    post = post_tests(cfg, runner, payload, svc, rp)
    res.steps.extend(post)
    res.post_ok = all(s.verdict == Verdict.PASS for s in post)
    if not res.post_ok:
        return _rollback_and_verify(cfg, runner, svc, res, sleep=sleep, clock=clock, persist=persist)
    # exito: la copia del venv se conserva hasta la siguiente actualizacion buena
    return res


def post_tests(cfg: Config, runner: Runner, payload: dict, svc, rp: dict) -> list[Step]:
    steps = []
    def add(n, ok, d):
        steps.append(Step(n, Verdict.PASS if ok else Verdict.FAIL, d))
    head = _git(runner, cfg.prod_repo, "rev-parse", "HEAD").out.strip()
    add("post_version_commit", head == payload["candidate"]["commit"], f"HEAD {head[:10]}")
    py = str(cfg.prod_python)
    r = runner.run([py, "-c", "from hermes_cli.version_info import get_version_info as g; print(g().base_version)"],
                   cwd=cfg.prod_repo, timeout=120)
    want = payload["target"]["tag"].lstrip("v")
    got = r.out.strip().splitlines()[-1] if r.ok and r.out.strip() else "?"
    add("post_version_reported", got == want, f"Hermes informa {got}, se esperaba {want}")
    log = svc.journal_since(rp.get("restart_at", time.time()))
    bad = [l for l in log.splitlines() if "Traceback" in l or " CRITICAL" in l]
    add("post_logs", not bad, "sin Traceback/CRITICAL desde el reinicio" if not bad else f"{len(bad)} lineas graves: {bad[0][:200]}")
    es = svc.exec_start()
    add("post_unit", es == rp.get("exec_start_before"), "ExecStart sin cambios (drop-in intacto)" if es == rp.get("exec_start_before")
        else "la unit del gateway cambio durante el despliegue")
    r = runner.run([py, str(cfg.prod_repo / "hermes_cli" / "main.py"), "plugins", "list", "--plain", "--no-bundled"],
                   cwd=cfg.prod_repo, timeout=180, env={"HERMES_HOME": str(cfg.hermes_home)})
    hc = [l for l in r.out.splitlines() if "hermes_control" in l]
    add("post_plugins", bool(r.ok and hc and "enabled" in hc[0]), (hc[0].strip()[:160] if hc else "hermes_control no aparece"))
    after = db_facts(cfg.hermes_home / "state.db")
    before = rp.get("db_before") or {}
    ok = after.get("quick_check") == "ok" and (after.get("sessions") or 0) >= (before.get("sessions") or 0) \
        and (after.get("messages") or 0) >= (before.get("messages") or 0)
    add("post_persistence", ok, f"quick_check {after.get('quick_check')}, sesiones {before.get('sessions')}->{after.get('sessions')}, "
                                f"mensajes {before.get('messages')}->{after.get('messages')}, schema {before.get('schema')}->{after.get('schema')}")
    r = runner.run([py, str(cfg.prod_repo / "hermes_cli" / "main.py"), "config", "check"], cwd=cfg.prod_repo,
                   timeout=180, env={"HERMES_HOME": str(cfg.hermes_home)})
    add("post_config", r.ok, f"config check rc={r.code}")
    missing = [d for d in EXTERNAL_LAYERS if not (cfg.hermes_home / d).exists()]
    now = content_digest(cfg.hermes_home)
    changed = sorted(k for k in set(now) | set(rp.get("content_before", {}))
                     if now.get(k) != rp.get("content_before", {}).get(k))
    add("post_external_layers", not missing and not changed,
        "capas externas presentes y plugins/scripts identicos" if not missing and not changed
        else f"faltan {missing}; cambiados {changed[:5]}")
    return steps


def _rollback_and_verify(cfg, runner, svc, res: DeployResult, *, sleep, clock, persist) -> DeployResult:
    persist("ROLLBACK", {"phase": "rollback"})
    ok = rollback(cfg, runner, svc, res.rollback_point, res)
    res.rolled_back = ok
    persist("VERIFY_RECOVERY", {"phase": "verify_recovery"})
    healthy, why = wait_healthy(svc, timeout_s=cfg.policy.service_ready_timeout_s, stable_s=cfg.policy.service_stable_s,
                                sleep=sleep, clock=clock)
    head = _git(runner, cfg.prod_repo, "rev-parse", "HEAD").out.strip()
    back = head == res.rollback_point.get("old_head")
    res.recovery_ok = bool(ok and healthy and back)
    res.add("verify_recovery", Verdict.PASS if res.recovery_ok else Verdict.FAIL,
            f"{why}; HEAD {'restaurado' if back else 'NO restaurado'} ({head[:10]})")
    return res


def rollback(cfg: Config, runner: Runner, svc, rp: dict, res: Optional[DeployResult] = None) -> bool:
    """Rollback SUAVE: codigo + venv + unit. La base se conserva (el runbook
    reserva el hard rollback para una decision humana)."""
    def add(n, ok, d):
        if res is not None:
            res.add(n, Verdict.PASS if ok else Verdict.FAIL, d)
        return ok
    ok_all = True
    r = _git(runner, cfg.prod_repo, "reset", "--hard", "-q", rp["old_head"])
    ok_all &= add("rollback_code", r.ok, f"HEAD -> {rp['old_head'][:10]}" if r.ok else r.tail(300))
    venv, bak = Path(rp["venv"]), Path(rp["venv_backup"])
    if bak.is_dir():
        failed = venv.with_name(f".venv.failed-{rp.get('ts', 'x')}")
        try:
            if venv.exists():
                os.replace(venv, failed)
            os.replace(bak, venv)
            shutil.rmtree(failed, ignore_errors=True)
            ok_all &= add("rollback_venv", True, "venv anterior restaurado")
        except OSError as exc:
            ok_all &= add("rollback_venv", False, str(exc))
    else:
        ok_all &= add("rollback_venv", False, "no existe la copia del venv")
    # unit + drop-ins: solo si cambiaron
    src = Path(rp["systemd_copy"])
    changed = False
    for f in src.rglob("*") if src.is_dir() else []:
        if f.is_file():
            dst = cfg.systemd_user_dir / f.relative_to(src)
            if not dst.is_file() or verify.sha256_file(dst) != verify.sha256_file(f):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                changed = True
    if changed:
        ok_all &= add("rollback_unit", svc.daemon_reload(), "unit/drop-ins restaurados y recargados")
    ok_all &= add("rollback_restart", svc.restart(), "reinicio tras rollback")
    return ok_all


def hard_restore_databases(cfg: Config, svc, rp: dict) -> list[str]:
    """SOLO manual (CLI con --hard). Para el gateway, restaura TODAS las bases
    desde el snapshot pre-despliegue, preservando la actual al lado."""
    out = []
    snap = Path(rp["db_snapshot"])
    stopped = svc.runner.run(["systemctl", "--user", "stop", svc.unit], timeout=120).ok if hasattr(svc, "runner") else True
    if not stopped or svc.is_active():
        raise RuntimeError("el gateway sigue activo: no se sustituye ninguna base con un escritor vivo")
    keep = snap.parent / f"db-displaced-{time.strftime('%Y%m%d-%H%M%S')}"
    keep.mkdir(parents=True)
    for rel in verify.live_databases(cfg.hermes_home):
        src = snap / verify.flat(rel)
        if not src.is_file():
            continue
        live = cfg.hermes_home / rel
        mode = os.stat(live).st_mode & 0o777
        os.replace(live, keep / verify.flat(rel))
        for sfx in ("-wal", "-shm"):
            if Path(str(live) + sfx).exists():
                os.replace(Path(str(live) + sfx), keep / (verify.flat(rel) + sfx))
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(str(live))
        s.backup(d)
        d.close()
        s.close()
        os.chmod(live, mode)
        out.append(rel)
    return out
