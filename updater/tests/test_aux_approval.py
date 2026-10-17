"""Actualizaciones auxiliares y autorizacion."""

import json
import os
import time
from pathlib import Path

from conftest import fake_fetch, make_cli

from hermes_updater import approval, auxiliary, versions
from hermes_updater.status import Verdict


def _pv(world, tmp_path, name, before, available):
    bindir = make_cli(tmp_path, name, before, world)
    fetch = fake_fetch({versions.NPM_PACKAGES[name]: available})
    pv = versions.cli_version(name, world.cfg, world.runner, fetch, path_env=str(bindir), sleep=lambda s: None)
    return pv, bindir


def _ver(world, name):
    return lambda: world.runner.aux[name]["version"]


def test_claude_se_actualiza_con_su_mecanismo_y_se_comprueba(world, tmp_path):
    pv, _ = _pv(world, tmp_path, "claude", "2.1.0", "2.1.5")
    world.runner.aux["claude"]["after"] = "2.1.5"
    r = auxiliary.update_one(world.cfg, world.runner, pv, version_of=_ver(world, "claude"), in_use=lambda p: 3)
    assert r.verdict == Verdict.PASS, r.detail
    assert (r.before, r.after) == ("2.1.0", "2.1.5")
    upd = [c for c in world.runner.calls if c[-1] == "update"]
    assert upd and upd[0][0].endswith("/claude")


def test_no_actualiza_claude_desde_claude(world, tmp_path, monkeypatch):
    pv, _ = _pv(world, tmp_path, "claude", "2.1.0", "2.1.5")
    monkeypatch.setenv("CLAUDECODE", "1")
    r = auxiliary.update_one(world.cfg, world.runner, pv, version_of=_ver(world, "claude"))
    assert r.verdict == Verdict.DEFERRED and "desde una sesion de Claude" in r.detail


def test_opencode_en_uso_se_pospone(world, tmp_path):
    pv, _ = _pv(world, tmp_path, "opencode", "1.19.0", "1.19.4")
    r = auxiliary.update_one(world.cfg, world.runner, pv, version_of=_ver(world, "opencode"), in_use=lambda p: 1)
    assert r.verdict == Verdict.DEFERRED and "en uso" in r.detail


def test_salto_mayor_requiere_aprobacion(world, tmp_path):
    pv, _ = _pv(world, tmp_path, "opencode", "1.19.0", "2.0.0")
    r = auxiliary.update_one(world.cfg, world.runner, pv, version_of=_ver(world, "opencode"), in_use=lambda p: 0)
    assert r.verdict == Verdict.DEFERRED and "mayor" in r.detail


def test_actualizacion_parcial_se_revierte(world, tmp_path):
    """El comando dice OK pero el binario nuevo no pasa el smoke: se restaura."""
    pv, bindir = _pv(world, tmp_path, "opencode", "1.19.0", "1.19.4")
    st = world.runner.aux["opencode"]
    st.update(after="1.19.4", smoke_ok=False)
    real = os.path.realpath(bindir / "opencode")
    orig = Path(real).read_bytes()

    def version_of():
        return st["version"]
    # el "instalador" sustituye el binario
    def fake_update(argv, **kw):
        Path(real).write_text("#!/bin/sh\nexit 1\n")
        st["version"] = "1.19.4"
        from hermes_updater.proc import Result
        return Result(tuple(argv), 0, "", 0.1)
    run = world.runner.run
    world.runner.run = lambda argv, **kw: fake_update(argv, **kw) if argv[-1] == "curl" else run(argv, **kw)
    restored = []
    orig_restore = auxiliary.restore

    def spy(rp):
        ok = orig_restore(rp)
        st["version"], st["smoke_ok"] = "1.19.0", True
        restored.append(ok)
        return ok
    auxiliary.restore = spy
    try:
        r = auxiliary.update_one(world.cfg, world.runner, pv, version_of=version_of, in_use=lambda p: 0)
    finally:
        auxiliary.restore = orig_restore
    assert r.verdict == Verdict.FAIL
    assert restored == [True] and r.rolled_back is True
    assert Path(real).read_bytes() == orig                  # binario original de vuelta
    assert "restaurado OK" in r.detail


def test_fallo_sin_cambios_no_toca_nada(world, tmp_path):
    pv, _ = _pv(world, tmp_path, "codex", "0.160.0", "0.161.0")
    world.runner.aux["codex"]["update_rc"] = 1
    r = auxiliary.update_one(world.cfg, world.runner, pv, version_of=_ver(world, "codex"), in_use=lambda p: 0)
    assert r.verdict == Verdict.FAIL and "sin cambios" in r.detail


def test_restore_de_symlink_es_atomico(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("a")
    b.write_text("b")
    link = tmp_path / "current"
    link.symlink_to(a)
    rp = {"kind": "symlink", "link": str(link), "target": str(a)}
    os.unlink(link)
    link.symlink_to(b)
    assert auxiliary.restore(rp) and os.readlink(link) == str(a)


# --- autorizacion -------------------------------------------------------------------

def _payload(now=1000.0, ttl=3600, **over):
    p = approval.make_payload(source_commit="a" * 40, source_version="v0.1.0", target_tag="v0.2.0",
                              target_commit="b" * 40, candidate_commit="c" * 40, candidate_branch="x",
                              staging_repo="/r", staging_venv="/v", lock_sha256="d" * 64, backup_id="bk",
                              preupdate_dir="/p", preupdate_manifest="e" * 64, tests={"t": "PASS"},
                              review="PASS", created_at=now, ttl_s=ttl)
    p.update(over)
    return p


def _approve(d: Path, code: str, at: float, factor="otp ok", ttl=1800):
    (d / f"{code}.json").write_text(json.dumps({"code": code, "approved_at": at, "expires_at": at + ttl,
                                                "uid": "1000", "factor": factor}))
    os.chmod(d / f"{code}.json", 0o644)


def test_aprobacion_valida(world):
    p = _payload()
    code = approval.candidate_code(p)
    _approve(world.approvals, code, 1100)
    ok, why, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert ok, why


def test_sin_aprobacion_no_se_despliega(world):
    ok, why, _ = approval.check(world.approvals, _payload(), owner_uid=os.getuid(), now=1200)
    assert not ok and "sin aprobacion" in why


def test_token_caducado(world):
    p = _payload()
    code = approval.candidate_code(p)
    _approve(world.approvals, code, 1100, ttl=60)
    ok, why, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert not ok and "caducado" in why


def test_candidato_caducado(world):
    p = _payload(ttl=100)
    _approve(world.approvals, approval.candidate_code(p), 1050)
    ok, why, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert not ok and "candidato ha caducado" in why


def test_aprobacion_de_otro_candidato_no_sirve(world):
    p1, p2 = _payload(), _payload()
    assert approval.candidate_code(p1) != approval.candidate_code(p2)       # nonce distinto
    _approve(world.approvals, approval.candidate_code(p1), 1100)
    ok, _, _ = approval.check(world.approvals, p2, owner_uid=os.getuid(), now=1200)
    assert not ok


def test_cambiar_cualquier_campo_cambia_el_codigo():
    p = _payload()
    c = approval.candidate_code(p)
    for k, v in (("backup", {"id": "otro"}), ("tests", {"t": "FAIL"}), ("review", "DEFERRED"),
                 ("target", {"tag": "v9", "commit": "f" * 40})):
        assert approval.candidate_code({**p, k: v}) != c


def test_aprobacion_anterior_al_candidato(world):
    p = _payload(now=1000)
    _approve(world.approvals, approval.candidate_code(p), 900)
    ok, why, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert not ok and "anterior" in why


def test_aprobacion_no_escrita_por_root_se_ignora(world):
    """Con el dueño real (root) exigido, un fichero del usuario no vale."""
    p = _payload()
    _approve(world.approvals, approval.candidate_code(p), 1100)
    ok, why, _ = approval.check(world.approvals, p, owner_uid=0, now=1200)
    assert not ok and "root" in why


def test_aprobacion_escribible_por_otros_se_ignora(world):
    p = _payload()
    code = approval.candidate_code(p)
    _approve(world.approvals, code, 1100)
    os.chmod(world.approvals / f"{code}.json", 0o666)
    ok, _, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert not ok


def test_aprobacion_sin_factor_se_ignora(world):
    p = _payload()
    _approve(world.approvals, approval.candidate_code(p), 1100, factor="")
    ok, why, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert not ok and "segundo factor" in why


def test_enlace_simbolico_como_aprobacion_se_ignora(world, tmp_path):
    p = _payload()
    code = approval.candidate_code(p)
    real = tmp_path / "x.json"
    real.write_text(json.dumps({"code": code, "approved_at": 1100, "expires_at": 9e9, "factor": "otp ok"}))
    (world.approvals / f"{code}.json").symlink_to(real)
    ok, _, _ = approval.check(world.approvals, p, owner_uid=os.getuid(), now=1200)
    assert not ok


def test_comando_de_aprobacion_coincide_con_la_gramatica_de_hermes_control():
    assert approval.approve_command("0" * 24) == "! hermes-update aprobar " + "0" * 24
