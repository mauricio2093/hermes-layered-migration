"""Fusiones revisadas de ARBOL COMPLETO, ligadas a (origen, objetivo).

rerere solo reaplica lo que cambio dentro de un fichero en conflicto. Cuando
upstream mueve codigo a un fichero nuevo, la parte propia hay que reinsertarla
FUERA de los bloques en conflicto, y rerere la pierde en silencio (v0.22.4,
2026-10-22: 19 lineas de Capa 2 en gateway/run_inbound_hooks.py).

Una fusion revisada guarda el commit resuelto a mano en un bundle, junto con
el tree SHA revisado. Solo se usa cuando el HEAD productivo y el commit del tag
son EXACTAMENTE los registrados, y es fail-closed: si existe un registro para
esa pareja y algo no cuadra (bundle, padres, tree), el candidato queda
BLOCKED y no se recurre a merge ni a rerere. El candidato se crea con
``commit-tree`` sobre el tree revisado, asi que lo que se compara es el
arbol completo, no el SHA del commit (que cambia con autor, fecha o mensaje).

Directorio: ``<state>/resolutions/merges/<source>-<target>/``
  record.json   source, target_commit, target_tag, commit, tree_sha, reviewed_by, reviewed_at, note
  merge.bundle  el commit revisado; prerrequisitos ^source ^target
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

BUNDLE_REF = "refs/reviewed/merge"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.M)


class ReviewError(Exception):
    """Un registro existe pero no se puede usar: el candidato se bloquea."""


def record_dir(merges_dir: Path, source: str, target: str) -> Path:
    return merges_dir / f"{source}-{target}"


def _out(git, *args) -> str:
    r = git(*args)
    if not r.ok:
        raise ReviewError(f"git {' '.join(args[:2])} fallido: {r.tail(300).strip()}")
    return r.out.strip()


def record(git, merges_dir: Path, commit: str, *, target_tag: str, reviewed_by: str,
           note: str = "") -> Path:
    """Guarda ``commit`` (una fusion source+target ya revisada) como fusion
    revisada. Valida padres y ausencia de marcas de conflicto; nunca
    sobrescribe un registro existente."""
    commit = _out(git, "rev-parse", "--verify", f"{commit}^{{commit}}")
    parents = _out(git, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    if len(parents) != 2:
        raise ReviewError(f"{commit[:10]} no es una fusion de dos padres ({len(parents)} padres)")
    source, target = parents
    tag_commit = _out(git, "rev-parse", "--verify", f"refs/tags/{target_tag}^{{commit}}")
    if tag_commit != target:
        raise ReviewError(f"el segundo padre {target[:10]} no es el tag {target_tag} ({tag_commit[:10]})")
    changed = set(_out(git, "diff", "--name-only", source, commit).split()) | \
        set(_out(git, "diff", "--name-only", target, commit).split())
    for f in sorted(changed):
        r = git("show", f"{commit}:{f}")
        if not r.ok:
            continue
        # Solo cuentan las marcas que la resolucion INTRODUCE: un fichero de
        # upstream puede contenerlas a proposito (p. ej. un test de conflictos).
        marks = {l for l in r.out.splitlines() if _MARKER.match(l)}
        for parent in (source, target):
            if not marks:
                break
            pr = git("show", f"{parent}:{f}")
            if pr.ok:
                marks -= set(pr.out.splitlines())
        if marks:
            raise ReviewError(f"{f} conserva marcas de conflicto en {commit[:10]}")
    dest = record_dir(merges_dir, source, target)
    if dest.exists():
        raise ReviewError(f"ya existe un registro para {source[:10]}..{target[:10]}: {dest}")
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        _out(git, "update-ref", BUNDLE_REF, commit)
        _out(git, "bundle", "create", str(tmp / "merge.bundle"), BUNDLE_REF, f"^{source}", f"^{target}")
        git("update-ref", "-d", BUNDLE_REF)
        rec = {"source": source, "target_commit": target, "target_tag": target_tag, "commit": commit,
               "tree_sha": _out(git, "rev-parse", f"{commit}^{{tree}}"),
               "files_changed_vs_source": len(_out(git, "diff", "--name-only", source, commit).split()),
               "reviewed_by": reviewed_by, "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "note": note}
        (tmp / "record.json").write_text(json.dumps(rec, indent=2) + "\n")
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def find(merges_dir: Path, source: str, target: str) -> Optional[Path]:
    d = record_dir(merges_dir, source, target)
    return d if d.exists() else None


def apply(git, rec_dir: Path, *, source: str, target: str, message: str) -> dict:
    """Con el clon de staging en ``source``: deja HEAD en un commit nuevo
    (padres source, target) cuyo tree es EXACTAMENTE el revisado. Cualquier
    discrepancia lanza ReviewError; el llamador bloquea el candidato."""
    try:
        rec = json.loads((rec_dir / "record.json").read_text())
    except (OSError, ValueError) as exc:
        raise ReviewError(f"record.json ilegible: {exc}") from exc
    for k in ("source", "target_commit", "commit", "tree_sha"):
        if not _SHA.match(str(rec.get(k, ""))):
            raise ReviewError(f"record.json: campo {k} ausente o invalido")
    if rec["source"] != source or rec["target_commit"] != target:
        raise ReviewError("el registro no corresponde a este origen/objetivo")
    if _out(git, "rev-parse", "HEAD") != source:
        raise ReviewError("el clon de staging no esta en el origen")
    bundle = rec_dir / "merge.bundle"
    v = git("bundle", "verify", str(bundle))
    if not v.ok:
        raise ReviewError(f"bundle no verificable: {v.tail(300).strip()}")
    _out(git, "fetch", "-q", "--no-tags", str(bundle), f"+{BUNDLE_REF}:{BUNDLE_REF}")
    got = _out(git, "rev-parse", f"{BUNDLE_REF}^{{commit}}")
    if got != rec["commit"]:
        raise ReviewError(f"el bundle trae {got[:10]}, el registro dice {rec['commit'][:10]}")
    parents = _out(git, "rev-list", "--parents", "-n", "1", got).split()[1:]
    if parents != [source, target]:
        raise ReviewError("los padres del commit revisado no son [origen, objetivo]")
    tree = _out(git, "rev-parse", f"{got}^{{tree}}")
    if tree != rec["tree_sha"]:
        raise ReviewError(f"tree del bundle {tree[:10]} != tree revisado {rec['tree_sha'][:10]}")
    new = _out(git, "commit-tree", tree, "-p", source, "-p", target, "-m", message)
    _out(git, "reset", "-q", "--hard", new)
    git("update-ref", "-d", BUNDLE_REF)
    final = _out(git, "rev-parse", "HEAD^{tree}")
    if final != rec["tree_sha"] or git("status", "--porcelain").out.strip():
        raise ReviewError("el arbol del candidato no coincide con el revisado tras aplicarlo")
    return {"record": str(rec_dir), "reviewed_commit": rec["commit"][:10], "tree_sha": tree,
            "reviewed_by": rec.get("reviewed_by"), "reviewed_at": rec.get("reviewed_at"), "note": rec.get("note", "")}


def check_invariants(git, commit: str, invariants) -> list[str]:
    """Cada invariante es ``"ruta::literal"``: el literal debe aparecer en
    algun fichero bajo esa ruta del commit. Devuelve los que faltan. Defiende
    las capas propias de una fusion que las pierda sin conflicto."""
    missing = []
    for inv in invariants:
        path, sep, literal = str(inv).partition("::")
        if not sep or not literal:
            missing.append(f"{inv} (mal formado)")
            continue
        r = git("grep", "-q", "-F", "-e", literal, commit, "--", path or ".")
        if not r.ok:
            missing.append(inv)
    return missing
