"""backup-hermes-home.sh: declaraciones por nombre, inventario SQLite por
contenido, estado del host, y fallo SEGURO ante lo no declarado.

Se ejecuta el script REAL contra un HERMES_HOME falso con la misma forma que el
de produccion, con su propio checkout y su propio directorio de units. Nunca
toca ~/.hermes.
"""

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backup-hermes-home.sh"

PLUGINS = ["hostinger-commands", "model-providers/onion", "voicestudio-tts"]
SCRIPTS = ["backup-hermes-home.sh", "cutover.sh", "rollback.sh", "alerta-a.sh", "alerta-b.sh"]
PATCHES = ["registry.json", "bin/save-patch.sh", "a.patch"]
ANCHORS = ["INDEX.md", "bin/tool", ".last-refresh"]
VOICE = ["ref.wav", "ref.txt"]
DBS = ["state.db", "verification_evidence.db", "kanban.db", "shared-state.db",
       "cron/executions.db", "cron/deliveries.db", "cron/notepad.db", "anchors/data/phone.db",
       "costs/prices.db", "hermes-control/gifs.db"]
COLD = ["state.db.bak-3504"]
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def declarations(**over) -> str:
    lists = {"DECL_PLUGINS": PLUGINS, "DECL_SCRIPTS": SCRIPTS, "DECL_PATCHES": PATCHES,
             "DECL_ANCHORS": ANCHORS, "DECL_VOICE": VOICE, "DECL_SQLITE_COLD": COLD}
    lists.update(over)
    return "".join(f"{k}=({' '.join(v)})\n" for k, v in lists.items() if v is not None)


def sqlite(path, wal=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("create table t(x)")
    c.execute("insert into t values (1)")
    c.commit()
    c.close()


def world(tmp_path):
    h = tmp_path / ".hermes"
    for p in PLUGINS:
        (h / "plugins" / p).mkdir(parents=True)
        (h / "plugins" / p / "plugin.yaml").write_text(f"name: {p}\n")
        (h / "plugins" / p / "__init__.py").write_text("def register(ctx): pass\n")
    (h / "scripts").mkdir()
    for s in SCRIPTS:
        (h / "scripts" / s).write_text(f"# {s}\n")
    for d, names in (("patches", PATCHES), ("anchors", ANCHORS), ("voice-samples", VOICE)):
        for n in names:
            (h / d / n).parent.mkdir(parents=True, exist_ok=True)
            (h / d / n).write_text(n)
    for db in DBS:
        sqlite(h / db)
    sqlite(h / COLD[0], wal=True)              # archivada, en modo WAL como la real
    (h / "backup-declarations.sh").write_text(declarations())
    for f in (".env", "auth.json", "config.yaml", "channel_directory.json"):
        (h / f).write_text("x")
        (h / f).chmod(0o600)
    (h / "SOUL.md").write_text("soul\n")
    for d in ("memories", "skills"):
        (h / d).mkdir()
        (h / d / "x").write_text("x")
    # checkout productivo: upstream + 2 commits propios
    prod = tmp_path / "prod"
    prod.mkdir()
    _git(prod, "init", "-q", "-b", "main")
    (prod / "a.py").write_text("a\n")
    _git(prod, "add", ".")
    _git(prod, "commit", "-q", "-m", "upstream")
    _git(prod, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(prod, "checkout", "-q", "-b", "feat/layer2-schema")
    for i in range(2):
        (prod / f"l2_{i}.py").write_text(str(i))
        _git(prod, "add", ".")
        _git(prod, "commit", "-q", "-m", f"propio {i}")
    units = tmp_path / "systemd"
    (units / "hermes-gateway.service.d").mkdir(parents=True)
    (units / "hermes-gateway.service").write_text("[Service]\nExecStart=/x\n")
    (units / "hermes-gateway.service.d" / "10-legacy-venv-launch.conf").write_text("[Service]\nExecStart=\nExecStart=/y\n")
    return h, prod, units


def backup(h, prod, units, **env):
    e = {"HERMES_HOME": str(h), "PATH": "/usr/bin:/bin", "HOME": str(h.parent),
         "HERMES_PROD_REPO": str(prod), "HERMES_SYSTEMD_USER_DIR": str(units),
         "HERMES_PROD_PYTHON": sys.executable, "HERMES_EXTRA_REPOS": str(prod), **GIT_ENV, **env}
    r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=300)
    dest = sorted((h / "backups" / "independiente").iterdir())[-1]
    state = json.loads((dest / "state.json").read_text())
    return r, state, dest


@pytest.fixture
def w(tmp_path):
    return world(tmp_path)


def test_todo_declarado_es_un_backup_verificado(w):
    r, state, dest = backup(*w)
    assert state["backup_verified"] is True, r.stdout[-4000:]
    for n in ("plugins", "scripts", "patches", "anchors", "voice-samples"):
        assert f"✓ {n}" in r.stdout
    assert "sha256 identico" in r.stdout
    assert "11 bases (10 vivas, 1 archivadas)" in r.stdout
    # hermes-maint lee estos cinco; host_state se suma y entra en backup_verified
    assert {"created", "archive_integrity", "database_integrity", "manifest_integrity",
            "restore_verified"} <= set(state["backup"])
    assert state["backup"]["host_state"] is True
    host = dest / "host"
    for f in ("carried.bundle", "prod-refs.txt", "venv-freeze.txt", "crontab.txt", "launchers.txt",
              "systemd/hermes-gateway.service", "systemd/hermes-gateway.service.d/10-legacy-venv-launch.conf"):
        assert (host / f).exists(), f
    assert "commits propios: 2 en carried.bundle" in r.stdout


def test_la_base_archivada_no_se_toca_ni_se_le_crean_ficheros(w):
    h = w[0]
    for sfx in ("-wal", "-shm"):
        p = pathlib.Path(str(h / COLD[0]) + sfx)
        if p.exists():
            p.unlink()
    before = (h / COLD[0]).read_bytes()
    r, state, _ = backup(*w)
    assert state["backup_verified"] is True, r.stdout[-3000:]
    assert (h / COLD[0]).read_bytes() == before
    assert not pathlib.Path(str(h / COLD[0]) + "-wal").exists()
    assert not pathlib.Path(str(h / COLD[0]) + "-shm").exists()


@pytest.mark.parametrize("rel", ["anchors/NUEVO.md", "patches/otro.patch", "voice-samples/x.wav"])
def test_un_fichero_nuevo_no_declarado_aborta_y_se_nombra(w, rel):
    (w[0] / rel).write_text("nuevo")
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert f"no declarado: {rel.split('/', 1)[1]}" in r.stdout


def test_un_declarado_que_desaparece_aborta(w):
    (w[0] / "anchors" / "INDEX.md").unlink()
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "falta:        INDEX.md" in r.stdout


@pytest.mark.parametrize("lista", ["DECL_ANCHORS", "DECL_PATCHES", "DECL_VOICE", "DECL_SQLITE_COLD"])
def test_una_lista_ausente_no_cuenta_como_vacia(w, lista):
    (w[0] / "backup-declarations.sh").write_text(declarations(**{lista: None}))
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert f"no define {lista}" in r.stdout


@pytest.mark.parametrize("rel", ["costs/nueva.sqlite", "hermes-control/cache.sqlite3", "notas.bin",
                                 "hermes-agent/rescate.db", "state.db.bak-9999"])
def test_cualquier_base_sqlite_no_declarada_aborta(w, rel):
    """Por contenido, no por nombre, y dentro de todo lo que cubre el tar
    (hermes-agent/ incluido, que antes se saltaba)."""
    sqlite(w[0] / rel)
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert f"base SQLite NO declarada: {rel}" in r.stdout


def test_lo_excluido_del_tar_tampoco_se_inventaria(w):
    sqlite(w[0] / "cache" / "regenerable.db")
    sqlite(w[0] / "hermes-agent" / "venv" / "lib.db")
    _, state, _ = backup(*w)
    assert state["backup_verified"] is True


def test_archivada_con_escritor_activo_aborta(w):
    pathlib.Path(str(w[0] / COLD[0]) + "-wal").write_bytes(b"x" * 64)
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "WAL con datos o journal" in r.stdout


def test_archivada_declarada_que_no_existe_aborta(w):
    (w[0] / "backup-declarations.sh").write_text(declarations(DECL_SQLITE_COLD=COLD + ["fantasma.db.bak"]))
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "no existe: fantasma.db.bak" in r.stdout


def test_un_plugin_no_declarado_aborta_y_se_nombra(w):
    h = w[0]
    (h / "plugins" / "hermes_control").mkdir()
    (h / "plugins" / "hermes_control" / "plugin.yaml").write_text("name: x\n")
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "no declarado: hermes_control" in r.stdout


def test_un_intercambio_con_el_mismo_recuento_se_detecta(w):
    """El caso que el recuento dejaba pasar: se pierde uno, aparece otro."""
    h = w[0]
    (h / "scripts" / "rollback.sh").unlink()
    (h / "scripts" / "otro.sh").write_text("#\n")
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "falta:        rollback.sh" in r.stdout
    assert "no declarado: otro.sh" in r.stdout


def test_un_fichero_db_que_no_es_base_no_desaparece_en_silencio(w):
    """El tar excluye *.db. Un 'cache.db' que NO es SQLite dentro de un plugin
    no puede caerse sin aviso: el manifiesto lo lleva y el restore no lo ve."""
    (w[0] / "plugins" / "voicestudio-tts" / "cache.db").write_text("no soy sqlite")
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "no coincide con el manifiesto" in r.stdout


def test_los_ficheros_que_el_tar_excluye_a_proposito_no_cuentan(w):
    pc = w[0] / "plugins" / "voicestudio-tts" / "__pycache__"
    pc.mkdir()
    (pc / "x.cpython-311.pyc").write_bytes(b"\0")
    _, state, _ = backup(*w)
    assert state["backup_verified"] is True


def test_sin_fichero_de_declaraciones_no_hay_backup_verificado(w):
    (w[0] / "backup-declarations.sh").unlink()
    r, state, _ = backup(*w)
    assert state["backup_verified"] is False
    assert "falta" in r.stdout and "backup-declarations.sh" in r.stdout


def test_sin_checkout_productivo_no_hay_backup_verificado(w, tmp_path):
    h, _, units = w
    r, state, _ = backup(h, tmp_path / "no-existe", units)
    assert state["backup"]["host_state"] is False and state["backup_verified"] is False
    assert "no hay checkout productivo" in r.stdout


def test_sin_unit_del_gateway_no_hay_backup_verificado(w, tmp_path):
    h, prod, _ = w
    r, state, _ = backup(h, prod, tmp_path / "sin-units")
    assert state["backup"]["host_state"] is False


def test_sin_origin_main_el_bundle_no_se_inventa(w):
    h, prod, units = w
    _git(prod, "update-ref", "-d", "refs/remotes/origin/main")
    r, state, _ = backup(h, prod, units)
    assert state["backup"]["host_state"] is False
    assert "no se puede acotar el bundle" in r.stdout


def test_la_cobertura_del_script_y_del_verificador_coincide():
    sys.path.insert(0, str(ROOT / "updater"))
    from hermes_updater import verify
    text = SCRIPT.read_text()
    top = re.search(r"^EXCLUDE_TOP=\(([^)]*)\)", text, re.M).group(1).split()
    sub = re.search(r"^EXCLUDE_SUB=\(([^)]*)\)", text, re.M).group(1).split()
    assert tuple(top) == verify.EXCLUDE_TOP and tuple(sub) == verify.EXCLUDE_SUB
    dirs = re.search(r"^CONTENT_DIRS=\(([^)]*)\)", text, re.M).group(1).split()
    assert tuple(dirs) == verify.CONTENT_DIRS


def test_el_verificador_independiente_lo_confirma_y_restaura(w, tmp_path):
    sys.path.insert(0, str(ROOT / "updater"))
    from hermes_updater import verify
    h, prod, units = w
    _, state, dest = backup(h, prod, units)
    rep = verify.verify_backup(dest, hermes_home=h, work_dir=tmp_path / "work", prod_repo=prod)
    assert rep.verdict.value == "PASS", {k: v for k, v in rep.checks.items() if v["verdict"] != "PASS"}
    dbs = rep.checks["restore_databases"]["databases"]
    assert dbs[COLD[0]]["kind"] == "archivada" and dbs[COLD[0]]["integrity_check"] == "ok"
    assert len(dbs) == 11
    # y si alguien quita la base archivada del archivo, el verificador lo ve
    sqlite(h / "intrusa.sqlite")
    rep2 = verify.verify_backup(dest, hermes_home=h, work_dir=tmp_path / "work", prod_repo=prod)
    assert rep2.checks["db_inventory"]["verdict"] == "FAIL" and "intrusa.sqlite" in rep2.checks["db_inventory"]["detail"]
