"""Verificacion INDEPENDIENTE de un backup A (backup-hermes-home.sh).

Independiente quiere decir: no lee el ``state.json`` del script para decidir,
no usa sus herramientas (sha256sum, gzip, tar) y no reutiliza su lista de
bases. Recalcula todo en Python y descubre el inventario por su cuenta en el
sistema vivo. Si el script y este modulo discrepan, gana el FAIL.

Comprueba:
  1. MANIFEST.sha256: cada fichero listado, recalculado; nada sin listar.
  2. Archivo: legible entero con ``tarfile``; sin rutas absolutas ni ``..``;
     sin bases SQLite en caliente.
  3. Inventario: las bases del sistema vivo (descubiertas aqui) estan TODAS
     en db-consistent/, y no hay ninguna de mas. Una base nueva no declarada
     es FAIL.
  4. Restauracion real en un directorio aislado: el archivo se extrae, las
     bases se colocan en su ruta, y se abre cada una (quick_check +
     integrity_check + objetos).
  5. Contenido declarado (plugins/, scripts/): el restaurado es exactamente
     content-manifest.sha256, en las dos direcciones.
  6. Permisos de secretos restaurados = los esperados.
  7. Rutas criticas presentes en el restaurado.

La evidencia queda en un JSON atado al sha256 del MANIFEST: una evidencia no
sirve para otro backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .status import Verdict, worst

#: Lo mismo que excluye backup-hermes-home.sh de la busqueda de bases: copias
#: de seguridad, rescates, checkouts de codigo y caches.
DB_SCAN_EXCLUDE = ("backups", "rescue", "hermes-agent", "hermes-oficial-clean", "cache")

#: Sin esto, el restaurado no es un Hermes recuperable.
CRITICAL_PATHS = ("config.yaml", ".env", "auth.json", "SOUL.md", "memories", "skills",
                  "cron", "plugins", "scripts")

CONTENT_DIRS = ("plugins", "scripts")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def flat(rel: str) -> str:
    return rel.replace("/", "_")


def live_databases(hermes_home: Path) -> list[str]:
    """Bases *.db del Hermes vivo, rutas relativas, con las mismas exclusiones
    de nivel superior que el script. Descubiertas aqui, no leidas del script."""
    out = []
    for root, dirs, files in os.walk(hermes_home):
        rel_root = Path(root).relative_to(hermes_home)
        if rel_root == Path("."):
            dirs[:] = [d for d in dirs if d not in DB_SCAN_EXCLUDE]
        for f in files:
            if f.endswith(".db"):
                out.append(str((rel_root / f)) if str(rel_root) != "." else f)
    return sorted(out)


def _sqlite_checks(p: Path) -> dict:
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            quick = c.execute("PRAGMA quick_check").fetchone()[0]
            integ = c.execute("PRAGMA integrity_check").fetchone()[0]
            objects = c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            schema = None
            try:
                schema = c.execute("SELECT max(version) FROM schema_version").fetchone()[0]
            except sqlite3.Error:
                pass
        finally:
            c.close()
        return {"quick_check": quick, "integrity_check": integ, "objects": objects, "schema_version": schema,
                "ok": quick == "ok" and integ == "ok" and objects > 0}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@dataclass
class Report:
    backup_dir: str
    manifest_sha256: Optional[str] = None
    checks: dict = field(default_factory=dict)       # nombre -> {"verdict":..., "detail":...}
    restored_to: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def add(self, name: str, verdict: Verdict, detail: str = "", **ev) -> None:
        self.checks[name] = {"verdict": Verdict(verdict).value, "detail": detail, **ev}

    @property
    def verdict(self) -> Verdict:
        return worst(v["verdict"] for v in self.checks.values())

    def to_dict(self) -> dict:
        return {"backup_dir": self.backup_dir, "manifest_sha256": self.manifest_sha256,
                "verdict": self.verdict.value, "checks": self.checks, "restored_to": self.restored_to,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "verifier": "hermes_updater.verify (python, independiente del script)"}


def _safe_member(m: tarfile.TarInfo) -> Optional[str]:
    name = m.name
    if name.startswith("/") or ".." in Path(name).parts:
        return "ruta fuera del destino"
    if m.isdev() or m.isfifo():
        return "dispositivo/fifo"
    if m.islnk():
        # enlace duro: el destino es otra entrada del propio archivo
        if m.linkname.startswith("/") or ".." in Path(m.linkname).parts:
            return f"enlace duro fuera del archivo -> {m.linkname}"
    if m.issym() and not m.linkname.startswith("/"):
        # relativo: debe quedarse dentro de la raiz al resolverse
        resolved = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
        if resolved.startswith("..") or os.path.isabs(resolved):
            return f"enlace que sale del archivo -> {m.linkname}"
    # Un symlink ABSOLUTO (p. ej. bin/python de un venv -> interprete del host)
    # es legitimo en un HERMES_HOME; se cuenta y se extrae como enlace, sin seguirlo.
    return None


def verify_backup(backup_dir: Path, *, hermes_home: Path, work_dir: Path,
                  keep_restore: bool = False, critical: Iterable[str] = CRITICAL_PATHS) -> Report:
    rep = Report(str(backup_dir))
    backup_dir = Path(backup_dir)

    # 1. MANIFEST
    man = backup_dir / "MANIFEST.sha256"
    if not man.is_file():
        rep.add("manifest", Verdict.FAIL, "falta MANIFEST.sha256")
        rep.finished_at = time.time()
        return rep
    rep.manifest_sha256 = sha256_file(man)
    listed, bad = {}, []
    for line in man.read_text().splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        listed[os.path.normpath(name)] = digest
    for name, digest in listed.items():
        p = backup_dir / name
        if not p.is_file():
            bad.append(f"falta {name}")
        elif sha256_file(p) != digest:
            bad.append(f"sha256 distinto: {name}")
    present = {os.path.normpath("./" + str(p.relative_to(backup_dir)))
               for p in backup_dir.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"}
    unlisted = sorted(present - set(listed))
    if unlisted:
        bad.append("sin listar: " + ", ".join(unlisted[:10]))
    rep.add("manifest", Verdict.FAIL if bad else Verdict.PASS,
            "; ".join(bad[:10]) or f"{len(listed)} ficheros, sha256 recalculado", files=len(listed))

    # 2. archivo
    archives = sorted(backup_dir.glob("hermes-home-*.tar.gz"))
    if len(archives) != 1:
        rep.add("archive", Verdict.FAIL, f"se esperaba 1 archivo, hay {len(archives)}")
        rep.finished_at = time.time()
        return rep
    archive = archives[0]
    members_bad, hot_dbs, top, abs_links = [], [], set(), 0
    try:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()       # lee el archivo ENTERO: detecta truncado
            for m in members:
                why = _safe_member(m)
                if why:
                    members_bad.append(f"{m.name}: {why}")
                if m.issym() and m.linkname.startswith("/"):
                    abs_links += 1
                if m.name.endswith((".db", ".db-wal", ".db-shm")):
                    hot_dbs.append(m.name)
                top.add(Path(m.name).parts[0])
    except (tarfile.TarError, OSError, EOFError) as exc:
        rep.add("archive", Verdict.FAIL, f"archivo ilegible: {type(exc).__name__}: {exc}")
        rep.finished_at = time.time()
        return rep
    problems = members_bad[:5] + ([f"{len(hot_dbs)} bases en caliente dentro del tar"] if hot_dbs else []) \
        + ([f"raices inesperadas {sorted(top)}"] if len(top) != 1 else [])
    rep.add("archive", Verdict.FAIL if problems else Verdict.PASS,
            "; ".join(problems) or f"{len(members)} entradas legibles, sin bases en caliente",
            members=len(members), absolute_symlinks=abs_links)
    base = next(iter(top)) if len(top) == 1 else None

    # 3. inventario de bases (descubierto en el vivo)
    live = live_databases(hermes_home)
    dbdir = backup_dir / "db-consistent"
    have = sorted(p.name for p in dbdir.glob("*.db")) if dbdir.is_dir() else []
    want = sorted(flat(x) for x in live)
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    det = []
    if missing:
        det.append("bases vivas SIN copia (no inventariadas): " + ", ".join(missing))
    if extra:
        det.append("copias sin base viva: " + ", ".join(extra))
    rep.add("db_inventory", Verdict.FAIL if missing else Verdict.PASS,
            "; ".join(det) or f"{len(live)} bases vivas, todas con copia consistente",
            live=live, copies=have)

    # 4-7. restauracion aislada
    if base is None:
        rep.add("restore", Verdict.FAIL, "sin raiz unica en el archivo")
        rep.finished_at = time.time()
        return rep
    work_dir.mkdir(parents=True, exist_ok=True)
    rdir = Path(work_dir) / f"restore-{backup_dir.name}-{os.getpid()}"
    if rdir.exists():
        shutil.rmtree(rdir)
    rdir.mkdir(mode=0o700)
    rep.restored_to = str(rdir)
    try:
        with tarfile.open(archive, "r:gz") as tf:
            # 'tar' y no 'data': el HERMES_HOME real tiene symlinks absolutos
            # legitimos (venvs del gestor de paquetes). 'tar' sigue negando
            # rutas absolutas y cualquier escritura fuera del destino.
            tf.extractall(rdir, filter="tar")
        home = rdir / base
        # bases: a su ruta, desde db-consistent (la fuente autoritativa)
        db_results, db_bad = {}, []
        for rel in live:
            src = dbdir / flat(rel)
            if not src.is_file():
                continue
            dst = home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            r = _sqlite_checks(dst)
            db_results[rel] = r
            if not r.get("ok"):
                db_bad.append(f"{rel}: {r.get('error') or r.get('integrity_check')}")
        for name in extra:   # copias sin base viva: tambien deben abrir
            r = _sqlite_checks(dbdir / name)
            db_results[name] = r
            if not r.get("ok"):
                db_bad.append(f"{name}: {r.get('error') or r.get('integrity_check')}")
        rep.add("restore_databases", Verdict.FAIL if (db_bad or not db_results) else Verdict.PASS,
                "; ".join(db_bad[:5]) or f"{len(db_results)} bases restauradas: quick_check+integrity_check ok",
                databases=db_results)

        # contenido declarado: plugins/ y scripts/
        cm = backup_dir / "content-manifest.sha256"
        if cm.is_file():
            exp = {}
            for line in cm.read_text().splitlines():
                if line.strip():
                    d, _, n = line.partition("  ")
                    exp[n] = d
            got = {}
            for d in CONTENT_DIRS:
                for p in (home / d).rglob("*") if (home / d).exists() else []:
                    if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                        got[str(p.relative_to(home))] = sha256_file(p)
            diff = [f"falta {n}" for n in exp if n not in got] + \
                   [f"sobra {n}" for n in got if n not in exp] + \
                   [f"distinto {n}" for n in exp if n in got and got[n] != exp[n]]
            rep.add("restore_content", Verdict.FAIL if diff else Verdict.PASS,
                    "; ".join(diff[:8]) or f"{len(exp)} ficheros de plugins/ y scripts/ identicos")
        else:
            rep.add("restore_content", Verdict.FAIL, "falta content-manifest.sha256")

        # permisos de secretos
        perms = backup_dir / "perms-esperados.txt"
        pbad = []
        if perms.is_file():
            for line in perms.read_text().splitlines():
                if not line.strip():
                    continue
                mode, _, name = line.partition(" ")
                p = home / name
                if not p.is_file():
                    pbad.append(f"{name} ausente")
                elif format(stat.S_IMODE(p.stat().st_mode), "o") != mode:
                    pbad.append(f"{name} {format(stat.S_IMODE(p.stat().st_mode), 'o')} != {mode}")
            rep.add("restore_permissions", Verdict.FAIL if pbad else Verdict.PASS,
                    "; ".join(pbad) or "permisos de secretos identicos")
        else:
            rep.add("restore_permissions", Verdict.FAIL, "falta perms-esperados.txt")

        miss = [c for c in critical if not (home / c).exists()]
        rep.add("restore_critical_paths", Verdict.FAIL if miss else Verdict.PASS,
                ("faltan: " + ", ".join(miss)) if miss else f"{len(tuple(critical))} rutas criticas presentes")
    except (tarfile.TarError, OSError, shutil.Error) as exc:
        rep.add("restore", Verdict.FAIL, f"restauracion fallida: {type(exc).__name__}: {exc}")
    finally:
        if not keep_restore:
            shutil.rmtree(rdir, ignore_errors=True)
            rep.restored_to = None
    rep.finished_at = time.time()
    return rep


def write_evidence(rep: Report, evidence_dir: Path) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    name = f"backup-{Path(rep.backup_dir).name}-{(rep.manifest_sha256 or 'nomanifest')[:12]}.json"
    p = evidence_dir / name
    p.write_text(json.dumps(rep.to_dict(), indent=2, default=str))
    os.chmod(p, 0o600)
    return p


def script_claims_verified(backup_dir: Path) -> Optional[bool]:
    """Lo que dice el propio script (solo para compararlo, nunca para decidir)."""
    try:
        st = json.loads((Path(backup_dir) / "state.json").read_text())
    except (OSError, ValueError):
        return None
    flags = st.get("backup") or {}
    return bool(st.get("backup_verified")) and bool(flags) and all(flags.values())
