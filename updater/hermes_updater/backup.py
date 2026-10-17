"""Backups: politica A (diario de mantenimiento) y B (previo a actualizacion).

A — reutiliza ``backup-hermes-home.sh`` tal cual (HERMES_HOME, SQLite con
    Connection.backup, declaraciones, restore propio). El resultado solo se
    da por bueno si ADEMAS pasa ``verify.verify_backup`` (independiente).

B — previo a cualquier cambio de Hermes. Siempre NUEVO (nunca uno viejo):
      * un backup A fresco + su verificacion independiente
      * el ZIP oficial de Hermes (``hermes backup``), verificado con zipfile
      * estado de codigo: HEAD/rama/refs, bundle de los commits propios
        (verificado con ``git bundle verify``), parches format-patch,
        ``git status``, ``pip freeze`` del venv, units/drop-ins de systemd,
        crontab, versiones de herramientas
      * MANIFEST.sha256 del conjunto

Retencion: solo se podan backups que creo este orquestador, y nunca el ultimo
verificado.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import verify
from .config import Config
from .proc import Runner
from .status import Verdict, worst


@dataclass
class BackupResult:
    kind: str                         # daily | pre-update
    verdict: Verdict = Verdict.INSUFFICIENT_DATA
    backup_dir: Optional[str] = None  # backup A
    bundle_dir: Optional[str] = None  # directorio pre-update (B)
    script_claim: Optional[bool] = None
    verification: Optional[dict] = None
    evidence_path: Optional[str] = None
    parts: dict = field(default_factory=dict)
    detail: str = ""
    log_tail: str = ""

    @property
    def backup_id(self) -> Optional[str]:
        if not self.backup_dir:
            return None
        man = (self.verification or {}).get("manifest_sha256") or ""
        return f"{Path(self.backup_dir).name}:{man[:12]}"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["verdict"] = self.verdict.value
        d["backup_id"] = self.backup_id
        return d


def _new_dirs(root: Path, before: set) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name not in before)


def run_backup_a(cfg: Config, runner: Runner, *, dest_root: Optional[Path] = None,
                 kind: str = "daily") -> BackupResult:
    res = BackupResult(kind)
    dest_root = Path(dest_root or cfg.daily_backups)
    dest_root.mkdir(parents=True, exist_ok=True)
    if not cfg.backup_script.is_file():
        res.verdict = Verdict.BLOCKED
        res.detail = f"no existe el script de backup {cfg.backup_script}"
        return res
    before = {p.name for p in dest_root.iterdir()} if dest_root.is_dir() else set()
    tmp = cfg.work_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    r = runner.run(["bash", str(cfg.backup_script)], timeout=cfg.policy.backup_timeout_s,
                   env={"HERMES_HOME": str(cfg.hermes_home), "BACKUP_ROOT": str(dest_root),
                        # el restore temporal del script, a disco: /tmp es tmpfs (RAM)
                        "TMPDIR": str(tmp)})
    res.log_tail = r.tail(4000)
    new = _new_dirs(dest_root, before)
    if not new:
        res.verdict = Verdict.FAIL
        res.detail = "el script no creo ningun backup" + (" (timeout)" if r.timed_out else "")
        return res
    bdir = new[-1]
    res.backup_dir = str(bdir)
    res.script_claim = verify.script_claims_verified(bdir)
    rep = verify.verify_backup(bdir, hermes_home=cfg.hermes_home, work_dir=cfg.work_dir)
    res.verification = rep.to_dict()
    res.evidence_path = str(verify.write_evidence(rep, cfg.evidence_dir))
    # Las dos cosas: el script dice que si Y la verificacion independiente pasa.
    if not r.ok or res.script_claim is not True:
        res.verdict = Verdict.FAIL
        res.detail = _script_failure(r.out) or f"el script termino con codigo {r.code}"
    else:
        res.verdict = rep.verdict
        res.detail = "verificado por el script y por el verificador independiente" \
            if rep.verdict == Verdict.PASS else "el script dice OK pero la verificacion independiente no"
    if rep.verdict != Verdict.PASS:
        fails = [f"{k}: {v['detail']}" for k, v in rep.checks.items() if v["verdict"] != "PASS"]
        res.detail += " | " + "; ".join(fails[:4])
    return res


def _script_failure(out: str) -> str:
    """Las lineas de ABORT/✗ del script: dicen exactamente que falta declarar."""
    lines = [l.strip() for l in out.splitlines() if "ABORT" in l or l.strip().startswith(("✗", "falta:", "no declarado:"))]
    return " | ".join(dict.fromkeys(lines))[:1500]


# --- B: previo a actualizacion -------------------------------------------------------

def _w(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def capture_code_state(cfg: Config, runner: Runner, out: Path, *, upstream_ref: Optional[str]) -> dict:
    """Todo lo necesario para devolver el CODIGO y el SERVICIO a este punto."""
    git = lambda *a, **k: runner.run(["git", "-C", str(cfg.prod_repo), *a], timeout=k.get("t", 300),
                                     env={"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}, keep=None)
    parts: dict = {}
    head = git("rev-parse", "HEAD").out.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").out.strip()
    _w(out / "refs.txt", f"HEAD {head}\nbranch {branch}\nupstream_ref {upstream_ref}\n")
    status = git("status", "--porcelain")
    _w(out / "git-status.txt", status.out)
    _w(out / "git-stash.txt", git("stash", "list").out)
    parts["head"] = head
    parts["branch"] = branch
    parts["dirty"] = bool(status.out.strip())

    # commits propios: bundle con prerrequisitos upstream (publicos) + parches
    rng = f"{upstream_ref}..{branch}" if upstream_ref else branch
    b = git("bundle", "create", str(out / "carried.bundle"), rng, t=900)
    v = git("bundle", "verify", str(out / "carried.bundle"), t=300) if b.ok else b
    parts["bundle"] = {"range": rng, "created": b.ok, "verified": v.ok,
                       "detail": (v.tail(300) if not v.ok else "git bundle verify ok")}
    pdir = out / "carried-patches"
    pdir.mkdir(exist_ok=True)
    fp = git("format-patch", "--no-merges", "-o", str(pdir), rng, t=300) if upstream_ref else None
    parts["patches"] = len(list(pdir.glob("*.patch"))) if fp is not None and fp.ok else None

    # dependencias
    if cfg.prod_python.exists():
        fr = runner.run([str(cfg.prod_python), "-m", "pip", "freeze", "--all"], timeout=180, keep=None)
        _w(out / "venv-freeze.txt", fr.out)
        parts["venv_freeze"] = fr.ok and bool(fr.out.strip())
    else:
        parts["venv_freeze"] = False

    # servicio: unit + drop-ins (el drop-in 10-legacy-venv-launch.conf es lo que lo mantiene vivo)
    sdir = out / "systemd"
    sdir.mkdir(exist_ok=True)
    unit = cfg.systemd_user_dir / cfg.gateway_unit
    copied = []
    for src in [unit, *sorted((cfg.systemd_user_dir / f"{cfg.gateway_unit}.d").glob("*.conf"))]:
        if src.is_file():
            rel = src.relative_to(cfg.systemd_user_dir)
            dst = sdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(str(rel))
    parts["systemd"] = copied
    cron = runner.run(["crontab", "-l"], timeout=30)
    _w(out / "crontab.txt", cron.out if cron.ok else f"# crontab -l: {cron.tail(200)}")
    return parts


def run_backup_b_zip(cfg: Config, runner: Runner, out: Path) -> dict:
    """El ZIP oficial de Hermes, desde el venv productivo. Verificado aparte."""
    if not cfg.prod_python.exists():
        return {"verdict": Verdict.BLOCKED.value, "detail": "sin venv productivo para `hermes backup`"}
    z = out / "hermes-backup-B.zip"
    r = runner.run([str(cfg.prod_python), str(cfg.prod_repo / "hermes_cli" / "main.py"), "backup",
                    "-o", str(z), "-k", "0"], timeout=cfg.policy.backup_timeout_s,
                   env={"HERMES_HOME": str(cfg.hermes_home)})
    if not r.ok or not z.is_file():
        return {"verdict": Verdict.FAIL.value, "detail": f"hermes backup fallo: {r.tail(300)}"}
    try:
        with zipfile.ZipFile(z) as zf:
            badfile = zf.testzip()
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        return {"verdict": Verdict.FAIL.value, "detail": f"zip corrupto: {exc}"}
    if badfile:
        return {"verdict": Verdict.FAIL.value, "detail": f"zip con CRC malo en {badfile}"}
    want = ("config.yaml", "state.db")
    missing = [w for w in want if not any(n.endswith(w) for n in names)]
    os.chmod(z, 0o600)
    return {"verdict": (Verdict.FAIL if missing else Verdict.PASS).value,
            "detail": ("falta en el zip: " + ", ".join(missing)) if missing else f"{len(names)} entradas, CRC ok",
            "size_bytes": z.stat().st_size}


def write_manifest(d: Path) -> str:
    lines = []
    for p in sorted(x for x in d.rglob("*") if x.is_file() and x.name != "MANIFEST.sha256"):
        lines.append(f"{verify.sha256_file(p)}  ./{p.relative_to(d)}")
    _w(d / "MANIFEST.sha256", "\n".join(lines) + "\n")
    return verify.sha256_file(d / "MANIFEST.sha256")


def run_preupdate(cfg: Config, runner: Runner, *, upstream_ref: Optional[str], versions: dict,
                  ts: Optional[str] = None) -> BackupResult:
    res = BackupResult("pre-update")
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    out = Path(cfg.preupdate_backups) / ts
    out.mkdir(parents=True, exist_ok=False)
    os.chmod(out, 0o700)
    res.bundle_dir = str(out)

    a = run_backup_a(cfg, runner, kind="pre-update")      # fresco, en independiente/
    res.backup_dir, res.verification = a.backup_dir, a.verification
    res.script_claim, res.evidence_path = a.script_claim, a.evidence_path
    res.parts["backup_a"] = {"verdict": a.verdict.value, "detail": a.detail, "dir": a.backup_dir}
    if a.verdict != Verdict.PASS:
        res.verdict = Verdict.FAIL
        res.detail = "backup A fallido: " + a.detail
        res.log_tail = a.log_tail
        return res
    _w(out / "backup-a.txt", f"{a.backup_dir}\n{a.backup_id}\n")

    code = capture_code_state(cfg, runner, out, upstream_ref=upstream_ref)
    res.parts["code"] = code
    _w(out / "tool-versions.json", json.dumps(versions, indent=2, default=str))
    if cfg.policy.backup_b:
        res.parts["backup_b_zip"] = run_backup_b_zip(cfg, runner, out)
    else:
        res.parts["backup_b_zip"] = {"verdict": Verdict.SKIPPED.value, "detail": "desactivado por politica"}
    res.parts["manifest_sha256"] = write_manifest(out)

    verdicts = [Verdict.PASS if a.verdict == Verdict.PASS else Verdict.FAIL,
                Verdict.PASS if code["bundle"]["verified"] else Verdict.FAIL,
                Verdict.PASS if code.get("venv_freeze") else Verdict.FAIL,
                Verdict.PASS if code.get("systemd") else Verdict.FAIL,
                Verdict.FAIL if code.get("dirty") else Verdict.PASS,
                Verdict(res.parts["backup_b_zip"]["verdict"]) if cfg.policy.backup_b else Verdict.PASS]
    res.verdict = worst(verdicts)
    probs = []
    if not code["bundle"]["verified"]:
        probs.append("bundle de commits propios no verificado")
    if not code.get("venv_freeze"):
        probs.append("sin pip freeze del venv")
    if not code.get("systemd"):
        probs.append("sin unit de systemd")
    if code.get("dirty"):
        probs.append("checkout productivo con cambios sin commit")
    if res.parts["backup_b_zip"]["verdict"] not in ("PASS", "SKIPPED"):
        probs.append("zip B: " + res.parts["backup_b_zip"]["detail"])
    res.detail = "; ".join(probs) or "A verificado + B + estado de codigo y servicio"
    return res


def verify_preupdate_still_valid(bundle_dir: Path) -> tuple[bool, str]:
    """Re-verificacion rapida justo antes del despliegue: el MANIFEST del
    directorio pre-update sigue cuadrando (nadie lo toco ni lo borro)."""
    man = Path(bundle_dir) / "MANIFEST.sha256"
    if not man.is_file():
        return False, "falta MANIFEST.sha256 del backup pre-update"
    for line in man.read_text().splitlines():
        if not line.strip():
            continue
        d, _, n = line.partition("  ")
        p = Path(bundle_dir) / n
        if not p.is_file() or verify.sha256_file(p) != d:
            return False, f"backup pre-update alterado: {n}"
    return True, "MANIFEST del backup pre-update intacto"


def prune(root: Path, owned: set, keep: int) -> list[str]:
    """Borra backups PROPIOS (creados por el orquestador) mas alla de los
    ``keep`` verificados mas recientes. Nunca el ultimo verificado, nunca uno
    ajeno, nunca uno sin veredicto del script."""
    if not root.is_dir():
        return []
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    verified = [p for p in dirs if verify.script_claims_verified(p)]
    if not verified:
        return []
    protect = {p.name for p in verified[-max(1, keep):]}
    removed = []
    for p in dirs:
        if p.name in protect or p.name not in owned:
            continue
        claim = verify.script_claims_verified(p)
        if claim is None:
            continue
        if claim is False and p.name > verified[-1].name:
            continue      # un fallo posterior al ultimo bueno es evidencia: se queda
        shutil.rmtree(p)
        removed.append(p.name)
    return removed
