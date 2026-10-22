"""Tests requeridos por id, con evidencia JUnit (no basta con un directorio)."""

from pathlib import Path

from hermes_updater import ci
from hermes_updater.status import Verdict

REQ = ("tests/layer2/test_hooks.py::test_a", "tests/layer2/test_hooks.py::test_b")


def _junit(tmp: Path, cases: dict) -> Path:
    rows = []
    for name, kind in cases.items():
        mod, _, fn = name.partition("::")
        cls = mod[:-3].replace("/", ".")
        inner = {"pass": "", "fail": "<failure message='x'/>", "error": "<error message='x'/>",
                 "skip": "<skipped message='x'/>"}[kind]
        rows.append(f'<testcase classname="{cls}" name="{fn}">{inner}</testcase>')
    p = tmp / "upstream-layer2.xml"
    p.write_text('<testsuites><testsuite name="pytest">' + "".join(rows) + "</testsuite></testsuites>")
    return p


def test_todos_pasan(tmp_path):
    s = ci.required_tests_suite(REQ, _junit(tmp_path, {REQ[0]: "pass", REQ[1]: "pass", "tests/x.py::t": "fail"}))
    assert s.verdict == Verdict.PASS and s.detail.startswith("2/2")


def test_uno_falla(tmp_path):
    s = ci.required_tests_suite(REQ, _junit(tmp_path, {REQ[0]: "pass", REQ[1]: "fail"}))
    assert s.verdict == Verdict.FAIL and REQ[1] in s.detail


def test_error_cuenta_como_fallo(tmp_path):
    s = ci.required_tests_suite(REQ, _junit(tmp_path, {REQ[0]: "pass", REQ[1]: "error"}))
    assert s.verdict == Verdict.FAIL


def test_omitido_bloquea(tmp_path):
    s = ci.required_tests_suite(REQ, _junit(tmp_path, {REQ[0]: "pass", REQ[1]: "skip"}))
    assert s.verdict == Verdict.BLOCKED and "omitidos" in s.detail


def test_fichero_desaparecido_bloquea(tmp_path):
    s = ci.required_tests_suite(REQ, _junit(tmp_path, {"tests/layer2/test_otro.py::test_ok": "pass"}))
    assert s.verdict == Verdict.BLOCKED and "no ejecutados" in s.detail and s.failed_ids == list(REQ)


def test_sin_junit_bloquea(tmp_path):
    s = ci.required_tests_suite(REQ, tmp_path / "no-existe.xml")
    assert s.verdict == Verdict.BLOCKED and "JUnit" in s.detail


def test_junit_corrupto_bloquea(tmp_path):
    p = tmp_path / "upstream-layer2.xml"
    p.write_text("<testsuites><testcase")
    s = ci.required_tests_suite(REQ, p)
    assert s.verdict == Verdict.BLOCKED


def test_sin_requeridos_no_cuenta(tmp_path):
    s = ci.required_tests_suite((), tmp_path / "x.xml")
    assert s.required is False and s.verdict == Verdict.SKIPPED
    assert ci.overall([ci.Suite("a", verdict=Verdict.PASS), s]) == Verdict.PASS


def test_requerido_que_falla_tumba_el_veredicto_global(tmp_path):
    s = ci.required_tests_suite(REQ, _junit(tmp_path, {REQ[0]: "pass"}))
    assert ci.overall([ci.Suite("a", verdict=Verdict.PASS), s]) == Verdict.BLOCKED


def test_ids_reales_de_pytest_coinciden_con_los_configurados(tmp_path):
    """El id que construye parse_junit a partir del classname de pytest debe ser
    exactamente el configurado por defecto (si no, todo saldria 'no ejecutado')."""
    from hermes_updater.config import Policy
    req = Policy().required_tests
    p = _junit(tmp_path, {t: "pass" for t in req})
    s = ci.required_tests_suite(req, p)
    assert s.verdict == Verdict.PASS, s.detail


def test_run_all_incluye_los_requeridos_leyendo_el_junit_de_layer2(tmp_path, monkeypatch):
    seen = {}

    def fake_suite(cfg, runner, name, patterns, **kw):
        if name == "upstream-layer2":
            _junit(kw["out"], {REQ[0]: "pass"})          # REQ[1] no se ejecuta
        return ci.Suite(name, verdict=Verdict.PASS)
    monkeypatch.setattr(ci, "upstream_suite", fake_suite)
    monkeypatch.setattr(ci, "hermes_control_suite", lambda *a, **k: ci.Suite("hermes-control", verdict=Verdict.PASS))
    monkeypatch.setattr(ci, "smoke", lambda *a, **k: [])
    from dataclasses import replace
    from hermes_updater.config import Config
    cfg = replace(Config(), policy=replace(Config().policy, required_tests=REQ))
    suites = ci.run_all(cfg, None, python="py", cand_repo=tmp_path, target_tag=None, carried_tests=(),
                        restored_home=None, out=tmp_path / "ci")
    req = next(s for s in suites if s.name == "required-tests")
    assert req.verdict == Verdict.BLOCKED and REQ[1] in req.detail
    assert ci.overall(suites) == Verdict.BLOCKED
