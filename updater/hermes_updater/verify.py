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

#: Cobertura del tar de backup-hermes-home.sh (EXCLUDE_TOP / EXCLUDE_SUB). Copia
#: deliberada: este modulo no lee el script para decidir, y un test exige que
#: las dos definiciones coincidan.
EXCLUDE_TOP = ("backups", "rescue", "hermes-oficial-clean", "node", "lsp", "cache", "audio_cache")
EXCLUDE_SUB = ("hermes-agent/venv", "hermes-agent/.git", "hermes-agent/node_modules")
SQLITE_MAGIC = b"SQLite format 3\x00"

#: Sin esto, el restaurado no es un Hermes recuperable.
CRITICAL_PATHS = ("config.yaml", ".env", "auth.json", "SOUL.md", "memories", "skills", "cron",
                  "plugins", "scripts", "patches", "anchors", "voice-samples", "hermes-control", "costs")

CONTENT_DIRS = ("plugins", "scripts", "patches", "anchors", "voice-samples")
_LIVE_DB_SUFFIX = (".db", ".db-wal", ".db-shm")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def flat(rel: str) -> str:
    return rel.replace("/", "_")


def live_databases(hermes_home: Path) -> list[str]:
    """Toda base SQLite del Hermes vivo, POR CONTENIDO (cabecera), sobre la
    misma cobertura que el tar. Descubiertas aqui, no leidas del script: una
    base llamada x.sqlite o state.db.bak-N cuenta igual que state.db."""
    out = []
    for root, dirs, files in os.walk(hermes_home):
        rel = os.path.relpath(root, hermes_home)
        keep = []
        for d in dirs:
            r = d if rel == "." else f"{rel}/{d}"
            if (rel == "." and d in EXCLUDE_TOP) or r in EXCLUDE_SUB or d == "__pycache__":
                continue
            keep.append(d)
        dirs[:] = keep
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.islink(p) or os.path.getsize(p) < 100:
                    continue
                with open(p, "rb") as fh:
                    if fh.read(16) != SQLITE_MAGIC:
                        continue
            except OSError:
                continue
            out.append(os.path.relpath(p, hermes_home))
    return sorted(out)


def _sqlite_checks(p: Path, *, immutable: bool = False) -> dict:
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro{'&immutable=1' if immutable else ''}", uri=True)
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
                  keep_restore: bool = False, critical: Iterable[str] = CRITICAL_PATHS,
                  prod_repo: Optional[Path] = None, gateway_unit: str = "hermes-gateway.service") -> Report:
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
    members_bad, hot_dbs, top, abs_links, names = [], [], set(), 0, set()
    try:
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()       # lee el archivo ENTERO: detecta truncado
            for m in members:
                why = _safe_member(m)
                if why:
                    members_bad.append(f"{m.name}: {why}")
                if m.issym() and m.linkname.startswith("/"):
                    abs_links += 1
                names.add(m.name)
                if m.name.endswith(_LIVE_DB_SUFFIX):
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

    # 3. inventario de bases (descubierto en el vivo, por contenido)
    found = live_databases(hermes_home)
    dbdir = backup_dir / "db-consistent"
    have = sorted(p.name for p in dbdir.glob("*.db")) if dbdir.is_dir() else []
    base0 = next(iter(top)) if len(top) == 1 else ""
    live, cold, missing, hot_cold = [], [], [], []
    for rel in found:
        if flat(rel) in have:
            live.append(rel)                     # viva: copia consistente
        elif f"{base0}/{rel}" in names:
            wal = hermes_home / (rel + "-wal")
            if (wal.exists() and wal.stat().st_size > 0) or (hermes_home / (rel + "-journal")).exists():
                hot_cold.append(rel)             # viaja en el tar pero tiene escritor
            else:
                cold.append(rel)                 # archivada: viaja en el tar
        else:
            missing.append(rel)
    extra = sorted(set(have) - {flat(x) for x in live})
    det = []
    if missing:
        det.append("bases SQLite vivas que NO estan en el backup: " + ", ".join(missing))
    if hot_cold:
        det.append("bases con WAL activo copiadas en caliente por tar: " + ", ".join(hot_cold))
    if extra:
        det.append("copias sin base viva: " + ", ".join(extra))
    rep.add("db_inventory", Verdict.FAIL if (missing or hot_cold) else Verdict.PASS,
            "; ".join(det) or f"{len(found)} bases SQLite: {len(live)} con copia consistente, {len(cold)} archivadas en el tar",
            live=live, cold=cold, copies=have)

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
        live_placed = set(live)
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
        for rel in cold:     # archivadas: la copia RESTAURADA desde el tar debe abrir
            r = _sqlite_checks(home / rel, immutable=True)
            db_results[rel] = {**r, "kind": "archivada"}
            if not r.get("ok"):
                db_bad.append(f"{rel} (archivada): {r.get('error') or r.get('integrity_check')}")
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
                    rel_p = str(p.relative_to(home))
                    # fuera solo las bases vivas que ESTE verificador coloco desde
                    # db-consistent (y sus -wal/-shm), por ruta exacta, nunca por patron
                    if rel_p in live_placed or rel_p.removesuffix("-wal").removesuffix("-shm") in live_placed:
                        continue
                    if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                        got[str(p.relative_to(home))] = sha256_file(p)
            diff = [f"falta {n}" for n in exp if n not in got] + \
                   [f"sobra {n}" for n in got if n not in exp] + \
                   [f"distinto {n}" for n in exp if n in got and got[n] != exp[n]]
            rep.add("restore_content", Verdict.FAIL if diff else Verdict.PASS,
                    "; ".join(diff[:8]) or f"{len(exp)} ficheros declarados ({', '.join(CONTENT_DIRS)}) identicos")
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

        _check_host(rep, backup_dir / "host", prod_repo=prod_repo, gateway_unit=gateway_unit)
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


def _check_host(rep: Report, host: Path, *, prod_repo: Optional[Path], gateway_unit: str) -> None:
    """Lo necesario para reconstruir el Hermes que usa los datos: codigo
    propio, dependencias y servicio. Comprobado aqui, no creido del script."""
    import subprocess
    if not host.is_dir():
        rep.add("host_state", Verdict.FAIL, "el backup no trae host/ (codigo propio, venv, units)")
        return
    bad, ok = [], []
    unit = host / "systemd" / gateway_unit
    dropins = sorted((host / "systemd" / f"{gateway_unit}.d").glob("*.conf"))
    (ok if unit.is_file() else bad).append(f"unit {gateway_unit}" + ("" if unit.is_file() else " AUSENTE"))
    if dropins:
        ok.append(f"{len(dropins)} drop-in(s)")
    refs = {}
    if (host / "prod-refs.txt").is_file():
        for line in (host / "prod-refs.txt").read_text().splitlines():
            k, _, v = line.partition(" ")
            refs[k] = v.strip()
    bundle = host / "carried.bundle"
    if bundle.is_file():
        heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], capture_output=True, text=True, timeout=120)
        head_ok = heads.returncode == 0 and refs.get("HEAD", "?") in heads.stdout
        (ok if head_ok else bad).append("bundle con el HEAD registrado" if head_ok else "el bundle no contiene el HEAD registrado")
        if prod_repo is not None and Path(prod_repo, ".git").exists():
            v = subprocess.run(["git", "-C", str(prod_repo), "bundle", "verify", str(bundle)],
                               capture_output=True, text=True, timeout=300)
            (ok if v.returncode == 0 else bad).append("git bundle verify ok" if v.returncode == 0
                                                       else "git bundle verify FALLA")
    else:
        bad.append("sin carried.bundle (commits propios)")
    fr = host / "venv-freeze.txt"
    (ok if fr.is_file() and fr.stat().st_size > 0 else bad).append(
        f"pip freeze {len(fr.read_text().splitlines())} paquetes" if fr.is_file() and fr.stat().st_size > 0 else "sin pip freeze")
    wheels = sorted((host / "wheels").glob("*.whl")) if (host / "wheels").is_dir() else []
    installed = {}
    if prod_repo is not None:
        for du in Path(prod_repo, ".venv").glob("lib/python*/site-packages/*.dist-info/direct_url.json"):
            try:
                d = json.loads(du.read_text())
            except (OSError, ValueError):
                continue
            if d.get("url", "").startswith("file://") and "archive_info" in d:
                installed[Path(d["url"][7:]).name] = (d["archive_info"].get("hashes") or {}).get("sha256")
    for name, want in installed.items():
        w = host / "wheels" / name
        if not w.is_file():
            bad.append(f"falta el wheel local instalado {name}")
        elif want and sha256_file(w) != want:
            bad.append(f"wheel {name} distinto del instalado")
        else:
            ok.append(f"wheel {name} = sha256 instalado")
    for w in wheels:
        if w.name not in installed:
            ok.append(f"wheel {w.name} sha256 {sha256_file(w)[:12]} (no contrastado)")
    (ok if (host / "crontab.txt").is_file() else bad).append("crontab" if (host / "crontab.txt").is_file() else "sin crontab.txt")
    rep.add("host_state", Verdict.FAIL if bad else Verdict.PASS, "; ".join(bad) if bad else "; ".join(ok),
            wheels=[w.name for w in wheels])


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
