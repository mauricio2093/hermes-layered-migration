"""Deteccion de versiones de Hermes, Claude Code, Codex y OpenCode.

Nada de aqui modifica nada:
  * Hermes: solo comandos git de lectura sobre el checkout productivo y
    ``git ls-remote`` contra upstream. NO hay ``git fetch`` en produccion:
    los objetos nuevos se traen en el clon de staging, nunca aqui.
  * CLIs: ``--version`` del binario instalado y el registro publico de npm
    (mismo numero de version que publican los instaladores nativos).

Cada producto devuelve un ``ProductVersion`` con su veredicto. Si no se pudo
saber algo, se dice (INSUFFICIENT_DATA / NOT_OBSERVED); jamas se rellena.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .proc import Result, Runner, retry
from .status import Verdict

STABLE_TAG = re.compile(r"^v(0|[1-9]\d{0,2})\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PRERELEASE_TAG = re.compile(r"^rc\.(\d+)-v(\d+)\.(\d+)\.(\d+)$")
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")

NPM_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "opencode": "opencode-ai",
}


def semver(s: Optional[str]) -> Optional[tuple]:
    if not s:
        return None
    m = SEMVER.search(s)
    return tuple(int(x) for x in m.groups()) if m else None


@dataclass
class ProductVersion:
    product: str
    installed: Optional[str] = None
    available: Optional[str] = None
    update_available: Optional[bool] = None   # None = no se sabe
    verdict: Verdict = Verdict.INSUFFICIENT_DATA
    method: Optional[str] = None              # como esta instalado
    update_command: Optional[tuple] = None    # mecanismo oficial
    major_jump: bool = False
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def status_label(self) -> str:
        if self.update_available is True:
            return "Actualizacion disponible"
        if self.update_available is False:
            return "Actualizado"
        return "Sin datos"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


# --- red -------------------------------------------------------------------

FetchJson = Callable[[str], dict]


def http_json(url: str, *, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "hermes-updater/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (URL fija, https)
        return json.loads(resp.read(2_000_000))


def fetch_with_retry(fetch: FetchJson, url: str, *, attempts: int, backoff_s: float,
                     sleep=time.sleep) -> tuple[Optional[dict], Optional[str]]:
    last = None
    for i in range(max(1, attempts)):
        try:
            return fetch(url), None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                break
            if i + 1 < attempts:
                sleep(backoff_s * (2 ** i))
    return None, last


# --- Hermes ------------------------------------------------------------------

def _git(runner: Runner, repo: Path, *args, timeout=60) -> Result:
    return runner.run(["git", "-C", str(repo), *args], timeout=timeout,
                      env={"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}, keep=None)


def parse_ls_remote(text: str) -> dict:
    """{tag: commit_peeled} para tags estables y prereleases; y 'refs/heads/main'."""
    raw, peeled = {}, {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = sha
        else:
            raw[ref] = sha
    out = {}
    for ref, sha in raw.items():
        out[ref] = {"object": sha, "commit": peeled.get(ref, sha)}
    return out


def hermes_installed(cfg: Config, runner: Runner) -> dict:
    repo = cfg.prod_repo
    info: dict = {"repo": str(repo)}
    head = _git(runner, repo, "rev-parse", "HEAD")
    if not head.ok:
        info["error"] = "no se pudo leer HEAD: " + head.tail(300)
        return info
    info["commit"] = head.out.strip()
    br = _git(runner, repo, "rev-parse", "--abbrev-ref", "HEAD")
    info["branch"] = br.out.strip() if br.ok else None
    sh = _git(runner, repo, "rev-parse", "--is-shallow-repository")
    info["shallow"] = sh.out.strip() == "true"
    st = _git(runner, repo, "status", "--porcelain", "--untracked-files=no")
    info["dirty_tracked"] = bool(st.out.strip()) if st.ok else None
    tags = _git(runner, repo, "tag", "--merged", "HEAD", "--list", "v*")
    stable = sorted((t for t in tags.out.split() if STABLE_TAG.match(t)), key=semver)
    info["base_tag"] = stable[-1] if stable else None
    if info["base_tag"]:
        d = _git(runner, repo, "rev-list", "--count", f"{info['base_tag']}..HEAD")
        info["distance"] = int(d.out.strip()) if d.ok and d.out.strip().isdigit() else None
    # commits propios: no alcanzables desde ninguna ref upstream conocida localmente
    upstream_refs = [r for r in _git(runner, repo, "for-each-ref", "--format=%(refname)",
                                     "refs/remotes/origin/main").out.split() if r]
    if upstream_refs:
        carried = _git(runner, repo, "rev-list", "--no-merges", "HEAD", *[f"^{r}" for r in upstream_refs])
        info["carried_commits"] = carried.out.split() if carried.ok else None
        info["carried_basis"] = "HEAD ^origin/main (ref local, puede estar atrasada)"
    date = _git(runner, repo, "log", "-1", "--format=%cI", "HEAD")
    info["commit_date"] = date.out.strip() if date.ok else None
    return info


def calver_base(repo: Path, runner: Runner, rev: str = "HEAD") -> Optional[dict]:
    d = _git(runner, repo, "describe", "--tags", "--long", "--match", "v2[0-9][0-9][0-9].*", rev)
    if not d.ok:
        return None
    try:
        tag, count, _ = d.out.strip().rsplit("-", 2)
    except ValueError:
        return None
    show = _git(runner, repo, "show", f"{tag}:pyproject.toml")
    if not show.ok:
        return None
    try:
        import tomllib
        version = tomllib.loads(show.out).get("project", {}).get("version")
    except ValueError:
        return None
    if not semver(version) or version == "0.0.0":
        return None
    return {"base_tag": f"v{version}", "calver_tag": tag, "distance": int(count) if count.isdigit() else None,
            "base_basis": f"pyproject de {tag} (CalVer mas cercano en HEAD), como hermes_cli/version_info.py"}


def hermes_version(cfg: Config, runner: Runner, *, ls_remote: Optional[Callable[[], Result]] = None) -> ProductVersion:
    pv = ProductVersion("hermes", method="git checkout (rama propia sobre upstream)",
                        update_command=("hermes-updater", "prepare"))
    inst = hermes_installed(cfg, runner)
    pv.extra["installed"] = inst
    if "error" in inst:
        pv.verdict = Verdict.FAIL
        pv.detail = inst["error"]
        return pv
    pv.installed = inst.get("base_tag") or inst["commit"][:10]
    if inst.get("distance"):
        pv.extra["display"] = f"{inst['base_tag']}+{inst['distance']}.g{inst['commit'][:7]}"
    if inst.get("shallow"):
        pv.detail = "checkout shallow: la ascendencia no es fiable"
        pv.verdict = Verdict.INSUFFICIENT_DATA
        return pv

    pol = cfg.policy
    def _ls():
        return runner.run(["git", "ls-remote", "--tags", "--heads", cfg.upstream_url],
                          timeout=pol.net_timeout_s * 3, env={"GIT_TERMINAL_PROMPT": "0"}, keep=None)
    res = retry(ls_remote or _ls, attempts=pol.net_retries, backoff_s=pol.net_backoff_s)
    if not res.ok:
        pv.verdict = Verdict.INSUFFICIENT_DATA
        pv.detail = "upstream inalcanzable: " + res.tail(300).strip()
        pv.extra["network_error"] = True
        return pv
    refs = parse_ls_remote(res.out)
    stable = {r[len("refs/tags/"):]: v for r, v in refs.items()
              if r.startswith("refs/tags/") and STABLE_TAG.match(r[len("refs/tags/"):])}
    pre = sorted((r[len("refs/tags/"):] for r in refs if r.startswith("refs/tags/")
                  and PRERELEASE_TAG.match(r[len("refs/tags/"):])),
                 key=lambda t: (semver(t.split("-", 1)[1]), int(PRERELEASE_TAG.match(t).group(1))))
    main = refs.get("refs/heads/main", {}).get("commit")
    pv.extra.update(upstream_main=main, latest_prerelease=pre[-1] if pre else None)
    if not stable:
        pv.verdict = Verdict.INSUFFICIENT_DATA
        pv.detail = "upstream no publica tags estables reconocibles"
        return pv
    if not inst.get("base_tag"):
        # Upstream etiqueta main con CalVer (vYYYY.M.D) y las versiones semver
        # en ramas release/*. Igual que hermes_cli/version_info.py: la version
        # que corre es la del pyproject del CalVer mas cercano en HEAD.
        cal = calver_base(cfg.prod_repo, runner)
        if cal:
            inst.update(cal)
            pv.installed = cal["base_tag"]
            pv.extra["display"] = (f"{cal['base_tag']} ({cal['calver_tag'][1:]})"
                                   + (f" +{cal['distance']}" if cal.get("distance") else ""))
    if not inst.get("base_tag"):
        # Ultimo recurso: el tag estable mas alto cuyo commit YA esta en HEAD.
        for tag in sorted(stable, key=semver, reverse=True):
            c = stable[tag]["commit"]
            if (_git(runner, cfg.prod_repo, "cat-file", "-e", f"{c}^{{commit}}").ok and
                    _git(runner, cfg.prod_repo, "merge-base", "--is-ancestor", c, "HEAD").ok):
                inst["base_tag"] = tag
                inst["base_basis"] = "tag estable de upstream (ls-remote) contenido en HEAD"
                d = _git(runner, cfg.prod_repo, "rev-list", "--count", f"{c}..HEAD")
                inst["distance"] = int(d.out.strip()) if d.ok and d.out.strip().isdigit() else None
                break
        if inst.get("base_tag"):
            pv.installed = inst["base_tag"]
            if inst.get("distance"):
                pv.extra["display"] = f"{inst['base_tag']}+{inst['distance']}.g{inst['commit'][:7]}"
    latest = max(stable, key=semver)
    target_commit = stable[latest]["commit"]
    pv.available = latest
    pv.extra["target"] = {"tag": latest, "tag_object": stable[latest]["object"], "commit": target_commit}
    # Que el commit del tag ya este en HEAD es la prueba; el numero, no.
    present = _git(runner, cfg.prod_repo, "cat-file", "-e", f"{target_commit}^{{commit}}").ok
    if present:
        contained = _git(runner, cfg.prod_repo, "merge-base", "--is-ancestor", target_commit, "HEAD").ok
    else:
        contained = False        # sin el objeto y sin shallow: no es ancestro
    pv.extra["target_in_head"] = contained
    base = semver(pv.installed if inst.get("base_tag") else None)
    if contained:
        pv.update_available = False
    elif base is not None and semver(latest) <= base:
        # El tag no esta en HEAD pero no es mas nuevo: rama paralela / backport.
        pv.update_available = False
        pv.detail = f"{latest} no es posterior a {pv.installed}; no se considera actualizacion"
    else:
        pv.update_available = True
        if base and semver(latest)[0] != base[0]:
            pv.major_jump = True
    if main:
        pv.extra["main_in_head"] = (_git(runner, cfg.prod_repo, "cat-file", "-e", f"{main}^{{commit}}").ok and
                                    _git(runner, cfg.prod_repo, "merge-base", "--is-ancestor", main, "HEAD").ok)
    pv.verdict = Verdict.PASS
    return pv


# --- CLIs auxiliares -------------------------------------------------------------

def _which(name: str, path_env: Optional[str] = None) -> Optional[Path]:
    for d in (path_env or os.environ.get("PATH", "")).split(os.pathsep):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def install_method(product: str, binary: Path) -> tuple[str, Optional[tuple]]:
    """(metodo, comando oficial de actualizacion) segun DONDE esta instalado."""
    real = Path(os.path.realpath(binary))
    s = str(real)
    if product == "claude":
        if "/.local/share/claude/versions/" in s:
            return "native (~/.local/share/claude/versions)", ("claude", "update")
        if "node_modules" in s:
            return "npm global", None                 # requiere npm; no se automatiza
    if product == "codex":
        if "/.codex/packages/standalone/" in s:
            return "standalone (~/.codex/packages/standalone)", ("codex", "update")
        if "node_modules" in s:
            return "npm global", None
    if product == "opencode":
        if "/.opencode/bin/" in s:
            return "curl (~/.opencode/bin)", ("opencode", "upgrade", "--method", "curl")
        if "node_modules" in s:
            return "npm global", None
    return f"desconocido ({real})", None


def cli_version(product: str, cfg: Config, runner: Runner, fetch: FetchJson,
                *, path_env: Optional[str] = None, sleep=time.sleep) -> ProductVersion:
    pv = ProductVersion(product)
    binary = _which(product, path_env)
    if binary is None:
        pv.verdict = Verdict.NOT_OBSERVED
        pv.detail = "no instalado en PATH"
        return pv
    pv.method, pv.update_command = install_method(product, binary)
    pv.extra["binary"] = str(binary)
    pv.extra["realpath"] = os.path.realpath(binary)
    r = runner.run([str(binary), "--version"], timeout=30, env={"NO_COLOR": "1", "CI": "1"})
    ver = semver(r.out) if r.ok else None
    if ver is None:
        pv.verdict = Verdict.FAIL
        pv.detail = f"--version fallo: {r.tail(200).strip()}"
        return pv
    pv.installed = ".".join(map(str, ver))
    pkg = NPM_PACKAGES[product]
    data, err = fetch_with_retry(fetch, f"https://registry.npmjs.org/-/package/{pkg}/dist-tags",
                                 attempts=cfg.policy.net_retries, backoff_s=cfg.policy.net_backoff_s, sleep=sleep)
    if data is None:
        pv.verdict = Verdict.INSUFFICIENT_DATA
        pv.detail = f"registro inalcanzable: {err}"
        pv.extra["network_error"] = True
        return pv
    tags = data if isinstance(data, dict) else {}
    channel = "latest"
    if product == "claude":
        channel = claude_channel()
    latest = tags.get(channel) or tags.get("latest")
    if not semver(latest):
        pv.verdict = Verdict.INSUFFICIENT_DATA
        pv.detail = f"el registro no publica dist-tag '{channel}'"
        return pv
    pv.available = ".".join(map(str, semver(latest)))
    pv.extra["channel"] = channel
    pv.update_available = semver(latest) > ver
    pv.major_jump = semver(latest)[0] != ver[0]
    pv.verdict = Verdict.PASS
    return pv


def claude_channel(settings: Path = Path.home() / ".claude" / "settings.json") -> str:
    """Solo lee la clave del canal; nunca el resto del fichero (puede llevar env)."""
    try:
        value = json.loads(settings.read_text()).get("autoUpdatesChannel")
    except (OSError, ValueError, AttributeError):
        return "latest"
    return value if value in ("latest", "stable") else "latest"


def discover(cfg: Config, runner: Runner, *, fetch: FetchJson = http_json,
             path_env: Optional[str] = None, ls_remote=None, sleep=time.sleep) -> dict:
    out = {"hermes": hermes_version(cfg, runner, ls_remote=ls_remote)}
    for p in ("claude", "codex", "opencode"):
        out[p] = cli_version(p, cfg, runner, fetch, path_env=path_env, sleep=sleep)
    return out
