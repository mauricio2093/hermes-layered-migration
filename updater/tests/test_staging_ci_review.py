"""Preparacion aislada, conflictos, canalizacion de pruebas y auditor IA."""

import json
import subprocess
import sys
from pathlib import Path

from conftest import commit, git

from hermes_updater import ci, review, staging
from hermes_updater.status import Verdict


def _prepare(world, tag="v0.2.0", **kw):
    c = git(world.up, "rev-parse", f"{tag}^{{commit}}")
    o = git(world.up, "rev-parse", tag)
    return staging.prepare(world.cfg, world.runner, target_tag=tag, target_commit=c, tag_object=o,
                           build_venv=kw.pop("build_venv", False), **kw)


def test_candidato_aislado_conserva_capa2_y_no_toca_produccion(world):
    head = git(world.prod, "rev-parse", "HEAD")
    refs = git(world.prod, "for-each-ref")
    cand = _prepare(world)
    assert cand.verdict == Verdict.PASS, cand.detail
    repo = Path(cand.repo)
    # los dos commits propios estan en el candidato, sin reescribir
    for c in git(world.prod, "rev-list", "--no-merges", "HEAD", "^origin/main").split():
        assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", c, cand.candidate_commit]).returncode == 0
    assert git(repo, "rev-parse", f"{cand.candidate_commit}^1") == head          # desciende del HEAD aprobado
    assert (repo / "hermes_layer2" / "menu.py").read_text() == "MENU = 1\n"      # Capa 2 conservada
    assert (repo / "app.py").read_text().startswith("A = 2")                      # upstream aplicado
    # produccion intacta
    assert git(world.prod, "rev-parse", "HEAD") == head
    assert git(world.prod, "for-each-ref") == refs
    assert git(world.prod, "status", "--porcelain") == ""
    a = cand.analysis
    assert a["carried_still_needed"] == 2 and a["carried_already_upstream"] == []
    assert a["config_version"] == {"source": 49, "target": 50, "changes": True}
    assert a["schema_version"]["changes"] is False
    assert a["ref_integrity"]["matches_ls_remote"] is True
    assert a["ref_integrity"]["signature"] == "NOT_OBSERVED"     # sin firma: se dice, no se inventa


def test_conflicto_bloquea_con_lista_exacta(world):
    commit(world.prod, {"app.py": "A = 1\nB = propio\n"}, "fix: cambio propio en app")
    commit(world.up, {"app.py": "A = 3\nB = upstream\n"}, "upstream cambia la misma linea")
    git(world.up, "tag", "-a", "v0.3.0", "-m", "r")
    head = git(world.prod, "rev-parse", "HEAD")
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.BLOCKED
    assert cand.conflicts == ["app.py"]
    assert "app.py" in cand.analysis["overlap_with_carried"]
    assert git(world.prod, "rev-parse", "HEAD") == head and git(world.prod, "status", "--porcelain") == ""


def _record_resolution(world, tag, resolved: dict, tmp):
    """Hace a mano el merge que hara el actualizador, lo resuelve con ``resolved``
    y guarda el rr-cache de rerere donde el actualizador lo busca."""
    scratch = tmp / "resolve"
    subprocess.run(["git", "clone", "-q", "--shared", "--branch", git(world.prod, "rev-parse", "--abbrev-ref", "HEAD"),
                    str(world.prod), str(scratch)], check=True)
    git(scratch, "config", "rerere.enabled", "true")
    git(scratch, "fetch", "-q", str(world.up), f"refs/tags/{tag}:refs/tags/{tag}")
    subprocess.run(["git", "-C", str(scratch), "merge", "--no-ff", "--no-edit", f"refs/tags/{tag}"],
                   capture_output=True)
    for f, text in resolved.items():
        (scratch / f).write_text(text)
    git(scratch, "add", *resolved)
    git(scratch, "commit", "-q", "--no-edit")
    import shutil
    shutil.copytree(scratch / ".git" / "rr-cache", world.cfg.resolutions_dir, dirs_exist_ok=True)


def _same_conflict(world):
    commit(world.prod, {"app.py": "A = 1\nB = propio\n"}, "fix: cambio propio en app")
    commit(world.up, {"app.py": "A = 3\nB = upstream\n"}, "upstream cambia la misma linea")
    git(world.up, "tag", "-a", "v0.3.0", "-m", "r")


def test_resolucion_revisada_se_reutiliza_solo_para_ese_conflicto(world, tmp_path):
    _same_conflict(world)
    _record_resolution(world, "v0.3.0", {"app.py": "A = 3\nB = propio\n"}, tmp_path)
    head = git(world.prod, "rev-parse", "HEAD")
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.PASS, cand.detail
    assert cand.analysis["recorded_resolutions"] == ["app.py"]
    assert "resoluciones revisadas en: app.py" in cand.detail and "limpio" not in cand.detail
    assert "+B = propio" in cand.analysis["recorded_resolution_diff"].replace("++", "+")
    ev = review.build_evidence(versions={}, backup={}, candidate=cand.to_dict(), tests=[], git_log="", home="/nadie")
    assert ev["analysis"]["recorded_resolutions"] == ["app.py"] and ev["analysis"]["recorded_resolution_diff"]
    repo = Path(cand.repo)
    assert git(repo, "show", f"{cand.candidate_commit}:app.py") == "A = 3\nB = propio"
    assert git(repo, "rev-parse", f"{cand.candidate_commit}^1") == head
    assert "reviewed recorded resolutions" in git(repo, "log", "-1", "--format=%B", cand.candidate_commit)
    # el rr-cache del estado no lo modifica el clon
    assert git(world.prod, "rev-parse", "HEAD") == head and git(world.prod, "status", "--porcelain") == ""


def test_un_conflicto_distinto_al_revisado_sigue_bloqueando(world, tmp_path):
    _same_conflict(world)
    _record_resolution(world, "v0.3.0", {"app.py": "A = 3\nB = propio\n"}, tmp_path)
    # upstream publica otra version que choca de otra forma
    commit(world.up, {"app.py": "A = 4\nB = otro\n"}, "upstream vuelve a cambiar")
    git(world.up, "tag", "-a", "v0.4.0", "-m", "r")
    cand = _prepare(world, "v0.4.0")
    assert cand.verdict == Verdict.BLOCKED
    assert cand.conflicts == ["app.py"]
    assert "recorded_resolutions" not in cand.analysis


def test_resolucion_de_solo_uno_de_dos_ficheros_bloquea(world, tmp_path):
    commit(world.prod, {"app.py": "A = 1\nB = propio\n", "hermes_state_common.py": "SCHEMA_VERSION = 31\n# propio\n"},
           "fix: dos cambios propios")
    commit(world.up, {"app.py": "A = 3\nB = upstream\n", "hermes_state_common.py": "SCHEMA_VERSION = 31\n# upstream\n"},
           "upstream choca en dos ficheros")
    git(world.up, "tag", "-a", "v0.3.0", "-m", "r")
    _record_resolution(world, "v0.3.0", {"app.py": "A = 3\nB = propio\n",
                                         "hermes_state_common.py": "SCHEMA_VERSION = 31\n# ambos\n"}, tmp_path)
    # se pierde la resolucion de uno de los dos ficheros
    import shutil
    entries = sorted(p for p in world.cfg.resolutions_dir.iterdir() if p.is_dir())
    assert len(entries) == 2
    shutil.rmtree(entries[0])
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.BLOCKED
    assert sorted(cand.conflicts) == ["app.py", "hermes_state_common.py"]


def test_commit_propio_ya_incorporado_upstream_se_identifica(world):
    commit(world.up, {"hermes_layer2/menu.py": "MENU = 1\n"}, "upstream adopta el menu")
    git(world.up, "tag", "-a", "v0.3.0", "-m", "r")
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.PASS
    assert len(cand.analysis["carried_already_upstream"]) == 1
    assert cand.analysis["carried_still_needed"] == 1


def test_upstream_incompatible_que_no_contiene_la_base_se_bloquea(world):
    # una "release" cortada de una rama anterior a lo que produccion ya corre
    git(world.up, "checkout", "-q", "-b", "old", f"{world.base}~0")
    git(world.up, "reset", "-q", "--hard", world.base)
    git(world.up, "commit", "-q", "--allow-empty", "-m", "rama vieja")
    # produccion ya corre un upstream posterior a esa rama
    commit(world.up, {"x.txt": "x"}, "main posterior")  # en 'old'
    git(world.prod, "fetch", "-q", "origin", "refs/tags/v0.2.0:refs/tags/v0.2.0")
    git(world.prod, "merge", "-q", "--no-edit", "v0.2.0")
    git(world.prod, "update-ref", "refs/remotes/origin/main", "v0.2.0")
    git(world.up, "tag", "-a", "v0.9.0", "-m", "r", "old")
    cand = _prepare(world, "v0.9.0")
    assert cand.verdict == Verdict.BLOCKED
    assert "NO contiene la base upstream" in cand.detail


def test_tag_que_no_coincide_con_ls_remote_falla(world):
    c = git(world.up, "rev-parse", "v0.1.0^{commit}")
    cand = staging.prepare(world.cfg, world.runner, target_tag="v0.2.0", target_commit=c, build_venv=False)
    assert cand.verdict == Verdict.FAIL and "no coincide" in cand.detail


def test_error_instalando_dependencias_falla_el_candidato(world):
    world.runner.fail_pip_install = True
    cand = _prepare(world, build_venv=True)
    assert cand.verdict == Verdict.FAIL
    assert "dependencias" in cand.detail


def test_venv_del_candidato_y_lock(world):
    cand = _prepare(world, build_venv=True)
    assert cand.verdict == Verdict.PASS, cand.detail
    assert cand.deps["extras"] == ["telegram"]           # inferido del venv productivo
    assert Path(cand.deps["lock"]).is_file()
    installs = [c for c in world.runner.calls if "install" in c and "-e" in c]
    assert any(c[-1].endswith("[telegram]") for c in installs)


def test_el_venv_copiado_no_arrastra_pyc_de_pytest_con_rutas_de_produccion(world):
    # 2026-10-18: pytest no corrige la ruta de sus *-pytest-*.pyc al cargarlos; el
    # venv copiado seguia apuntando al de produccion y v0.22.2 abortaba la suite.
    cache = world.prod / ".venv" / "lib" / "python3.11" / "site-packages" / "plug" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "plugin.cpython-311-pytest-9.1.1.pyc").write_bytes(b"x")
    (cache / "plugin.cpython-311.pyc").write_bytes(b"x")
    cand = _prepare(world, build_venv=True)
    assert cand.verdict == Verdict.PASS, cand.detail
    copied = Path(cand.venv) / "lib" / "python3.11" / "site-packages" / "plug" / "__pycache__"
    assert not (copied / "plugin.cpython-311-pytest-9.1.1.pyc").exists()
    assert (copied / "plugin.cpython-311.pyc").exists()                 # los normales se quedan
    assert (cache / "plugin.cpython-311-pytest-9.1.1.pyc").exists()     # produccion intacta
    assert cand.deps["stale_pytest_pyc_removed"] == 1


def test_extras_inferidos():
    pp = '[project]\nname="hermes-agent"\n[project.optional-dependencies]\n' \
         'a=["x-y==1"]\nb=["z>=2"]\nall=["hermes-agent[a,b]"]\n'
    assert staging.installed_extras(pp, {"x-y": "1.0"}) == ["a"]


def test_un_paquete_compartido_no_arrastra_el_extra():
    # 2026-10-18: con "basta uno", aiohttp arrastraba dingtalk/teams/matrix y el
    # candidato instalaba ~50 paquetes que produccion nunca tuvo.
    pp = '[project]\nname="hermes-agent"\n[project.optional-dependencies]\n' \
         'messaging=["aiohttp>=3"]\nmatrix=["aiohttp>=3","mautrix==0.21"]\n'
    assert staging.installed_extras(pp, {"aiohttp": "3.9"}) == ["messaging"]


def test_pin_incumplido_no_cuenta():
    # [web] fija fastapi==0.133.1; produccion tiene 0.141.1 por la dependencia
    # base: [web] nunca se instalo y no debe bajar fastapi en el candidato.
    pp = '[project]\nname="hermes-agent"\n[project.optional-dependencies]\n' \
         'web=["fastapi==0.133.1","uvicorn==0.41.0"]\n'
    assert staging.installed_extras(pp, {"fastapi": "0.141.1", "uvicorn": "0.53.0"}) == []
    assert staging.installed_extras(pp, {"fastapi": "0.133.1", "uvicorn": "0.41.0"}) == ["web"]


def test_marcadores_del_python_del_venv():
    pp = '[project]\nname="hermes-agent"\n[project.optional-dependencies]\n' \
         'u=["uvloop>=0.19; sys_platform != \'win32\'", "winloop; sys_platform == \'win32\'"]\n'
    linux = {"sys_platform": "linux", "python_version": "3.11", "python_full_version": "3.11.15",
             "os_name": "posix", "platform_machine": "x86_64", "platform_python_implementation": "CPython",
             "implementation_name": "cpython", "platform_system": "Linux", "platform_release": "",
             "platform_version": "", "implementation_version": "3.11.15"}
    assert staging.installed_extras(pp, {"uvloop": "0.21"}, linux) == ["u"]
    assert staging.installed_extras(pp, {}, linux) == []


def test_instalacion_directa_sin_version_solo_cumple_sin_pin():
    pp = '[project]\nname="hermes-agent"\n[project.optional-dependencies]\n' \
         'a=["foo"]\nb=["foo==1"]\n'
    assert staging.installed_extras(pp, {"foo": None}) == ["a"]


# --- canalizacion de pruebas con pytest REAL -------------------------------------------

def _repo_with_tests(path: Path, tests: dict) -> Path:
    path.mkdir(parents=True)
    for name, body in tests.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    return path


def test_regresion_vs_fallo_preexistente(world, tmp_path):
    cand = _repo_with_tests(tmp_path / "cand", {
        "tests/test_a.py": "def test_ok():\n    assert True\n"
                           "def test_roto_en_upstream():\n    assert False\n"
                           "def test_roto_por_el_candidato():\n    assert False\n"})
    base = _repo_with_tests(tmp_path / "base", {
        "tests/test_a.py": "def test_ok():\n    assert True\n"
                           "def test_roto_en_upstream():\n    assert False\n"
                           "def test_roto_por_el_candidato():\n    assert True\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL
    assert s.regressions == ["tests/test_a.py::test_roto_por_el_candidato"]
    assert s.preexisting == ["tests/test_a.py::test_roto_en_upstream"]


def test_solo_fallos_preexistentes_pasan_y_se_dice(world, tmp_path):
    body = "def test_ok():\n    assert True\ndef test_viejo():\n    assert False\n"
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": body})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": body})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.PASS
    assert "1 fallos preexistentes" in s.detail


def test_un_modulo_que_no_se_puede_recoger_no_detiene_la_suite(world, tmp_path):
    # 2026-10-18: v0.22.2 trae un test de upstream que falla al recogerse
    # (NameError); interrumpia la suite entera y quedaba en 0 tests ejecutados.
    files = {"tests/test_a.py": "def test_ok():\n    assert True\n",
             "tests/test_roto.py": "x = Path('no importado')\n"}
    cand = _repo_with_tests(tmp_path / "cand", files)
    base = _repo_with_tests(tmp_path / "base", files)
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_*.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.passed == 1
    assert s.verdict == Verdict.PASS, s.detail
    assert s.preexisting == ["tests/test_roto.py"]


def test_un_modulo_que_solo_el_candidato_rompe_es_regresion(world, tmp_path):
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": "def test_ok():\n    assert True\n",
                                                "tests/test_b.py": "x = Path('roto')\n"})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": "def test_ok():\n    assert True\n",
                                                "tests/test_b.py": "def test_b():\n    assert True\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_*.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL
    assert s.regressions == ["tests/test_b.py"]


def test_un_fallo_que_pasa_al_repetirlo_es_inestable_y_se_dice(world, tmp_path):
    flag = tmp_path / "ya-fallo"
    body = (f"import pathlib\nF = pathlib.Path({str(flag)!r})\n"
            "def test_a_veces():\n"
            "    if not F.exists():\n        F.write_text('x')\n        assert False\n")
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": body})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": "def test_a_veces():\n    assert True\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.PASS
    assert s.flaky == ["tests/test_a.py::test_a_veces"] and s.regressions == []
    assert "1 inestables" in s.detail
    ev = review.build_evidence(versions={}, backup={}, candidate={}, tests=[s.to_dict()], git_log="", home="/nadie")
    assert ev["tests"][0]["flaky"] == ["tests/test_a.py::test_a_veces"]


def test_inestable_con_ids_raros_y_duplicados(world, tmp_path):
    # 2026-10-18: un fallo en la llamada y otro al desmontar duplicaban el id, y la
    # cuenta "ids reconocidos <= pasados" descartaba TODOS los inestables.
    flag = tmp_path / "ya-fallo"
    body = (f"import pathlib, pytest\nF = pathlib.Path({str(flag)!r})\n"
            "@pytest.fixture\ndef roto_al_salir():\n    yield\n"
            "    if F.read_text() == '1':\n        F.write_text('2')\n        raise RuntimeError('teardown')\n"
            "def test_a_veces(roto_al_salir):\n"
            "    if not F.exists():\n        F.write_text('1')\n        assert False\n"
            "@pytest.mark.parametrize('v', ['a\\\\nb', 'x y'])\n"
            "def test_param_siempre_roto(v):\n    assert False\n")
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": body})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=None, out=tmp_path / "out")
    assert s.flaky == ["tests/test_a.py::test_a_veces"]
    assert sorted(s.regressions) == ["tests/test_a.py::test_param_siempre_roto[a\\\\nb]",
                                     "tests/test_a.py::test_param_siempre_roto[x y]"]


def test_muchos_inestables_a_la_vez_no_son_inestabilidad(world, tmp_path, monkeypatch):
    # 2026-10-19: bajo systemd, el limite de 1024 descriptores tumbo 940 tests
    # ("Too many open files"); al repetirlos pasaban y salian 1075 "inestables".
    monkeypatch.setattr(ci, "FLAKY_MAX", 2)
    flag = tmp_path / "ya-fallo"
    tests = "".join(f"def test_{i}():\n    if not F{i}.exists():\n        F{i}.write_text('x')\n        assert False\n"
                    for i in range(4))
    head = "import pathlib\n" + "".join(f"F{i} = pathlib.Path({str(flag)!r} + '{i}')\n" for i in range(4))
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": head + tests})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": "".join(f"def test_{i}():\n    pass\n" for i in range(4))})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL
    assert s.flaky == [] and len(s.regressions) == 4
    assert "demasiados para ser inestabilidad" in s.detail


def test_fallo_segun_el_orden_se_compara_con_el_mismo_conjunto(world, tmp_path):
    # 2026-10-22, v0.22.4: un test inestable deja estado global y su vecino falla
    # solo si corre despues. La repeticion del candidato los corre juntos; la base
    # corria solo el vecino y lo veia pasar -> 31 regresiones falsas.
    flag = tmp_path / "ya-fallo"
    body = ("import os, pathlib\n"
            f"F = pathlib.Path({str(flag)!r})\n"
            "def test_a_contamina():\n"
            "    os.environ['CONTAMINADO'] = '1'\n"
            "    if not F.exists():\n        F.write_text('x')\n        assert False\n"
            "def test_b_victima():\n"
            "    assert os.environ.get('CONTAMINADO') != '1'\n")
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": body})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": body})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.flaky == ["tests/test_a.py::test_a_contamina"]
    assert s.recheck_ids == ["tests/test_a.py::test_a_contamina", "tests/test_a.py::test_b_victima"]
    assert s.regressions == [] and s.preexisting == ["tests/test_a.py::test_b_victima"]
    assert s.verdict == Verdict.PASS, s.detail


def test_regresion_real_sigue_detectandose_con_el_mismo_conjunto(world, tmp_path):
    flag = tmp_path / "ya-fallo"
    head = f"import pathlib\nF = pathlib.Path({str(flag)!r})\n"
    flaky = "def test_a_inestable():\n    if not F.exists():\n        F.write_text('x')\n        assert False\n"
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": head + flaky + "def test_b():\n    assert False\n"})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": head + flaky + "def test_b():\n    assert True\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL and s.regressions == ["tests/test_a.py::test_b"]


def test_un_fallo_que_se_repite_sigue_siendo_regresion(world, tmp_path):
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": "def test_x():\n    assert False\n"})
    base = _repo_with_tests(tmp_path / "base", {"tests/test_a.py": "def test_x():\n    assert True\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=base, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL and s.flaky == [] and s.regressions == ["tests/test_a.py::test_x"]


def test_pytest_sin_fsync_solo_si_existe_eatmydata(world, tmp_path, monkeypatch):
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": "def test_ok():\n    assert True\n"})
    fake = tmp_path / "eatmydata"
    fake.write_text('#!/bin/sh\nexec "$@"\n')
    fake.chmod(0o755)
    monkeypatch.setattr(ci, "EATMYDATA", str(fake))
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=None, out=tmp_path / "o1")
    assert s.verdict == Verdict.PASS
    pyt = [c for c in world.runner.calls if "pytest" in c]
    assert pyt and pyt[-1][pyt[-1].index(str(fake)) + 1] == sys.executable
    monkeypatch.setattr(ci, "EATMYDATA", None)
    ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                      cand_repo=cand, pure_target=None, out=tmp_path / "o2")
    assert str(fake) not in [c for c in world.runner.calls if "pytest" in c][-1]


def test_un_internalerror_de_pytest_es_fail_aunque_lo_demas_pase(world, tmp_path):
    cand = _repo_with_tests(tmp_path / "cand", {
        "tests/test_a.py": "def test_ok():\n    assert True\n",
        "tests/conftest.py": "def pytest_runtest_logreport(report):\n"
                             "    if report.when == 'call':\n        raise RuntimeError('pytest roto')\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=None, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL
    assert s.internal_error and "INTERNALERROR" in s.detail
    assert not any("internal" in t for t in s.failed_ids)


def test_cero_tests_ejecutados_no_es_pass(world, tmp_path):
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": "x = 1\n"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=None, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL


def test_sin_tests_que_coincidan_no_es_pass(world, tmp_path):
    cand = _repo_with_tests(tmp_path / "cand", {"README": "x"})
    s = ci.upstream_suite(world.cfg, world.runner, "x", ["tests/nada*.py"], python=sys.executable,
                          cand_repo=cand, pure_target=None, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL


def test_timeout_de_suite_es_fail(world, tmp_path):
    from dataclasses import replace
    cfg = replace(world.cfg, policy=replace(world.cfg.policy, test_timeout_s=2))
    cand = _repo_with_tests(tmp_path / "cand", {"tests/test_a.py": "import time\ndef test_lento():\n    time.sleep(30)\n"})
    s = ci.upstream_suite(cfg, world.runner, "x", ["tests/test_a.py"], python=sys.executable,
                          cand_repo=cand, pure_target=None, out=tmp_path / "out")
    assert s.verdict == Verdict.FAIL and "timeout" in s.detail


# --- auditor IA -----------------------------------------------------------------------

def test_ia_no_disponible_es_deferred_o_blocked_nunca_pass(world):
    rv = review.run_review(world.cfg, world.runner, {"x": 1}, deterministic=Verdict.PASS, provider="none")
    assert rv.verdict == Verdict.DEFERRED
    from dataclasses import replace
    cfg = replace(world.cfg, policy=replace(world.cfg.policy, ai_required=True))
    rv = review.run_review(cfg, world.runner, {"x": 1}, deterministic=Verdict.PASS, provider="none")
    assert rv.verdict == Verdict.BLOCKED


def test_ia_caida_a_mitad_es_deferred(world, monkeypatch):
    from hermes_updater.proc import Result
    monkeypatch.setattr(review.shutil, "which", lambda p: "/usr/bin/" + p)
    world.runner.run = lambda argv, **kw: Result(tuple(argv), 1, "API Error: overloaded", 0.1)
    rv = review.run_review(world.cfg, world.runner, {"x": 1}, deterministic=Verdict.PASS, provider="claude")
    assert rv.verdict == Verdict.DEFERRED and "no disponible" in rv.detail
    assert rv.cost["billing"] == "subscription" and rv.cost["variable_cost_usd"] == 0.0


def test_ia_no_promociona_lo_determinista():
    raw = {"verdict": "pass", "summary": "todo bien", "risks": [], "affected_customizations": [],
           "recommendations": [], "evidence_gaps": []}
    for det in (Verdict.FAIL, Verdict.INSUFFICIENT_DATA, Verdict.NOT_OBSERVED, Verdict.DEFERRED):
        assert review.interpret(raw, deterministic=det).verdict == det
    assert review.interpret(raw, deterministic=Verdict.PASS).verdict == Verdict.PASS


def test_ia_solo_puede_empeorar():
    base = {"summary": "", "risks": [], "affected_customizations": [], "recommendations": [], "evidence_gaps": []}
    assert review.interpret({**base, "verdict": "block"}, deterministic=Verdict.PASS).verdict == Verdict.BLOCKED
    assert review.interpret({**base, "verdict": "concerns"}, deterministic=Verdict.PASS).verdict == Verdict.DEFERRED
    high = {**base, "verdict": "pass", "risks": [{"severity": "high", "area": "x", "description": "y", "evidence": "z"}]}
    assert review.interpret(high, deterministic=Verdict.PASS).verdict == Verdict.DEFERRED
    assert review.interpret({**base, "verdict": "aprobado!"}, deterministic=Verdict.PASS).verdict == Verdict.DEFERRED


def test_ia_que_pide_privilegios_queda_marcada_y_sin_efecto():
    raw = {"verdict": "pass", "summary": "", "risks": [], "affected_customizations": [], "evidence_gaps": [],
           "recommendations": ["anadir NOPASSWD en sudoers para el updater", "revisar el adaptador"]}
    rv = review.interpret(raw, deterministic=Verdict.PASS)
    assert rv.flagged == ["anadir NOPASSWD en sudoers para el updater"]


def test_evidencia_sin_secretos(world):
    # Credenciales FALSAS montadas en tiempo de ejecucion: un literal con su
    # forma haria saltar el escaner de secretos del repo (y con razon).
    bot = "1234567" + "89:" + "A" * 35
    sk, gh = "sk-" + "abcdefghij" * 2, "gh" + "p_" + "abcdefghij" * 3
    ev = review.build_evidence(
        versions={"hermes": {"installed": "v1", "available": "v2", "update_available": True, "verdict": "PASS",
                             "method": "git", "extra": {"secret": "no debe salir"}}},
        backup={"verdict": "PASS", "verification": {"checks": {"m": {"verdict": "PASS", "detail": "/home/u/.hermes ok"}}}},
        candidate={"verdict": "PASS", "detail": f"valor {bot} del bot"},
        tests=[{"name": "t", "verdict": "PASS", "detail": f"{sk} y {gh}"}],
        git_log="abc fix by someone@example.com", home="/home/u")
    raw = json.dumps(ev)
    for bad in ("no debe salir", bot, sk, gh, "someone@example.com", "/home/u"):
        assert bad not in raw, bad
    assert "[REDACTADO]" in raw


def test_proveedor_claude_sin_herramientas():
    argv = review._provider_argv("claude", Path("/w"), Path("/s"), Path("/o"))
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
