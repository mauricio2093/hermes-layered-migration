"""Backup diario (script REAL) + verificacion independiente y sus fallos."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

from hermes_updater import backup, verify
from hermes_updater.status import Verdict


def _daily(world):
    from hermes_updater.config import ensure_dirs
    ensure_dirs(world.cfg)
    return backup.run_backup_a(world.cfg, world.runner)


def test_backup_diario_restaurable_y_verificado_por_dos_vias(world):
    b = _daily(world)
    assert b.verdict == Verdict.PASS, b.detail
    assert b.script_claim is True
    checks = b.verification["checks"]
    for name in ("manifest", "archive", "db_inventory", "restore_databases", "restore_content",
                 "restore_permissions", "restore_critical_paths"):
        assert checks[name]["verdict"] == "PASS", (name, checks[name])
    # las 10 bases restauradas en aislado con quick_check e integrity_check
    dbs = checks["restore_databases"]["databases"]
    assert len(dbs) == 10 and all(d["quick_check"] == "ok" and d["integrity_check"] == "ok" for d in dbs.values())
    # la evidencia queda atada al MANIFEST de ESE backup
    ev = json.loads(Path(b.evidence_path).read_text())
    assert ev["manifest_sha256"] == verify.sha256_file(Path(b.backup_dir) / "MANIFEST.sha256")
    # y hermes-maint (backup-freshness) lo aceptaria: state.json con los 5 flags
    st = json.loads((Path(b.backup_dir) / "state.json").read_text())
    assert st["backup_verified"] is True and all(st["backup"].values())
    # nada restaurado queda en disco
    assert not list((world.cfg.work_dir).glob("restore-*"))


def test_backup_corrupto_se_detecta(world):
    b = _daily(world)
    arch = next(Path(b.backup_dir).glob("hermes-home-*.tar.gz"))
    data = bytearray(arch.read_bytes())
    data[len(data) // 2] ^= 0xFF
    arch.write_bytes(bytes(data))
    rep = verify.verify_backup(Path(b.backup_dir), hermes_home=world.home, work_dir=world.cfg.work_dir)
    assert rep.verdict == Verdict.FAIL
    assert rep.checks["manifest"]["verdict"] == "FAIL"


def test_backup_corrupto_aunque_el_manifest_se_recalcule(world):
    """Si alguien rehace el MANIFEST tras corromper el archivo, el archivo
    sigue sin poder leerse entero."""
    b = _daily(world)
    d = Path(b.backup_dir)
    arch = next(d.glob("hermes-home-*.tar.gz"))
    arch.write_bytes(arch.read_bytes()[: arch.stat().st_size // 2])
    backup.write_manifest(d)
    rep = verify.verify_backup(d, hermes_home=world.home, work_dir=world.cfg.work_dir)
    assert rep.checks["manifest"]["verdict"] == "PASS"
    assert rep.checks["archive"]["verdict"] == "FAIL"


def test_backup_incompleto_se_detecta(world):
    b = _daily(world)
    (Path(b.backup_dir) / "db-consistent" / "kanban.db").unlink()
    rep = verify.verify_backup(Path(b.backup_dir), hermes_home=world.home, work_dir=world.cfg.work_dir)
    assert rep.verdict == Verdict.FAIL
    assert rep.checks["db_inventory"]["verdict"] == "FAIL"
    assert "kanban.db" in rep.checks["db_inventory"]["detail"]


def test_copia_borrada_con_manifest_rehecho_sigue_detectandose(world):
    """Aunque alguien rehaga el MANIFEST, el inventario del vivo no cuadra."""
    b = _daily(world)
    d = Path(b.backup_dir)
    (d / "db-consistent" / "kanban.db").unlink()
    backup.write_manifest(d)
    rep = verify.verify_backup(d, hermes_home=world.home, work_dir=world.cfg.work_dir)
    assert rep.checks["manifest"]["verdict"] == "PASS"
    assert rep.checks["db_inventory"]["verdict"] == "FAIL"
    assert rep.verdict == Verdict.FAIL


def test_base_nueva_no_inventariada_bloquea(world):
    c = sqlite3.connect(world.home / "hermes-control" / "nueva.db")
    c.execute("create table t(x)")
    c.commit()
    c.close()
    b = _daily(world)
    assert b.verdict == Verdict.FAIL
    assert b.script_claim is False                     # el script la detecta
    assert "nueva" in b.detail                          # y el verificador tambien
    assert b.verification["checks"]["db_inventory"]["verdict"] == "FAIL"


def test_plugin_no_declarado_bloquea(world):
    (world.home / "plugins" / "orca-status").mkdir()
    (world.home / "plugins" / "orca-status" / "plugin.yaml").write_text("name: orca\n")
    b = _daily(world)
    assert b.verdict == Verdict.FAIL
    assert "no declarado: orca-status" in b.detail


def test_permisos_de_secretos_alterados(world):
    b = _daily(world)
    d = Path(b.backup_dir)
    (d / "perms-esperados.txt").write_text("644 .env\n")
    backup.write_manifest(d)
    rep = verify.verify_backup(d, hermes_home=world.home, work_dir=world.cfg.work_dir)
    assert rep.checks["restore_permissions"]["verdict"] == "FAIL"


def test_symlink_que_escapa_se_rechaza():
    import tarfile
    m = tarfile.TarInfo(".hermes/x")
    m.type, m.linkname = tarfile.SYMTYPE, "../../etc/passwd"
    assert verify._safe_member(m)
    m.linkname = "/usr/bin/python3"         # absoluto: legitimo (venvs), no se sigue
    assert verify._safe_member(m) is None
    m.linkname = "../h/hp2621"               # relativo dentro del archivo
    m.name = ".hermes/terminfo/2/2621"
    assert verify._safe_member(m) is None


def test_retencion_nunca_borra_el_ultimo_verificado_ni_ajenos(world, tmp_path):
    root = tmp_path / "ret"
    for name, ok in (("20260101-000000", True), ("20260102-000000", True), ("20260103-000000", False),
                     ("20260104-000000", True), ("20260105-000000", False), ("20250101-000000", True)):
        d = root / name
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"backup_verified": ok, "backup": {"a": ok}}))
    owned = {"20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000", "20260105-000000"}
    removed = backup.prune(root, owned, keep=1)
    left = {p.name for p in root.iterdir()}
    assert "20260104-000000" in left           # ultimo verificado
    assert "20250101-000000" in left           # ajeno
    assert "20260105-000000" in left           # fallo posterior al ultimo bueno: evidencia
    assert set(removed) == {"20260101-000000", "20260102-000000", "20260103-000000"}


def test_preupdate_completo_y_reverificable(world):
    from hermes_updater.config import ensure_dirs
    ensure_dirs(world.cfg)
    b = backup.run_preupdate(world.cfg, world.runner, upstream_ref=world.base, versions={"x": 1})
    assert b.verdict == Verdict.PASS, b.detail
    d = Path(b.bundle_dir)
    for f in ("refs.txt", "carried.bundle", "venv-freeze.txt", "crontab.txt", "tool-versions.json",
              "hermes-backup-B.zip", "MANIFEST.sha256", "backup-a.txt"):
        assert (d / f).exists(), f
    assert b.parts["code"]["bundle"]["verified"] is True
    assert b.parts["code"]["patches"] == 2
    assert "hermes-gateway.service.d/10-legacy-venv-launch.conf" in b.parts["code"]["systemd"]
    assert backup.verify_preupdate_still_valid(d)[0]
    (d / "refs.txt").write_text("manipulado")
    assert not backup.verify_preupdate_still_valid(d)[0]


def test_preupdate_falla_si_produccion_tiene_cambios_sin_commit(world):
    from hermes_updater.config import ensure_dirs
    ensure_dirs(world.cfg)
    (world.prod / "app.py").write_text("tocado a mano\n")
    b = backup.run_preupdate(world.cfg, world.runner, upstream_ref=world.base, versions={})
    assert b.verdict == Verdict.FAIL
    assert "sin commit" in b.detail
