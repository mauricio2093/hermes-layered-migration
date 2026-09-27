"""backup-hermes-home.sh: plugins/ y scripts/ por nombre y con manifiesto.

Se ejecuta el script REAL contra un HERMES_HOME falso con la misma forma que el
de produccion. Nunca toca ~/.hermes.
"""

import json
import pathlib
import sqlite3
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "backup-hermes-home.sh"

PLUGINS = ["hostinger-commands", "model-providers/onion", "voicestudio-tts"]
SCRIPTS = ["backup-hermes-home.sh", "cutover.sh", "rollback.sh", "alerta-a.sh", "alerta-b.sh"]
DBS = ["state.db", "verification_evidence.db", "kanban.db", "shared-state.db",
       "cron/executions.db", "cron/deliveries.db", "cron/notepad.db", "anchors/data/phone.db"]


def hermes_home(tmp_path) -> pathlib.Path:
    h = tmp_path / ".hermes"
    for p in PLUGINS:
        (h / "plugins" / p).mkdir(parents=True)
        (h / "plugins" / p / "plugin.yaml").write_text(f"name: {p}\n")
        (h / "plugins" / p / "__init__.py").write_text("def register(ctx): pass\n")
    (h / "scripts").mkdir()
    for s in SCRIPTS:
        (h / "scripts" / s).write_text(f"# {s}\n")
    for d, n in (("patches", 6), ("anchors", 27), ("voice-samples", 7)):
        (h / d).mkdir(exist_ok=True)
        for i in range(n):
            (h / d / f"f{i}").write_text(str(i))
    for db in DBS:
        (h / db).parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(h / db).execute("create table t(x)").connection.commit()
    (h / "backup-declarations.sh").write_text(
        "DECL_PLUGINS=(" + " ".join(PLUGINS) + ")\n"
        "DECL_SCRIPTS=(" + " ".join(SCRIPTS) + ")\n")
    for f in (".env", "auth.json", "config.yaml", "channel_directory.json"):
        (h / f).write_text("x")
        (h / f).chmod(0o600)
    return h


def backup(h):
    r = subprocess.run(["bash", str(SCRIPT)], env={"HERMES_HOME": str(h), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=180)
    dest = sorted((h / "backups" / "independiente").iterdir())[-1]
    state = json.loads((dest / "state.json").read_text())
    return r, state, dest


def test_todo_declarado_es_un_backup_verificado(tmp_path):
    r, state, dest = backup(hermes_home(tmp_path))
    assert state["backup_verified"] is True, r.stdout[-3000:]
    assert "✓ plugins" in r.stdout and "✓ scripts" in r.stdout
    assert "sha256 identico" in r.stdout
    assert (dest / "content-manifest.sha256").exists()
    # El esquema de state.json no cambia: hermes-maint lo lee.
    assert set(state["backup"]) == {"created", "archive_integrity", "database_integrity",
                                    "manifest_integrity", "restore_verified"}


def test_un_plugin_no_declarado_aborta_y_se_nombra(tmp_path):
    h = hermes_home(tmp_path)
    (h / "plugins" / "hermes_control").mkdir()
    (h / "plugins" / "hermes_control" / "plugin.yaml").write_text("name: x\n")
    r, state, _ = backup(h)
    assert state["backup_verified"] is False
    assert "no declarado: hermes_control" in r.stdout


def test_un_intercambio_con_el_mismo_recuento_se_detecta(tmp_path):
    """El caso que el recuento dejaba pasar: se pierde uno, aparece otro."""
    h = hermes_home(tmp_path)
    (h / "scripts" / "rollback.sh").unlink()
    (h / "scripts" / "otro.sh").write_text("#\n")
    r, state, _ = backup(h)
    assert state["backup_verified"] is False
    assert "falta:        rollback.sh" in r.stdout
    assert "no declarado: otro.sh" in r.stdout


def test_lo_que_el_tar_no_trae_lo_detecta_el_manifiesto(tmp_path):
    """Un fichero que el tar excluye (*.db) dentro de un plugin: el recuento
    no lo sabia; el manifiesto si."""
    h = hermes_home(tmp_path)
    (h / "plugins" / "voicestudio-tts" / "cache.db").write_text("no soy sqlite")
    r, state, _ = backup(h)
    assert state["backup_verified"] is False
    assert "no coincide con el manifiesto" in r.stdout


def test_los_ficheros_que_el_tar_excluye_a_proposito_no_cuentan(tmp_path):
    h = hermes_home(tmp_path)
    pc = h / "plugins" / "voicestudio-tts" / "__pycache__"
    pc.mkdir()
    (pc / "x.cpython-311.pyc").write_bytes(b"\0")
    _, state, _ = backup(h)
    assert state["backup_verified"] is True


def test_sin_fichero_de_declaraciones_no_hay_backup_verificado(tmp_path):
    h = hermes_home(tmp_path)
    (h / "backup-declarations.sh").unlink()
    r, state, _ = backup(h)
    assert state["backup_verified"] is False
    assert "falta" in r.stdout and "backup-declarations.sh" in r.stdout
