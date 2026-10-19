"""Mundo aislado para probar hermes-updater sin tocar nada real.

  upstream/     repo git con main etiquetado (CalVer v2026.1.1 -> 0.1.0) y
                releases v0.1.0 y v0.2.0 (como NousResearch: semver en tags)
  prod/         clon en la rama feat/layer2-schema con 2 commits propios
                (Capa 2), refs/remotes/origin/main y un .venv falso
  home/.hermes  HERMES_HOME con la forma exacta que exige el script de
                backup REAL (10 bases, plugins/scripts declarados, conteos)
  FakeService   gateway simulado (activo / no arranca / no se recupera)
  ScriptedRunner  intercepta pip, hermes_cli, CLIs; git, cp y bash son reales
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_updater import config  # noqa: E402
from hermes_updater.proc import Result, Runner  # noqa: E402

SCRIPT = ROOT.parent / "scripts" / "backup-hermes-home.sh"
DBS = ["state.db", "verification_evidence.db", "kanban.db", "shared-state.db", "cron/executions.db",
       "cron/deliveries.db", "cron/notepad.db", "anchors/data/phone.db", "costs/prices.db", "hermes-control/gifs.db"]
PLUGINS = ["hermes_control", "voicestudio-tts"]
SCRIPTS = ["backup-hermes-home.sh", "rollback.sh"]
ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
      "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={**os.environ, **ID})
    if check and r.returncode:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def commit(repo, files: dict, msg: str) -> str:
    for name, text in files.items():
        p = Path(repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def pyproject(version: str, deps=("requests>=2",)) -> str:
    d = ", ".join(f'"{x}"' for x in deps)
    return (f'[project]\nname = "hermes-agent"\nversion = "{version}"\ndependencies = [{d}]\n'
            f'[project.optional-dependencies]\ntelegram = ["python-telegram-bot==22"]\n')


def make_hermes_home(base: Path) -> Path:
    h = base / ".hermes"
    for p in PLUGINS:
        (h / "plugins" / p).mkdir(parents=True)
        (h / "plugins" / p / "plugin.yaml").write_text(f"name: {p}\n")
    (h / "scripts").mkdir()
    for s in SCRIPTS:
        (h / "scripts" / s).write_text(f"# {s}\n")
    for d, n in (("patches", 6), ("anchors", 27), ("voice-samples", 7)):
        (h / d).mkdir(exist_ok=True)
        for i in range(n):
            (h / d / f"f{i}").write_text(str(i))
    for db in DBS:
        (h / db).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(h / db)
        if db == "state.db":
            c.executescript("create table schema_version(version int); insert into schema_version values (31);"
                            "create table sessions(id); create table messages(id);"
                            "insert into sessions values (1),(2); insert into messages values (1),(2),(3);")
        else:
            c.execute("create table t(x)")
        c.commit()
        c.close()
    lists = {"DECL_PLUGINS": PLUGINS, "DECL_SCRIPTS": SCRIPTS,
             "DECL_PATCHES": [f"f{i}" for i in range(6)], "DECL_ANCHORS": [f"f{i}" for i in range(27)],
             "DECL_VOICE": [f"f{i}" for i in range(7)], "DECL_SQLITE_COLD": []}
    (h / "backup-declarations.sh").write_text("".join(f"{k}=({' '.join(v)})\n" for k, v in lists.items()))
    for f in (".env", "auth.json", "config.yaml", "channel_directory.json"):
        (h / f).write_text("FAKE_TOKEN=no-es-real\n" if f == ".env" else "{}\n")
        os.chmod(h / f, 0o600)
    (h / "SOUL.md").write_text("soul\n")
    for d in ("memories", "skills", "cron/jobs"):
        (h / d).mkdir(parents=True, exist_ok=True)
        (h / d / "x").write_text("x")
    return h


class FakeService:
    """Gateway simulado. `fail_restart`: el reinicio tras el despliegue no
    levanta. `fail_after_rollback`: tampoco tras el rollback (incidente)."""

    def __init__(self, active=True):
        self.active, self.pid, self.nrestarts = active, 100, 0
        self.restarts_called = 0
        self.fail_restart = False
        self.fail_after_rollback = False
        self.exec = "ExecStart=/x/.venv/bin/python hermes_cli/main.py gateway run"
        self.log = "gateway started\ntelegram connected\n"
        self.unit = "hermes-gateway.service"

    def is_active(self):
        return self.active

    def main_pid(self):
        return self.pid if self.active else 0

    def restarts(self):
        return self.nrestarts

    def exec_start(self):
        # como systemctl show: ruta + argv, y datos de ejecucion que cambian
        # en cada reinicio (start_time, pid)
        return (f"{{ path={self.exec.split()[0].split('=', 1)[1]} ; argv[]={self.exec.split('=', 1)[1]} ; "
                f"ignore_errors=no ; start_time=[{self.restarts_called}] ; pid={self.main_pid()} ; status=0/0 }}")

    def restart(self):
        self.restarts_called += 1
        fail = self.fail_after_rollback if self.restarts_called > 1 else self.fail_restart
        self.active = not fail
        self.pid += 1
        return True

    def daemon_reload(self):
        return True

    def journal_since(self, ts):
        return self.log


class ScriptedRunner(Runner):
    """git/cp/bash/python3 de verdad; el resto, guionizado y registrado."""

    def __init__(self, world):
        self.world = world
        self.calls = []
        self.fail_pip_install = False
        self.version_report = "0.2.0"
        self.sent = []
        self.aux = {}          # producto -> dict(version, update_rc, after, smoke_ok)

    def run(self, argv, *, timeout, cwd=None, env=None, stdin=None, keep=65536):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        joined = " ".join(argv)
        ok = lambda out="": Result(tuple(argv), 0, out, 0.01)
        bad = lambda out="", code=1: Result(tuple(argv), code, out, 0.01)
        if argv[0] == "crontab":
            return bad("no crontab for t")
        if "default_environment" in joined:
            from packaging.markers import default_environment
            return ok(json.dumps(default_environment()))
        if "-m pip" in joined:
            if " freeze" in joined:
                return ok("requests==2.31.0\npython-telegram-bot==22.0\n")
            if " check" in joined:
                return ok("No broken requirements found.")
            if " install" in joined:
                return bad("ERROR: Could not find a version") if self.fail_pip_install else ok()
        if "hermes_cli/main.py" in joined:
            sub = argv[argv.index(next(a for a in argv if a.endswith("hermes_cli/main.py"))) + 1]
            if sub == "send":
                self.sent.append(stdin)
                return ok()
            if sub == "plugins":
                return ok("hermes_control   enabled   0.19.1\n")
            if sub == "config":
                return ok("config ok")
            if sub == "backup":
                out = argv[argv.index("-o") + 1]
                with zipfile.ZipFile(out, "w") as z:
                    z.writestr(".hermes/config.yaml", "{}")
                    z.writestr(".hermes/state.db", "x")
                return ok()
        if "get_version_info" in joined:
            return ok(self.version_report + "\n")
        name = Path(argv[0]).name
        if name in self.aux:
            st = self.aux[name]
            if argv[1:] == ["--version"]:
                return ok(f"{st['version']} ({name})") if st.get("smoke_ok", True) or st["version"] == st["before"] else bad()
            if argv[1:] == ["--help"]:
                return ok("help") if st.get("smoke_ok", True) else bad("crash")
            # el comando de actualizacion
            st["version"] = st.get("after", st["version"])
            return ok() if st.get("update_rc", 0) == 0 else bad("update failed")
        return super().run(argv, timeout=timeout, cwd=cwd, env=env, stdin=stdin, keep=keep)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_UPDATER_NO_SCOPE", "1")
    # el script de backup registra el estado del host: en los tests, el del
    # mundo aislado (python con pip, repos y HOME del fixture), nunca el real
    shim = tmp_path / "prod-python"
    shim.write_text("#!/bin/sh\n"
                    'if [ "$1" = "-m" ] && [ "$2" = "pip" ] && [ "$3" = "freeze" ]; then\n'
                    "  printf 'requests==2.31.0\\npython-telegram-bot==22.0\\n'; exit 0\nfi\n"
                    f'exec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    # pip freeze fijo, el resto delegado: no depende de que el Python de pytest
    # traiga pip (el del sistema no lo trae).
    monkeypatch.setenv("HERMES_PROD_PYTHON", str(shim))
    monkeypatch.setenv("HERMES_EXTRA_REPOS", str(tmp_path / "prod"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for k, v in ID.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    commit(up, {"pyproject.toml": pyproject("0.1.0"), "app.py": "A = 1\nB = 1\n",
                "hermes_state_common.py": "SCHEMA_VERSION = 31\n",
                "hermes_cli/config_defaults.py": '"_config_version": 49,\n'}, "base")
    git(up, "tag", "-a", "v2026.1.1", "-m", "calver")
    base = git(up, "rev-parse", "HEAD")
    git(up, "tag", "-a", "v0.1.0", "-m", "release")

    prod = tmp_path / "prod"
    subprocess.run(["git", "clone", "-q", str(up), str(prod)], check=True, env={**os.environ, **ID})
    git(prod, "checkout", "-q", "-b", "feat/layer2-schema")
    commit(prod, {"hermes_layer2/menu.py": "MENU = 1\n"}, "feat(layer2): menu")
    commit(prod, {"tests/layer2/test_menu.py": "def test_ok():\n    assert True\n"}, "test(layer2): menu")
    (prod / ".venv" / "bin").mkdir(parents=True)
    (prod / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(prod / ".venv" / "bin" / "python", 0o755)
    (prod / ".git" / "info" / "exclude").write_text(".venv/\n")

    # upstream avanza y publica v0.2.0
    commit(up, {"pyproject.toml": pyproject("0.2.0"), "app.py": "A = 2\nB = 1\n",
                "hermes_cli/config_defaults.py": '"_config_version": 50,\n'}, "release 0.2.0 work")
    git(up, "tag", "-a", "v0.2.0", "-m", "release 0.2.0")

    home = make_hermes_home(tmp_path / "home")
    sysd = tmp_path / "systemd"
    (sysd / "hermes-gateway.service.d").mkdir(parents=True)
    (sysd / "hermes-gateway.service").write_text("[Service]\nExecStart=/x\n")
    (sysd / "hermes-gateway.service.d" / "10-legacy-venv-launch.conf").write_text("[Service]\nExecStart=\nExecStart=/y\n")
    approvals = tmp_path / "approvals"
    approvals.mkdir(mode=0o755)

    cfg = config.load(Path("/nonexistent"), hermes_home=str(home), prod_repo=str(prod), upstream_url=str(up),
                      state_dir=str(tmp_path / "state"), backup_script=str(SCRIPT),
                      daily_backups=str(home / "backups" / "independiente"),
                      preupdate_backups=str(home / "backups" / "pre-update"),
                      systemd_user_dir=str(sysd), approvals_dir=str(approvals),
                      approvals_owner_uid=os.getuid(), hermes_control_repo=str(tmp_path / "no-hc"),
                      policy={"min_free_disk_gb": 0.01, "min_avail_mem_mb": 1, "net_retries": 2,
                              "net_backoff_s": 0, "notify_transport": "file", "ai_provider": "none",
                              "service_ready_timeout_s": 3, "service_stable_s": 1, "notify_retries": 1})

    class W:
        pass
    w = W()
    w.tmp, w.up, w.prod, w.home, w.cfg, w.base, w.approvals, w.sysd = tmp_path, up, prod, home, cfg, base, approvals, sysd
    w.svc = FakeService()
    w.runner = ScriptedRunner(w)
    return w


def fake_fetch(versions: dict):
    def fetch(url):
        for pkg, v in versions.items():
            if url.endswith(f"/{pkg}/dist-tags"):
                if isinstance(v, Exception):
                    raise v
                return {"latest": v}
        raise OSError("unexpected url " + url)
    return fetch


def make_cli(tmp: Path, name: str, version: str, w=None) -> Path:
    """Binario falso con la forma de instalacion real (para install_method)."""
    layout = {"claude": tmp / "h/.local/share/claude/versions" / version,
              "codex": tmp / "h/.codex/packages/standalone/releases" / version / "bin/codex",
              "opencode": tmp / "h/.opencode/bin/opencode"}[name]
    layout.parent.mkdir(parents=True, exist_ok=True)
    layout.write_text(f"#!/bin/sh\necho '{version} ({name})'\n")
    os.chmod(layout, 0o755)
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    link = bindir / name
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(layout)
    if w is not None:
        w.runner.aux[str(link)] = {"version": version, "before": version}
        w.runner.aux[name] = w.runner.aux[str(link)]
    return bindir
