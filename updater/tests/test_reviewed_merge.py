"""Fusiones revisadas de arbol completo e invariantes de las capas propias.

Reproduce en pequeno el caso real de v0.22.4 (2026-10-22): upstream mueve una
funcion a un fichero nuevo; la parte propia hay que reinsertarla en ese fichero,
FUERA de los bloques en conflicto, y rerere la pierde sin avisar.
"""

import json
import shutil
from dataclasses import replace
import subprocess
from pathlib import Path

import pytest
from conftest import commit, git

from hermes_updater import reviewed_merge, staging
from hermes_updater.status import Verdict

HOOK_OLD = "def hooks():\n    fire()\n"
HOOK_OURS = "def hooks():\n    fire()\n    record_use()  # capa propia\n"
MOVED = "def hooks():\n    fire()\n"
MOVED_OURS = "def hooks():\n    fire()\n    record_use()  # capa propia\n"


def _prepare(world, tag, **kw):
    c = git(world.up, "rev-parse", f"{tag}^{{commit}}")
    o = git(world.up, "rev-parse", tag)
    return staging.prepare(world.cfg, world.runner, target_tag=tag, target_commit=c, tag_object=o,
                           build_venv=False, **kw)


def _invariants(world, *inv):
    world.cfg = replace(world.cfg, policy=replace(world.cfg.policy, carried_invariants=inv))


def _moved_function(world):
    """Base comun con hooks en inbound.py; produccion anade su linea; upstream
    la mueve a inbound_hooks.py y deja inbound.py sin la funcion."""
    commit(world.up, {"inbound.py": "X = 1\n" + HOOK_OLD}, "upstream: inbound")
    git(world.up, "tag", "-a", "v0.2.5", "-m", "r")
    git(world.prod, "fetch", "-q", str(world.up), "refs/tags/v0.2.5:refs/tags/v0.2.5")
    git(world.prod, "merge", "-q", "--no-edit", "v0.2.5")
    commit(world.prod, {"inbound.py": "X = 1\n" + HOOK_OURS}, "feat(layer2): record use in hooks")
    commit(world.up, {"inbound.py": "X = 1\n", "inbound_hooks.py": MOVED}, "upstream: move hooks")
    git(world.up, "tag", "-a", "v0.3.0", "-m", "r")
    _invariants(world, "hermes_layer2/::MENU = 1", "::record_use()")


def _resolve_by_hand(world, tmp, *, rerere=True, extra=None):
    """Lo que hace una persona: merge, resolver (tomar upstream y reinsertar la
    linea propia en el fichero nuevo), commit. Devuelve (clon, commit)."""
    scratch = tmp / "resolve"
    subprocess.run(["git", "clone", "-q", "--shared", "--branch", "feat/layer2-schema",
                    str(world.prod), str(scratch)], check=True)
    if rerere:
        git(scratch, "config", "rerere.enabled", "true")
    git(scratch, "fetch", "-q", str(world.up), "refs/tags/v0.3.0:refs/tags/v0.3.0")
    subprocess.run(["git", "-C", str(scratch), "merge", "--no-ff", "--no-edit", "refs/tags/v0.3.0"],
                   capture_output=True)
    (scratch / "inbound.py").write_text("X = 1\n")
    (scratch / "inbound_hooks.py").write_text(MOVED_OURS)
    for f, text in (extra or {}).items():
        (scratch / f).write_text(text)
    git(scratch, "add", "-A")
    git(scratch, "commit", "-q", "--no-edit")
    return scratch, git(scratch, "rev-parse", "HEAD")


def _record(world, scratch, c, tag="v0.3.0"):
    g = staging.Git(world.runner, scratch)
    return reviewed_merge.record(g, world.cfg.merges_dir, c, target_tag=tag, reviewed_by="test")


def test_solo_rerere_pierde_la_capa_y_la_invariante_lo_bloquea(world, tmp_path):
    _moved_function(world)
    scratch, _ = _resolve_by_hand(world, tmp_path)
    shutil.copytree(scratch / ".git" / "rr-cache", world.cfg.resolutions_dir, dirs_exist_ok=True)
    cand = _prepare(world, "v0.3.0")
    # rerere resolvio inbound.py, pero la linea propia de inbound_hooks.py no existe
    assert cand.verdict == Verdict.BLOCKED, cand.detail
    assert "::record_use()" in cand.analysis["carried_invariants"]["missing"]
    assert "pierde codigo de las capas propias" in cand.detail


def test_fusion_revisada_reproduce_el_arbol_completo(world, tmp_path):
    _moved_function(world)
    head = git(world.prod, "rev-parse", "HEAD")
    scratch, c = _resolve_by_hand(world, tmp_path, rerere=False)
    reviewed_tree = git(scratch, "rev-parse", f"{c}^{{tree}}")
    d = _record(world, scratch, c)
    rec = json.loads((d / "record.json").read_text())
    assert rec["source"] == head and rec["tree_sha"] == reviewed_tree
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.PASS, cand.detail
    repo = Path(cand.repo)
    assert git(repo, "rev-parse", f"{cand.candidate_commit}^{{tree}}") == reviewed_tree
    assert git(repo, "show", f"{cand.candidate_commit}:inbound_hooks.py") == MOVED_OURS.rstrip("\n")
    assert git(repo, "rev-parse", f"{cand.candidate_commit}^1") == head
    assert git(repo, "rev-parse", f"{cand.candidate_commit}^2") == git(world.up, "rev-parse", "v0.3.0^{commit}")
    assert "fusion revisada de arbol completo" in cand.detail
    assert "Reviewed full-tree resolution" in git(repo, "log", "-1", "--format=%B", cand.candidate_commit)
    assert cand.analysis["reviewed_merge"]["tree_sha"] == reviewed_tree
    assert "record_use()" in cand.analysis["reviewed_merge_diff"]
    assert cand.analysis["carried_invariants"]["missing"] == []
    # produccion intacta y el registro no se modifica
    assert git(world.prod, "rev-parse", "HEAD") == head and git(world.prod, "status", "--porcelain") == ""
    assert json.loads((d / "record.json").read_text()) == rec


def test_tree_distinto_al_revisado_bloquea_sin_recurrir_a_merge(world, tmp_path):
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path)
    shutil.copytree(scratch / ".git" / "rr-cache", world.cfg.resolutions_dir, dirs_exist_ok=True)
    d = _record(world, scratch, c)
    rec = json.loads((d / "record.json").read_text())
    rec["tree_sha"] = "0" * 40
    (d / "record.json").write_text(json.dumps(rec))
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.BLOCKED
    assert "inservible" in cand.detail and "tree" in cand.detail
    assert cand.candidate_commit is None and "recorded_resolutions" not in cand.analysis


def test_bundle_corrupto_bloquea(world, tmp_path):
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path)
    d = _record(world, scratch, c)
    (d / "merge.bundle").write_bytes(b"no es un bundle\n")
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.BLOCKED and "bundle" in cand.detail


def test_registro_ilegible_bloquea(world, tmp_path):
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path)
    d = _record(world, scratch, c)
    (d / "record.json").write_text("{")
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.BLOCKED and "record.json" in cand.detail


def test_registro_de_otra_pareja_se_ignora(world, tmp_path):
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path)
    _record(world, scratch, c)
    # produccion avanza: el origen ya no es el revisado
    commit(world.prod, {"hermes_layer2/otro.py": "Y = 1\n"}, "feat(layer2): otro")
    cand = _prepare(world, "v0.3.0")
    assert cand.verdict == Verdict.BLOCKED
    assert cand.conflicts == ["inbound.py"] and "reviewed_merge" not in cand.analysis


def test_record_valida_lo_que_guarda(world, tmp_path):
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path)
    g = staging.Git(world.runner, scratch)
    with pytest.raises(reviewed_merge.ReviewError, match="dos padres"):
        reviewed_merge.record(g, world.cfg.merges_dir, f"{c}^1", target_tag="v0.3.0", reviewed_by="t")
    with pytest.raises(reviewed_merge.ReviewError, match="no es el tag"):
        reviewed_merge.record(g, world.cfg.merges_dir, c, target_tag="v0.2.5", reviewed_by="t")
    reviewed_merge.record(g, world.cfg.merges_dir, c, target_tag="v0.3.0", reviewed_by="t")
    with pytest.raises(reviewed_merge.ReviewError, match="ya existe"):
        reviewed_merge.record(g, world.cfg.merges_dir, c, target_tag="v0.3.0", reviewed_by="t")


def test_record_rechaza_marcas_de_conflicto(world, tmp_path):
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path, extra={"inbound.py": "<<<<<<< ours\nX = 1\n=======\n>>>>>>> theirs\n"})
    g = staging.Git(world.runner, scratch)
    with pytest.raises(reviewed_merge.ReviewError, match="marcas de conflicto"):
        reviewed_merge.record(g, world.cfg.merges_dir, c, target_tag="v0.3.0", reviewed_by="t")
    assert not world.cfg.merges_dir.exists() or not any(world.cfg.merges_dir.iterdir())


def test_merge_limpio_que_pierde_una_capa_tambien_bloquea(world):
    _invariants(world, "hermes_layer2/::MENU = 1", "app.py::NO_EXISTE")
    cand = _prepare(world, "v0.2.0")
    assert cand.verdict == Verdict.BLOCKED
    assert cand.analysis["carried_invariants"]["missing"] == ["app.py::NO_EXISTE"]


def test_record_acepta_marcas_que_ya_traia_upstream(world, tmp_path):
    """Un test de upstream sobre conflictos contiene marcas literales: no son de la resolucion."""
    literal = 'SAMPLE = """\n<<<<<<< ours\nA\n=======\nB\n>>>>>>> theirs\n"""\n'
    commit(world.up, {"conflict_sample.py": literal}, "upstream: test de conflictos")
    _moved_function(world)
    scratch, c = _resolve_by_hand(world, tmp_path)
    assert git(scratch, "show", f"{c}:conflict_sample.py") == literal.rstrip("\n")
    d = _record(world, scratch, c)
    assert (d / "record.json").exists()
