"""Preparacion AISLADA de un candidato de Hermes.

Nunca toca el checkout productivo: clona con ``--shared`` (lee sus objetos,
no escribe en el), trae upstream al clon, y fusiona el tag objetivo sobre la
rama propia en una rama ``updater/candidate-*``. Los commits del usuario no se
reescriben: es un merge, como el que ya se hizo a mano el 2026-09-30.

Si hay conflictos, se aborta el merge y el candidato queda BLOCKED con la lista
exacta de ficheros. No se fuerza nada, no se aplican parches viejos a ciegas.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config
from .proc import Result, Runner, retry
from .status import Verdict

IDENTITY = ("Mauricio", "68365906+mauricio2093@users.noreply.github.com")
SCHEMA_FILE = "hermes_state_common.py"
CONFIG_DEFAULTS = "hermes_cli/config_defaults.py"
PLUGIN_API_HINTS = ("hermes_cli/plugins", "gateway/plugin", "plugins/platforms/")


@dataclass
class Candidate:
    verdict: Verdict = Verdict.INSUFFICIENT_DATA
    workdir: Optional[str] = None
    repo: Optional[str] = None
    venv: Optional[str] = None
    branch: Optional[str] = None
    source_commit: Optional[str] = None
    target_tag: Optional[str] = None
    target_commit: Optional[str] = None
    candidate_commit: Optional[str] = None
    conflicts: list = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    deps: dict = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["verdict"] = self.verdict.value
        return d


class Git:
    def __init__(self, runner: Runner, repo: Path):
        self.runner, self.repo = runner, repo

    def __call__(self, *args, timeout=600, env=None) -> Result:
        e = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
             "GIT_AUTHOR_NAME": IDENTITY[0], "GIT_AUTHOR_EMAIL": IDENTITY[1],
             "GIT_COMMITTER_NAME": IDENTITY[0], "GIT_COMMITTER_EMAIL": IDENTITY[1]}
        if env:
            e.update(env)
        return self.runner.run(["git", "-C", str(self.repo), *args], timeout=timeout, env=e, keep=None)

    def out(self, *args, **kw) -> str:
        r = self(*args, **kw)
        return r.out.strip() if r.ok else ""




def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_extras(pyproject_text: str, installed: dict, env: Optional[dict] = None) -> list[str]:
    """Extras ya elegidos en el venv productivo, leidos del pyproject de
    PRODUCCION: los que tienen TODOS sus requisitos aplicables cumplidos por lo
    instalado (``installed``: nombre normalizado -> version, o None si es una
    instalacion directa). Los meta-extras (``hermes-agent[...]``) no cuentan.

    Antes bastaba con UN paquete en comun: ``aiohttp`` o ``httpx`` arrastraban
    dingtalk, teams, matrix... y el candidato instalaba ~50 paquetes que
    produccion nunca tuvo, ademas de bajar ``fastapi`` al pin de ``[web]``,
    un extra que produccion no tenia. ``env`` son los marcadores del Python
    del venv (no los del Python que ejecuta el actualizador)."""
    from packaging.requirements import InvalidRequirement, Requirement

    data = tomllib.loads(pyproject_text)
    project = data.get("project", {})
    me = _norm(project.get("name", "hermes-agent"))
    out = []
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        own, ok = 0, True
        for r in reqs:
            try:
                req = Requirement(r)
            except InvalidRequirement:
                ok = False
                break
            if _norm(req.name) == me:
                continue
            if req.marker is not None and not req.marker.evaluate({**(env or {}), "extra": extra}):
                continue
            own += 1
            name = _norm(req.name)
            if name not in installed:
                ok = False
                break
            ver = installed[name]
            if req.specifier and (ver is None or not req.specifier.contains(ver, prereleases=True)):
                ok = False
                break
        if own and ok:
            out.append(extra)
    return sorted(out)


def _installed_versions(freeze: str) -> dict:
    out = {}
    for line in freeze.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            name, ver = line.split("==", 1)
            out[_norm(name)] = ver.strip()
        else:
            out[_norm(line.split(" @ ")[0].strip())] = None
    return out


_MARKER_ENV_PROBE = (
    "import json\n"
    "try:\n"
    "    from packaging.markers import default_environment\n"
    "except ImportError:\n"
    "    from pip._vendor.packaging.markers import default_environment\n"
    "print(json.dumps(default_environment()))\n"
)


def _deps(pyproject_text: str) -> dict:
    p = tomllib.loads(pyproject_text).get("project", {})
    return {"dependencies": sorted(p.get("dependencies") or []),
            "requires-python": p.get("requires-python")}


def _schema_version(text: str) -> Optional[int]:
    m = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def _config_version(text: str) -> Optional[int]:
    m = re.search(r'"_config_version":\s*(\d+)', text)
    return int(m.group(1)) if m else None


UPSTREAM_MAIN = "refs/remotes/upstream/main"


def analyze(git: Git, source: str, target: str, candidate: Optional[str]) -> dict:
    """Relacion explicita origen -> destino (protocolo, paso 6)."""
    a: dict = {}
    has_main = git("rev-parse", "--verify", "-q", UPSTREAM_MAIN).ok
    # Propios = en el origen, ni en el objetivo ni en upstream/main.
    carried = git.out("rev-list", "--no-merges", "--reverse", source, f"^{target}",
                      *([f"^{UPSTREAM_MAIN}"] if has_main else [])).split()
    cherry = {}
    for line in git.out("cherry", target, source).splitlines():
        sign, _, sha = line.partition(" ")
        cherry[sha.strip()] = sign
    carried_info, carried_files = [], set()
    for c in carried:
        files = git.out("show", "--name-only", "--format=", c).split()
        carried_files.update(files)
        carried_info.append({"commit": c[:10], "subject": git.out("log", "-1", "--format=%s", c),
                             "already_upstream": cherry.get(c) == "-", "files": files})
    a["carried"] = carried_info
    a["carried_still_needed"] = sum(1 for c in carried_info if not c["already_upstream"])
    a["carried_already_upstream"] = [c["commit"] for c in carried_info if c["already_upstream"]]
    mb = git.out("merge-base", source, target)
    a["merge_base"] = mb[:10]
    upstream_files = set(git.out("diff", "--name-only", f"{mb}..{target}").split()) if mb else set()
    a["upstream_commits"] = int(git.out("rev-list", "--count", f"{mb}..{target}") or 0) if mb else None
    a["upstream_files_changed"] = len(upstream_files)
    a["overlap_with_carried"] = sorted(carried_files & upstream_files)
    a["plugin_api_files_changed"] = sorted(f for f in upstream_files if f.startswith(PLUGIN_API_HINTS))[:60]
    a["migration_files_changed"] = sorted(f for f in upstream_files if "migrat" in f.lower()
                                          and not f.startswith("tests/"))[:60]
    sv_old = _schema_version(git.out("show", f"{source}:{SCHEMA_FILE}"))
    sv_new = _schema_version(git.out("show", f"{target}:{SCHEMA_FILE}"))
    cv_old = _config_version(git.out("show", f"{source}:{CONFIG_DEFAULTS}"))
    cv_new = _config_version(git.out("show", f"{target}:{CONFIG_DEFAULTS}"))
    a["schema_version"] = {"source": sv_old, "target": sv_new, "changes": sv_old != sv_new}
    a["config_version"] = {"source": cv_old, "target": cv_new, "changes": cv_old != cv_new}
    try:
        d_old = _deps(git.out("show", f"{source}:pyproject.toml"))
        d_new = _deps(git.out("show", f"{target}:pyproject.toml"))
        a["dependencies"] = {
            "added": sorted(set(d_new["dependencies"]) - set(d_old["dependencies"])),
            "removed": sorted(set(d_old["dependencies"]) - set(d_new["dependencies"])),
            "requires_python": [d_old["requires-python"], d_new["requires-python"]]}
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        a["dependencies"] = {"error": str(exc)}
    # Lo que de upstream YA corre en produccion: el ultimo commit de upstream/main
    # contenido en el origen. Si el objetivo no lo contiene, fusionarlo mezclaria
    # una linea de version anterior con una posterior.
    ub = git.out("merge-base", source, UPSTREAM_MAIN) if has_main else mb
    a["source_upstream_base"] = (ub or "")[:10]
    a["target_contains_source_upstream_base"] = bool(ub) and git("merge-base", "--is-ancestor", ub, target).ok
    if candidate:
        a["candidate_diff_vs_target"] = git.out("diff", "--stat", "--stat-width=100", target, candidate)[-3000:]
    return a


def prepare(cfg: Config, runner: Runner, *, target_tag: str, target_commit: str,
            tag_object: Optional[str] = None, build_venv: bool = True,
            stamp: Optional[str] = None) -> Candidate:
    cand = Candidate(target_tag=target_tag, target_commit=target_commit)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    work = cfg.work_dir / "staging" / f"{target_tag}-{stamp}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    cand.workdir = str(work)
    repo = work / "repo"
    cand.repo = str(repo)
    prod = Git(runner, cfg.prod_repo)
    source = prod.out("rev-parse", "HEAD")
    branch = prod.out("rev-parse", "--abbrev-ref", "HEAD")
    if not source:
        cand.verdict, cand.detail = Verdict.FAIL, "no se pudo leer HEAD productivo"
        return cand
    if branch != cfg.prod_branch:
        cand.verdict = Verdict.BLOCKED
        cand.detail = f"produccion esta en la rama {branch!r}, se esperaba {cfg.prod_branch!r}"
        return cand
    cand.source_commit = source

    r = runner.run(["git", "clone", "-q", "--no-checkout", "--shared", "--branch", branch,
                    str(cfg.prod_repo), str(repo)], timeout=600, env={"GIT_TERMINAL_PROMPT": "0"})
    if not r.ok:
        cand.verdict, cand.detail = Verdict.FAIL, "clon de staging fallido: " + r.tail(300)
        return cand
    git = Git(runner, repo)
    git("remote", "set-url", "origin", cfg.upstream_url)
    pol = cfg.policy
    f = retry(lambda: git("fetch", "-q", "--no-tags", "origin",
                          f"+refs/tags/{target_tag}:refs/tags/{target_tag}",
                          "+refs/heads/main:refs/remotes/upstream/main", timeout=1800),
              attempts=pol.net_retries, backoff_s=pol.net_backoff_s)
    if not f.ok:
        cand.verdict = Verdict.INSUFFICIENT_DATA if f.transient else Verdict.FAIL
        cand.detail = "no se pudo traer upstream: " + f.tail(300)
        return cand
    # integridad de referencias: lo traido == lo anunciado por ls-remote
    got_obj = git.out("rev-parse", f"refs/tags/{target_tag}")
    got_commit = git.out("rev-parse", f"refs/tags/{target_tag}^{{commit}}")
    ref_ok = got_commit == target_commit and (tag_object is None or got_obj == tag_object)
    sig = git("verify-tag", target_tag, timeout=60)
    cand.analysis["ref_integrity"] = {
        "tag_object": got_obj, "commit": got_commit, "matches_ls_remote": ref_ok,
        "signature": "PASS" if sig.ok else "NOT_OBSERVED",
        "signature_detail": sig.tail(200).strip() or "sin firma verificable con las claves locales"}
    if not ref_ok:
        cand.verdict = Verdict.FAIL
        cand.detail = f"el tag {target_tag} traido ({got_commit[:10]}) no coincide con el anunciado ({target_commit[:10]})"
        return cand

    cbranch = f"updater/candidate-{target_tag}-{stamp}"
    cand.branch = cbranch
    co = git("checkout", "-q", "-b", cbranch, source)
    if not co.ok:
        cand.verdict, cand.detail = Verdict.FAIL, "checkout de staging fallido: " + co.tail(300)
        return cand
    msg = (f"Merge upstream {target_tag} into {branch}\n\n"
           f"Candidate prepared by hermes-updater in an isolated clone.\n"
           f"source {source}\ntarget {target_commit} ({target_tag})")
    m = git("merge", "--no-ff", "--no-edit", "-m", msg, f"refs/tags/{target_tag}", timeout=900)
    if not m.ok:
        cand.conflicts = git.out("diff", "--name-only", "--diff-filter=U").split()
        git("merge", "--abort")
        cand.analysis.update(analyze(git, source, target_commit, None))
        cand.verdict = Verdict.BLOCKED
        cand.detail = ("conflictos de merge en: " + ", ".join(cand.conflicts)) if cand.conflicts \
            else "merge fallido: " + m.tail(400)
        return cand
    cand.candidate_commit = git.out("rev-parse", "HEAD")
    cand.analysis.update(analyze(git, source, target_commit, cand.candidate_commit))
    if not cand.analysis.get("target_contains_source_upstream_base"):
        cand.verdict = Verdict.BLOCKED
        cand.detail = ("el tag objetivo NO contiene la base upstream que ya corre en produccion: "
                       "fusionarlo mezclaria lineas de version; requiere decision humana")
        return cand

    if build_venv:
        ok, detail = build_staging_venv(cfg, runner, cand)
        if not ok:
            cand.verdict, cand.detail = Verdict.FAIL, detail
            return cand
    cand.verdict = Verdict.PASS
    cand.detail = f"merge limpio de {target_tag} sobre {branch}; {cand.analysis['carried_still_needed']} commits propios conservados"
    return cand


def build_staging_venv(cfg: Config, runner: Runner, cand: Candidate) -> tuple[bool, str]:
    """Copia del venv productivo + instalacion editable del candidato. Asi solo
    se descarga lo que cambia, y lo que se prueba es exactamente lo que se
    desplegara (el lock resultante viaja con el candidato)."""
    work = Path(cand.workdir)
    venv = work / "venv"
    cp = runner.run(["cp", "-a", "--reflink=auto", str(cfg.prod_repo / ".venv"), str(venv)], timeout=900)
    if not cp.ok:
        return False, "no se pudo copiar el venv productivo: " + cp.tail(300)
    cand.venv = str(venv)
    py = str(venv / "bin" / "python")
    fr = runner.run([py, "-m", "pip", "freeze", "--all"], timeout=300, keep=None)
    if not fr.ok:
        return False, "pip freeze del venv copiado fallido: " + fr.tail(300)
    menv = runner.run([py, "-I", "-c", _MARKER_ENV_PROBE], timeout=120)
    try:
        env = json.loads(menv.out) if menv.ok else None
    except ValueError:
        env = None
    if not isinstance(env, dict):
        return False, "no se pudieron leer los marcadores del Python del venv: " + menv.tail(300)
    # Los extras se deciden con el pyproject de PRODUCCION (lo que de verdad se
    # instalo) y se instalan los que siguen existiendo en el candidato.
    prod_extras = installed_extras((cfg.prod_repo / "pyproject.toml").read_text(),
                                   _installed_versions(fr.out), env)
    target = tomllib.loads((Path(cand.repo) / "pyproject.toml").read_text())
    target_extras = set((target.get("project", {}).get("optional-dependencies") or {}))
    extras = [e for e in prod_extras if e in target_extras]
    cand.deps["extras"] = extras
    cand.deps["extras_gone_in_target"] = [e for e in prod_extras if e not in target_extras]
    spec = f"{cand.repo}[{','.join(extras)}]" if extras else cand.repo
    ins = runner.run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", spec],
                     timeout=cfg.policy.test_timeout_s, env={"PIP_NO_INPUT": "1"})
    cand.deps["install_tail"] = ins.tail(2000)
    if not ins.ok:
        return False, "instalacion de dependencias del candidato fallida: " + ins.tail(500)
    chk = runner.run([py, "-m", "pip", "check"], timeout=300)
    cand.deps["pip_check"] = {"ok": chk.ok, "out": chk.tail(2000)}
    lock = runner.run([py, "-m", "pip", "freeze", "--all", "--exclude-editable"], timeout=300, keep=None)
    (work / "requirements.lock").write_text(lock.out)
    cand.deps["lock"] = str(work / "requirements.lock")
    cand.deps["lock_lines"] = len(lock.out.splitlines())
    before = {l for l in fr.out.splitlines() if "==" in l}
    after = {l for l in lock.out.splitlines() if "==" in l}
    cand.deps["changed"] = sorted(after - before)[:200]
    cand.deps["dropped"] = sorted(before - after - {l for l in before if l.lower().startswith("hermes-agent")})[:200]
    return True, "venv del candidato listo"


def write_candidate(cand: Candidate, path: Path) -> None:
    path.write_text(json.dumps(cand.to_dict(), indent=2, default=str))
