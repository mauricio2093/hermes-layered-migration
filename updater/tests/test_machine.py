"""La maquina de estados de extremo a extremo, en el mundo aislado.

Cutover y rollback se ejecutan DE VERDAD sobre el repo 'prod' del fixture
(git fast-forward / reset, intercambio real de directorios del venv); solo el
gateway (FakeService) y los comandos de Hermes/pip estan simulados."""

import json
import os
import time
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from conftest import commit, fake_fetch, git, make_cli

from hermes_updater import approval, ci, notify, staging, store as store_mod
from hermes_updater.machine import Ops, Orchestrator
from hermes_updater.status import Outcome, State, Verdict
from hermes_updater.store import Store, exclusive


def _clis(world, tmp_path, claude=("2.1.0", "2.1.0")):
    for name, (have, _) in (("claude", claude), ("codex", ("0.160.0", "")), ("opencode", ("1.19.0", ""))):
        bindir = make_cli(tmp_path, name, have, world)
    fetch = fake_fetch({"@anthropic-ai/claude-code": claude[1], "@openai/codex": "0.160.0", "opencode-ai": "1.19.0"})
    return {"fetch": fetch, "path_env": str(bindir), "sleep": lambda s: None}


def _pass_suites(cfg, runner, **kw):
    return [ci.Suite(n, verdict=Verdict.PASS, passed=5, detail="5 ok") for n in
            ("upstream-layer2", "upstream-core", "hermes-control", "smoke-imports", "smoke-config-check",
             "smoke-plugins", "smoke-db-migration")]


def orch(world, tmp_path, transport=None, ops=None, **dk):
    kw = _clis(world, tmp_path, **dk)
    ops = ops or Ops(prepare=partial(staging.prepare, build_venv=True), run_tests=_pass_suites)
    return Orchestrator(world.cfg, runner=world.runner, svc=world.svc, ops=ops,
                        transport=transport or notify.FileTransport(world.cfg.outbox_dir),
                        discover_kwargs=kw, sleep=lambda s: None)


def _msgs(world):
    return [p.read_text() for p in sorted(world.cfg.outbox_dir.glob("*.txt"))]


def _states(world, rid):
    s = Store(world.cfg.db_path)
    try:
        return [t["to_state"] for t in s.transitions(rid)]
    finally:
        s.close()


def _up_to_date(world):
    git(world.prod, "fetch", "-q", "origin", "refs/tags/v0.2.0:refs/tags/v0.2.0")
    git(world.prod, "merge", "-q", "--no-edit", "v0.2.0")


# --- diario --------------------------------------------------------------------------

def test_dia_sin_actualizaciones(world, tmp_path):
    _up_to_date(world)
    rid, out = orch(world, tmp_path).daily("manual")
    assert out == Outcome.NO_UPDATES
    assert _states(world, rid) == ["SCHEDULED", "PREFLIGHT", "DAILY_BACKUP", "VERSION_DISCOVERY",
                                   "UPDATE_CLASSIFICATION", "NO_UPDATES", "NOTIFY_CURRENT_VERSIONS", "COMPLETE"]
    [msg] = _msgs(world)
    assert msg.startswith("HERMES — Revisión diaria")
    assert "Hermes: v0.2.0 — Actualizado" in msg and "Claude: 2.1.0 — Actualizado" in msg
    assert "Backup diario: PASS" in msg and "Nuevas versiones: 0" in msg
    # el backup diario existe y hermes-maint lo aceptaria
    [b] = list((world.home / "backups" / "independiente").iterdir())
    assert json.loads((b / "state.json").read_text())["backup_verified"] is True


def test_dia_con_actualizaciones_prepara_y_pide_aprobacion(world, tmp_path):
    head = git(world.prod, "rev-parse", "HEAD")
    o = orch(world, tmp_path, claude=("2.1.0", "2.1.5"))
    world.runner.aux["claude"]["after"] = "2.1.5"
    rid, out = o.daily("manual")
    assert out == Outcome.PENDING_APPROVAL
    st = _states(world, rid)
    for s in ("UPDATES_AVAILABLE", "NOTIFY_AVAILABLE", "AUX_UPDATES", "BACKUP", "BACKUP_VERIFICATION",
              "STAGING_PREPARATION", "COMPATIBILITY_TESTS", "AI_REVIEW", "UPDATE_READY", "AUTHORIZATION_GATE",
              "TELEGRAM_REPORT", "COMPLETE"):
        assert s in st, s
    msgs = _msgs(world)
    assert msgs[0].startswith("HERMES — Actualizaciones detectadas")
    assert "Claude:\nActual: 2.1.0\nDisponible: 2.1.5" in msgs[0]
    assert any(m.startswith("HERMES — Herramientas auxiliares") and "Claude: PASS" in m for m in msgs)
    prep = next(m for m in msgs if m.startswith("HERMES — Actualización preparada"))
    assert "Estado: PENDING_APPROVAL" in prep and "Revisión IA: DEFERRED" in prep
    assert "Backup restaurable: PASS" in prep and "Tests: PASS" in prep
    s = Store(world.cfg.db_path)
    cand = s.pending_candidate()
    s.close()
    assert f"! hermes-update aprobar {cand['id']}" in prep
    # produccion intacta tras preparar
    assert git(world.prod, "rev-parse", "HEAD") == head and git(world.prod, "status", "--porcelain") == ""


def test_el_mismo_candidato_no_se_prepara_dos_veces(world, tmp_path):
    o = orch(world, tmp_path)
    o.daily("manual")
    n = len(_msgs(world))
    rid, out = o.daily("manual")
    assert out == Outcome.SUCCESS                  # revision hecha; el candidato sigue pendiente
    s = Store(world.cfg.db_path)
    c = {r["name"]: r for r in s.checks(rid)}
    s.close()
    assert c["preparation"]["verdict"] == "SKIPPED" and "pendiente de aprobacion" in c["preparation"]["detail"]
    assert not any(m.startswith("HERMES — Actualización preparada") for m in _msgs(world)[n:])


def test_conflicto_bloquea_y_no_se_reintenta_cada_dia(world, tmp_path):
    commit(world.prod, {"app.py": "A = 1\nB = propio\n"}, "fix propio")
    commit(world.up, {"app.py": "A = 3\nB = upstream\n"}, "upstream")
    git(world.up, "tag", "-a", "v0.3.0", "-m", "r")
    o = orch(world, tmp_path)
    rid, out = o.daily("manual")
    assert out == Outcome.BLOCKED
    blk = [m for m in _msgs(world) if m.startswith("HERMES — Actualización bloqueada")]
    assert len(blk) == 1 and "Conflictos: app.py" in blk[0] and "Producción no se ha tocado" in blk[0]
    rid2, out2 = o.daily("manual")
    s = Store(world.cfg.db_path)
    c = {r["name"]: r for r in s.checks(rid2)}
    s.close()
    assert c["preparation"]["verdict"] == "SKIPPED" and "se reintenta tras" in c["preparation"]["detail"]
    assert len([m for m in _msgs(world) if m.startswith("HERMES — Actualización bloqueada")]) == 1
    assert c["daily"]["verdict"] == "PASS"      # el backup diario sigue haciendose


def test_backup_fallido_bloquea_la_actualizacion(world, tmp_path):
    import sqlite3
    sqlite3.connect(world.home / "intrusa.db").execute("create table t(x)").connection.commit()
    rid, out = orch(world, tmp_path).daily("manual")
    assert out == Outcome.FAILED
    msgs = _msgs(world)
    assert any(m.startswith("HERMES — Backup fallido") and "Bloqueada cualquier actualización" in m for m in msgs)
    assert not any(m.startswith("HERMES — Actualización preparada") for m in msgs)
    assert "STAGING_PREPARATION" not in _states(world, rid)


def test_sin_internet(world, tmp_path):
    from hermes_updater.proc import Result
    import urllib.error
    o = orch(world, tmp_path)
    o.discover_kwargs["ls_remote"] = lambda: Result(("git",), 128, "fatal: Could not resolve host: github.com", 0.1)

    def down(url):
        raise urllib.error.URLError("Temporary failure in name resolution")
    o.discover_kwargs["fetch"] = down
    rid, out = o.daily("manual")
    assert out == Outcome.BLOCKED                  # nunca verde sin datos
    [msg] = _msgs(world)
    assert "Revisión incompleta" in msg and "Backup diario: PASS" in msg


def test_sin_espacio_no_hace_nada_que_escriba(world, tmp_path):
    world.cfg = replace(world.cfg, policy=replace(world.cfg.policy, min_free_disk_gb=10 ** 9))
    rid, out = orch(world, tmp_path).daily("manual")
    assert out == Outcome.FAILED
    assert not (world.home / "backups" / "independiente").exists() or \
        not list((world.home / "backups" / "independiente").iterdir())
    assert "STAGING_PREPARATION" not in _states(world, rid)


def test_ejecucion_concurrente(world, tmp_path):
    from hermes_updater.config import ensure_dirs
    ensure_dirs(world.cfg)
    with exclusive(world.cfg.lock_path):
        rid, out = orch(world, tmp_path).daily("manual")
    assert rid is None and out == Outcome.BLOCKED


def test_telegram_caido_guarda_y_reenvia_sin_duplicar(world, tmp_path):
    _up_to_date(world)
    down = notify.FileTransport(world.cfg.outbox_dir, fail=True)
    rid, out = orch(world, tmp_path, transport=down).daily("manual")
    assert out == Outcome.NO_UPDATES              # la notificacion no bloquea nada
    s = Store(world.cfg.db_path)
    assert len(s.unsent()) == 1
    assert s.unsent()[0]["last_error"] == "transporte simulado caido"
    up = notify.FileTransport(world.cfg.outbox_dir)
    assert notify.flush(s, up, retries=1, sleep=lambda x: None)["sent"] == 1
    s.close()
    orch(world, tmp_path, transport=up).daily("manual")      # mismo dia: misma clave, no se repite
    assert len(_msgs(world)) == 1


def test_una_excepcion_nunca_termina_en_verde(world, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("fallo inesperado")
    o = orch(world, tmp_path, ops=Ops(discover=boom))
    rid, out = o.daily("manual")
    assert out == Outcome.FAILED
    assert any(m.startswith("HERMES — INCIDENTE") for m in _msgs(world))


def test_no_se_puede_cerrar_en_verde_con_obligatorias_sin_pass(tmp_path):
    s = Store(tmp_path / "u.db")
    rid = s.open_run("daily", "test")
    s.check(rid, "x", "y", Verdict.INSUFFICIENT_DATA)
    with pytest.raises(RuntimeError):
        s.close_run(rid, Outcome.SUCCESS)
    s.close_run(rid, Outcome.FAILED)
    with pytest.raises(RuntimeError):
        s.close_run(rid, Outcome.FAILED)          # una sola vez


# --- aplicacion aprobada -------------------------------------------------------------

def _prepared(world, tmp_path):
    o = orch(world, tmp_path)
    rid, out = o.daily("manual")
    assert out == Outcome.PENDING_APPROVAL
    s = Store(world.cfg.db_path)
    row = s.pending_candidate()
    s.close()
    return o, row["id"], json.loads(row["payload"])


def _approve(world, code, at=None):
    at = time.time() if at is None else at
    p = world.approvals / f"{code}.json"
    p.write_text(json.dumps({"code": code, "approved_at": at, "expires_at": at + 1800, "uid": "1000",
                             "factor": "otp ok"}))
    os.chmod(p, 0o644)


def test_sin_aprobacion_no_hay_despliegue(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    head = git(world.prod, "rev-parse", "HEAD")
    rid, out = o.apply()
    assert out == Outcome.BLOCKED
    assert git(world.prod, "rev-parse", "HEAD") == head and world.svc.restarts_called == 0
    assert any(m.startswith("HERMES — Aprobación no válida") for m in _msgs(world))


def test_cutover_aprobado_completo(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    (world.prod / ".venv" / "marca").write_text("venv viejo")
    _approve(world, code)
    rid, out = o.apply()
    assert out == Outcome.SUCCESS
    assert git(world.prod, "rev-parse", "HEAD") == payload["candidate"]["commit"]   # fast-forward real
    assert (world.prod / "app.py").read_text().startswith("A = 2")
    assert (world.prod / "hermes_layer2" / "menu.py").exists()                        # Capa 2 conservada
    assert world.svc.restarts_called == 1                                              # un solo reinicio
    st = _states(world, rid)
    assert st[-5:] == ["CONTROLLED_DEPLOYMENT", "SERVICE_RECOVERY", "POST_UPDATE_TESTS", "TELEGRAM_REPORT", "COMPLETE"]
    final = next(m for m in _msgs(world) if m.startswith("HERMES — Reporte de actualización"))
    assert "Estado final: SUCCESS" in final and "Versión nueva: v0.2.0" in final
    for k in ("Backup: PASS", "Integración: PASS", "Tests: PASS", "Gateway: PASS", "Telegram: PASS",
              "Persistencia: PASS", "Rollback disponible: PASS"):
        assert k in final, k
    # la aprobacion era de un solo uso
    rid2, out2 = o.apply(code)
    assert out2 == Outcome.BLOCKED


def test_aprobacion_reutilizada_no_vale(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    _approve(world, code)
    s = Store(world.cfg.db_path)
    assert s.consume_approval(code, code, 999)       # alguien ya la uso
    s.close()
    rid, out = o.apply()
    assert out == Outcome.BLOCKED


def test_post_pruebas_fallidas_hacen_rollback(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    head = git(world.prod, "rev-parse", "HEAD")
    (world.prod / ".venv" / "marca").write_text("venv viejo")
    world.runner.version_report = "0.1.0"            # el gateway sigue informando la version vieja
    _approve(world, code)
    rid, out = o.apply()
    assert out == Outcome.ROLLED_BACK
    assert git(world.prod, "rev-parse", "HEAD") == head                 # codigo restaurado
    assert (world.prod / ".venv" / "marca").read_text() == "venv viejo"  # venv restaurado
    assert world.svc.restarts_called == 2 and world.svc.is_active()
    assert "ROLLBACK" in _states(world, rid) and "VERIFY_RECOVERY" in _states(world, rid)
    final = next(m for m in _msgs(world) if m.startswith("HERMES — Reporte de actualización"))
    assert "ROLLED_BACK" in final and "post_version_reported" in final


def test_gateway_que_no_reinicia_hace_rollback(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    head = git(world.prod, "rev-parse", "HEAD")
    world.svc.fail_restart = True
    _approve(world, code)
    rid, out = o.apply()
    assert out == Outcome.ROLLED_BACK
    assert git(world.prod, "rev-parse", "HEAD") == head and world.svc.is_active()


def test_fallo_del_rollback_es_incidente_y_para_todo(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    world.svc.fail_restart = True
    world.svc.fail_after_rollback = True
    _approve(world, code)
    rid, out = o.apply()
    assert out == Outcome.RECOVERY_FAILED
    inc = next(m for m in _msgs(world) if m.startswith("HERMES — INCIDENTE"))
    assert "Se han detenido todas las acciones automáticas" in inc and f"--run {rid} --hard" in inc


def test_produccion_cambiada_tras_aprobar_bloquea(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    commit(world.prod, {"otro.txt": "x"}, "cambio manual posterior")
    _approve(world, code)
    rid, out = o.apply()
    assert out == Outcome.BLOCKED
    assert world.svc.restarts_called == 0


def test_backup_preupdate_manipulado_tras_aprobar_bloquea(world, tmp_path):
    o, code, payload = _prepared(world, tmp_path)
    (Path(payload["backup"]["preupdate_dir"]) / "refs.txt").write_text("x")
    _approve(world, code)
    rid, out = o.apply()
    assert out == Outcome.BLOCKED and world.svc.restarts_called == 0


# --- interrupciones ---------------------------------------------------------------------

def _dead_run(world, state, data):
    from hermes_updater.config import ensure_dirs
    ensure_dirs(world.cfg)
    s = Store(world.cfg.db_path)
    rid = s.open_run("apply", "test")
    s.db.execute("UPDATE runs SET state=?, pid=?, data=? WHERE id=?", (state, 999999, json.dumps(data), rid))
    s.close()
    return rid


def test_interrupcion_durante_despliegue_se_reconcilia_con_rollback(world, tmp_path):
    head = git(world.prod, "rev-parse", "HEAD")
    venv_bak = world.tmp / "venv-bak"
    import shutil
    shutil.copytree(world.prod / ".venv", venv_bak, symlinks=True)
    (venv_bak / "marca").write_text("original")
    # el proceso murio tras avanzar el codigo
    commit(world.prod, {"medio.txt": "a medias"}, "avance a medias")
    rp = {"old_head": head, "branch": "feat/layer2-schema", "venv": str(world.prod / ".venv"),
          "venv_backup": str(venv_bak), "systemd_copy": str(world.tmp / "nada"), "ts": "t"}
    dead = _dead_run(world, State.CONTROLLED_DEPLOYMENT.value, {"rollback_point": rp, "phase": "code"})
    _up_to_date_free = None
    orch(world, tmp_path).daily("manual")
    assert git(world.prod, "rev-parse", "HEAD") == head
    assert (world.prod / ".venv" / "marca").read_text() == "original"
    s = Store(world.cfg.db_path)
    assert s.run(dead)["outcome"] == "INTERRUPTED"
    assert "rollback OK" in s.data(dead)["reconcile"]
    s.close()


def test_interrupcion_fuera_de_despliegue_solo_se_cierra(world, tmp_path):
    head = git(world.prod, "rev-parse", "HEAD")
    dead = _dead_run(world, State.STAGING_PREPARATION.value, {})
    orch(world, tmp_path).daily("manual")
    s = Store(world.cfg.db_path)
    assert s.run(dead)["outcome"] == "INTERRUPTED"
    s.close()
    assert world.svc.restarts_called == 0


def test_cli_codigos_de_salida(world, tmp_path, monkeypatch):
    from hermes_updater import cli
    assert cli.EXIT[Outcome.NO_UPDATES] == 0 and cli.EXIT[Outcome.PENDING_APPROVAL] == 4
    assert cli.EXIT[Outcome.RECOVERY_FAILED] == 9
    assert len(set(cli.EXIT[o] for o in (Outcome.BLOCKED, Outcome.FAILED, Outcome.ROLLED_BACK))) == 3


# --- backup diario independiente de las actualizaciones ---------------------------

def _daily_ok(world, rid):
    s = Store(world.cfg.db_path)
    try:
        c = {(r["component"], r["name"]): r for r in s.checks(rid)}
    finally:
        s.close()
    return c[("backup", "daily")]["verdict"]


def test_backup_diario_aunque_la_deteccion_reviente(world, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("deteccion rota")
    rid, out = orch(world, tmp_path, ops=Ops(discover=boom)).daily("manual")
    assert out == Outcome.FAILED
    assert _daily_ok(world, rid) == "PASS"
    assert _states(world, rid).index("DAILY_BACKUP") < len(_states(world, rid))


def test_backup_diario_con_y_sin_actualizaciones_y_sin_red(world, tmp_path):
    from hermes_updater.proc import Result
    import urllib.error
    rid1, _ = orch(world, tmp_path).daily("manual")              # hay actualizacion de Hermes
    assert _daily_ok(world, rid1) == "PASS"
    o = orch(world, tmp_path)
    o.discover_kwargs["ls_remote"] = lambda: Result(("git",), 128, "Could not resolve host", 0.1)
    o.discover_kwargs["fetch"] = lambda url: (_ for _ in ()).throw(urllib.error.URLError("sin red"))
    rid2, _ = o.daily("manual")                                  # sin red
    assert _daily_ok(world, rid2) == "PASS"
    names = sorted(p.name for p in (world.home / "backups" / "independiente").iterdir())
    assert len(names) >= 2


def test_el_preupdate_reutiliza_el_backup_de_la_misma_ejecucion(world, tmp_path):
    rid, out = orch(world, tmp_path).daily("manual")
    assert out == Outcome.PENDING_APPROVAL
    s = Store(world.cfg.db_path)
    d = s.data(rid)
    s.close()
    assert d["preupdate_backup"]["parts"]["backup_a_reused"] is True
    assert d["preupdate_backup"]["backup_dir"] == d["daily_backup"]["backup_dir"]
    assert len(list((world.home / "backups" / "independiente").iterdir())) == 1   # un solo A


def test_si_el_diario_falla_el_preupdate_no_se_inventa_otro(world, tmp_path):
    import sqlite3
    sqlite3.connect(world.home / "intrusa.sqlite").execute("create table t(x)").connection.commit()
    rid, out = orch(world, tmp_path).daily("manual")
    assert out == Outcome.FAILED
    assert "STAGING_PREPARATION" not in _states(world, rid)
