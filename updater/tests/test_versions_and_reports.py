"""Deteccion de las cuatro herramientas y mensajes sin/con actualizaciones."""

import urllib.error

from conftest import fake_fetch, git, make_cli

from hermes_updater import report, versions
from hermes_updater.proc import Result
from hermes_updater.status import Verdict


def test_hermes_detecta_version_instalada_y_estable_disponible(world):
    pv = versions.hermes_version(world.cfg, world.runner)
    assert pv.verdict == Verdict.PASS
    assert pv.installed == "v0.1.0"
    assert pv.available == "v0.2.0"
    assert pv.update_available is True
    inst = pv.extra["installed"]
    assert inst["branch"] == "feat/layer2-schema"
    assert len(inst["carried_commits"]) == 2          # los dos commits propios
    assert pv.extra["target_in_head"] is False


def test_hermes_no_ofrece_lo_que_ya_contiene(world):
    git(world.prod, "fetch", "-q", "origin", "refs/tags/v0.2.0:refs/tags/v0.2.0")
    git(world.prod, "merge", "-q", "--no-edit", "v0.2.0")
    pv = versions.hermes_version(world.cfg, world.runner)
    assert pv.update_available is False
    assert pv.extra["target_in_head"] is True


def test_hermes_no_toca_el_checkout_productivo(world):
    head = git(world.prod, "rev-parse", "HEAD")
    refs = git(world.prod, "for-each-ref")
    versions.hermes_version(world.cfg, world.runner)
    assert git(world.prod, "rev-parse", "HEAD") == head
    assert git(world.prod, "for-each-ref") == refs        # ni un fetch
    assert git(world.prod, "status", "--porcelain") == ""


def test_hermes_sin_red_es_insufficient_data_nunca_pass(world):
    down = lambda: Result(("git",), 128, "fatal: unable to access: Could not resolve host: github.com", 0.1)
    pv = versions.hermes_version(world.cfg, world.runner, ls_remote=down)
    assert pv.verdict == Verdict.INSUFFICIENT_DATA
    assert pv.update_available is None
    assert pv.extra["network_error"]


def test_hermes_shallow_no_se_da_por_bueno(world, tmp_path):
    import subprocess
    sh = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "feat/layer2-schema",
                    f"file://{world.prod}", str(sh)], check=True)
    from dataclasses import replace
    pv = versions.hermes_version(replace(world.cfg, prod_repo=sh), world.runner)
    assert pv.verdict == Verdict.INSUFFICIENT_DATA
    assert "shallow" in pv.detail


def test_cli_detecta_version_metodo_y_mecanismo(world, tmp_path):
    for name, v in (("claude", "2.1.0"), ("codex", "0.160.0"), ("opencode", "1.19.0")):
        path = make_cli(tmp_path, name, v)
    fetch = fake_fetch({"@anthropic-ai/claude-code": "2.1.5", "@openai/codex": "0.160.0", "opencode-ai": "2.0.0"})
    out = {p: versions.cli_version(p, world.cfg, world.runner, fetch, path_env=str(path), sleep=lambda s: None)
           for p in ("claude", "codex", "opencode")}
    assert (out["claude"].installed, out["claude"].available, out["claude"].update_available) == ("2.1.0", "2.1.5", True)
    assert out["claude"].update_command == ("claude", "update")
    assert out["codex"].update_available is False
    assert out["codex"].update_command == ("codex", "update")
    assert out["opencode"].update_command == ("opencode", "upgrade", "--method", "curl")
    assert out["opencode"].major_jump is True
    assert all(v.verdict == Verdict.PASS for v in out.values())


def test_cli_registro_caido_tras_reintentos(world, tmp_path):
    path = make_cli(tmp_path, "codex", "0.160.0")
    calls = []

    def fetch(url):
        calls.append(url)
        raise urllib.error.URLError("Temporary failure in name resolution")
    pv = versions.cli_version("codex", world.cfg, world.runner, fetch, path_env=str(path), sleep=lambda s: None)
    assert pv.verdict == Verdict.INSUFFICIENT_DATA and pv.update_available is None
    assert len(calls) == world.cfg.policy.net_retries


def test_cli_no_instalado(world, tmp_path):
    pv = versions.cli_version("opencode", world.cfg, world.runner, fake_fetch({}), path_env=str(tmp_path / "vacio"))
    assert pv.verdict == Verdict.NOT_OBSERVED


def _vd(**over):
    base = {p: {"installed": v, "available": v, "update_available": False, "verdict": "PASS"}
            for p, v in (("hermes", "v0.1.0"), ("claude", "2.1.0"), ("codex", "0.160.0"), ("opencode", "1.19.0"))}
    for k, v in over.items():
        base[k].update(v)
    return base


def test_mensaje_sin_actualizaciones():
    msg = report.daily_no_updates(_vd(), gateway="PASS", backup="PASS")
    assert msg.splitlines()[0] == "HERMES — Revisión diaria"
    for line in ("Hermes: v0.1.0 — Actualizado", "Claude: 2.1.0 — Actualizado", "Codex: 0.160.0 — Actualizado",
                 "OpenCode: 1.19.0 — Actualizado", "Nuevas versiones: 0", "Gateway: PASS", "Backup diario: PASS",
                 "No se requieren actualizaciones."):
        assert line in msg


def test_mensaje_sin_datos_no_dice_actualizado():
    msg = report.daily_no_updates(_vd(codex={"update_available": None, "verdict": "INSUFFICIENT_DATA"}),
                                  gateway="PASS", backup="PASS")
    assert "Codex: 0.160.0 — Actualizado" not in msg
    assert "sin datos" in msg and "Revisión incompleta" in msg


def test_mensaje_con_actualizaciones():
    msg = report.updates_available(_vd(hermes={"available": "v0.2.0", "update_available": True},
                                       claude={"available": "2.1.5", "update_available": True}))
    assert msg.startswith("HERMES — Actualizaciones detectadas")
    assert "Hermes:\nActual: v0.1.0\nDisponible: v0.2.0" in msg
    assert "Claude:\nActual: 2.1.0\nDisponible: 2.1.5" in msg
    assert "Codex: 0.160.0 — Actualizado" in msg
    assert "Preparando backup y validaciones..." in msg
