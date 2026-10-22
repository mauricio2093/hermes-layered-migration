"""Fusiones revisadas de ARBOL COMPLETO, ligadas a (origen, objetivo).

rerere solo reaplica lo que cambio dentro de un fichero en conflicto. Cuando
upstream mueve codigo a un fichero nuevo, la parte propia hay que reinsertarla
FUERA de los bloques en conflicto, y rerere la pierde en silencio (v0.22.4,
2026-10-22: 19 lineas de Capa 2 en gateway/run_inbound_hooks.py).

Una fusion revisada guarda el commit resuelto a mano y su tree SHA. Solo se usa
cuando el HEAD productivo y el commit del tag son EXACTAMENTE los registrados,
y es fail-closed: si existe una entrada para esa pareja y algo no cuadra
(entrada, esquema, digest, pack, objetos, padres, tree), el candidato queda
BLOCKED y no se recurre a merge ni a rerere. El candidato se crea con
``commit-tree`` sobre el tree revisado: se compara el arbol completo, no el
SHA del commit (que cambia con autor, fecha o mensaje).

El registro protege la INTEGRIDAD del contenido revisado; no es una
aprobacion. La aprobacion sigue siendo la de siempre (TOTP, o la decision
explicita de activar auto_apply).

Directorio: ``<state>/resolutions/merges/<source>-<target>/``
  record.json  schema_version, source, target_commit, target_tag, commit,
               tree_sha, pack_sha256, pack_objects, reviewed_by, reviewed_at, note
  merge.pack   pack AUTOCONTENIDO (sin deltas contra objetos externos) con todo
               objeto alcanzable desde el commit que no este en los padres. Un
               bundle de git es un pack "fino" y no se puede verificar solo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1
RECORD = "record.json"
PACK = "merge.pack"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKER = re.compile(r"^(<{7}|>{7})( |$)")
_DIFF_LIMIT = 40_000


class ReviewError(Exception):
    """Una entrada existe pero no se puede usar: el candidato se bloquea."""


def record_dir(merges_dir: Path, source: str, target: str) -> Path:
    return merges_dir / f"{source}-{target}"


def _out(git, *args) -> str:
    r = git(*args)
    if not r.ok:
        raise ReviewError(f"git {' '.join(str(a) for a in args[:2])} fallido: {r.tail(300).strip()}")
    return r.out.strip()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_objects(git, commit: str, source: str, target: str) -> set[str]:
    out = _out(git, "rev-list", "--objects", commit, f"^{source}", f"^{target}")
    return {line[:40] for line in out.splitlines() if line}


def _pack_objects_independent(git, pack: Path) -> set[str]:
    """Indexa ``pack`` en un repositorio VACIO, sin alternates: si el pack
    dependiera de objetos externos (deltas finos) o estuviera corrupto, falla.
    Devuelve el conjunto de objetos que contiene."""
    with tempfile.TemporaryDirectory(prefix="reviewed-pack-") as tmp:
        bare = Path(tmp) / "empty.git"
        _out(git, "init", "-q", "--bare", str(bare))
        if (bare / "objects" / "info" / "alternates").exists():
            raise ReviewError("el repositorio de verificacion tiene alternates")
        copy = Path(tmp) / "p.pack"
        shutil.copyfile(pack, copy)
        idx = Path(tmp) / "p.idx"
        r = git("--git-dir", str(bare), "index-pack", "-o", str(idx), str(copy))
        if not r.ok:
            raise ReviewError(f"el pack no es autocontenido o esta corrupto: {r.tail(300).strip()}")
        v = git("--git-dir", str(bare), "verify-pack", "-v", str(idx))
        if not v.ok:
            raise ReviewError(f"verify-pack fallido: {v.tail(300).strip()}")
        return {line[:40] for line in v.out.splitlines()
                if _SHA.match(line[:40]) and line[40:41] == " "
                and line.split()[1:2] in (["commit"], ["tree"], ["blob"], ["tag"])}


def record(git, merges_dir: Path, commit: str, *, target_tag: str, reviewed_by: str,
           note: str = "") -> Path:
    """Guarda ``commit`` (una fusion source+target ya revisada) como fusion
    revisada. Valida padres, tag y marcas de conflicto introducidas, y
    verifica el pack en un repositorio vacio. Nunca sobrescribe."""
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
        marks = {line for line in r.out.splitlines() if _MARKER.match(line)}
        for parent in (source, target):
            if not marks:
                break
            pr = git("show", f"{parent}:{f}")
            if pr.ok:
                marks -= set(pr.out.splitlines())
        if marks:
            raise ReviewError(f"{f} conserva marcas de conflicto en {commit[:10]}")
    dest = record_dir(merges_dir, source, target)
    if os.path.lexists(dest):
        raise ReviewError(f"ya existe una entrada para {source[:10]}..{target[:10]}: {dest}")
    expected = _expected_objects(git, commit, source, target)
    tmp = dest.with_name(dest.name + ".tmp")
    if os.path.lexists(tmp):
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        # pack-objects sin --thin: objetos completos, sin deltas contra los padres
        listing = "\n".join(sorted(expected)) + "\n"
        # pack-objects deja .pack, .idx y (git reciente) .rev, y los RENOMBRA desde un
        # temporal del propio repo: se construye en el mismo sistema de ficheros que el
        # repo y luego se copia (el estado puede estar en otro: rename daria EXDEV).
        git_dir = Path(_out(git, "rev-parse", "--absolute-git-dir"))
        build = Path(tempfile.mkdtemp(prefix="reviewed-pack-", dir=git_dir))
        try:
            pk = git.runner.run(["git", "-C", str(git.repo), "pack-objects", "-q", str(build / "pack")],
                                timeout=900, stdin=listing, env={"LC_ALL": "C"})
            if not pk.ok:
                raise ReviewError(f"pack-objects fallido: {pk.tail(300).strip()}")
            name = pk.out.strip().splitlines()[-1]
            shutil.copyfile(build / f"pack-{name}.pack", tmp / PACK)
        finally:
            shutil.rmtree(build, ignore_errors=True)
        inpack = _pack_objects_independent(git, tmp / PACK)
        missing = expected - inpack
        if missing or commit not in inpack:
            raise ReviewError(f"el pack no contiene {len(missing)} objetos esperados")
        rec = {"schema_version": SCHEMA_VERSION, "source": source, "target_commit": target,
               "target_tag": target_tag, "commit": commit,
               "tree_sha": _out(git, "rev-parse", f"{commit}^{{tree}}"),
               "pack_sha256": _sha256(tmp / PACK), "pack_objects": len(inpack),
               "reviewed_by": reviewed_by, "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "note": note}
        (tmp / RECORD).write_text(json.dumps(rec, indent=2) + "\n")
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def find(merges_dir: Path, source: str, target: str) -> Optional[Path]:
    """La entrada de esa pareja si EXISTE DE CUALQUIER FORMA (tambien un enlace
    roto o un fichero suelto): una entrada corrupta debe bloquear, no pasar
    por ausente."""
    d = record_dir(merges_dir, source, target)
    return d if os.path.lexists(d) else None


def _load(rec_dir: Path) -> dict:
    if rec_dir.is_symlink() or not rec_dir.is_dir():
        raise ReviewError(f"la entrada {rec_dir.name[:21]} no es un directorio real")
    for name in (RECORD, PACK):
        p = rec_dir / name
        if p.is_symlink() or not p.is_file():
            raise ReviewError(f"la entrada no tiene {name} como fichero real")
    try:
        rec = json.loads((rec_dir / RECORD).read_text())
    except (OSError, ValueError) as exc:
        raise ReviewError(f"{RECORD} ilegible: {exc}") from exc
    if not isinstance(rec, dict):
        raise ReviewError(f"{RECORD} no es un objeto JSON ({type(rec).__name__})")
    if type(rec.get("schema_version")) is not int or rec["schema_version"] != SCHEMA_VERSION:
        raise ReviewError(f"{RECORD}: schema_version {rec.get('schema_version')!r}, se esperaba {SCHEMA_VERSION}")
    for k in ("source", "target_commit", "commit", "tree_sha"):
        if not isinstance(rec.get(k), str) or not _SHA.match(rec[k]):
            raise ReviewError(f"{RECORD}: campo {k} ausente o invalido")
    if not isinstance(rec.get("pack_sha256"), str) or not _SHA256.match(rec["pack_sha256"]):
        raise ReviewError(f"{RECORD}: campo pack_sha256 ausente o invalido")
    if not isinstance(rec.get("pack_objects"), int) or isinstance(rec["pack_objects"], bool) \
            or rec["pack_objects"] < 1:
        raise ReviewError(f"{RECORD}: campo pack_objects ausente o invalido")
    return rec


def apply(git, rec_dir: Path, *, source: str, target: str, message: str) -> dict:
    """Con el clon de staging en ``source``: deja HEAD en un commit nuevo
    (padres source, target) cuyo tree es EXACTAMENTE el revisado. Cualquier
    discrepancia lanza ReviewError; el llamador bloquea el candidato."""
    rec = _load(rec_dir)
    if rec["source"] != source or rec["target_commit"] != target:
        raise ReviewError("el registro no corresponde a este origen/objetivo")
    if _out(git, "rev-parse", "HEAD") != source:
        raise ReviewError("el clon de staging no esta en el origen")
    pack = rec_dir / PACK
    if _sha256(pack) != rec["pack_sha256"]:
        raise ReviewError("el digest del pack no coincide con el registrado")
    inpack = _pack_objects_independent(git, pack)
    if len(inpack) != rec["pack_objects"] or rec["commit"] not in inpack:
        raise ReviewError("el pack no contiene el commit revisado o su numero de objetos cambio")
    # importar: copia dentro de objects/pack del clon e indexado alli
    git_dir = Path(_out(git, "rev-parse", "--absolute-git-dir"))
    dest = git_dir / "objects" / "pack" / f"pack-reviewed-{rec['pack_sha256'][:16]}.pack"
    shutil.copyfile(pack, dest)
    _out(git, "index-pack", str(dest))
    got = rec["commit"]
    if _out(git, "cat-file", "-t", got) != "commit":
        raise ReviewError("el commit revisado no esta disponible tras importar el pack")
    if _expected_objects(git, got, source, target) - inpack:
        raise ReviewError("el commit revisado necesita objetos que no estan ni en el pack ni en los padres")
    parents = _out(git, "rev-list", "--parents", "-n", "1", got).split()[1:]
    if parents != [source, target]:
        raise ReviewError("los padres del commit revisado no son [origen, objetivo]")
    tree = _out(git, "rev-parse", f"{got}^{{tree}}")
    if tree != rec["tree_sha"]:
        raise ReviewError(f"tree del pack {tree[:10]} != tree revisado {rec['tree_sha'][:10]}")
    new = _out(git, "commit-tree", tree, "-p", source, "-p", target, "-m", message)
    _out(git, "reset", "-q", "--hard", new)
    final = _out(git, "rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain", "--untracked-files=no")
    if not status.ok:
        raise ReviewError(f"no se pudo comprobar el arbol de trabajo: {status.tail(300).strip()}")
    if final != rec["tree_sha"] or status.out.strip():
        raise ReviewError("el arbol del candidato no coincide con el revisado tras aplicarlo")
    return {"record": str(rec_dir), "schema_version": rec["schema_version"], "reviewed_commit": got[:10],
            "tree_sha": tree, "pack_sha256": rec["pack_sha256"], "pack_objects": rec["pack_objects"],
            "reviewed_by": rec.get("reviewed_by"), "reviewed_at": rec.get("reviewed_at"),
            "note": rec.get("note", ""),
            "integrity_only": "el registro prueba integridad del contenido revisado, no es una aprobacion"}


def review_diffs(git, commit: str, source: str, target: str) -> dict:
    """Lo que el auditor debe ver, contra AMBOS padres: ``show --cc`` omite los
    cambios que coinciden con uno de los dos lados."""
    vs_target = git.out("diff", target, commit)
    return {
        "vs_target_stat": git.out("diff", "--stat", "--stat-width=100", target, commit)[-6000:],
        "vs_target": vs_target[:_DIFF_LIMIT],
        "vs_target_truncated": len(vs_target) > _DIFF_LIMIT,
        "vs_source_stat": git.out("diff", "--shortstat", source, commit),
        "combined": git.out("show", "--cc", "--format=", commit)[:20_000],
    }


def check_invariants(git, commit: str, invariants) -> list[str]:
    """Cada invariante es ``"ruta::literal"``: el literal debe aparecer en
    algun fichero bajo esa ruta del commit. Devuelve los que faltan. Es una
    red de minimos (presencia), no una prueba de comportamiento."""
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
