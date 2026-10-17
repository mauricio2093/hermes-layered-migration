"""Actualizaciones auxiliares: Claude Code, Codex y OpenCode.

Independientes entre si y de Hermes: un fallo aqui nunca empuja ni bloquea
la actualizacion de Hermes, y cada producto tiene su propio resultado.

Por producto:
  1. version actual + punto de recuperacion (symlink de version o copia del binario)
  2. no en uso critico (y nunca actualizar Claude desde dentro de Claude)
  3. mecanismo OFICIAL segun el metodo instalado (claude update / codex update /
     opencode upgrade --method curl); sin sudo, sin gestores globales
  4. codigo de salida + version posterior + smoke (--version y --help)
  5. si algo falla: se restaura el punto de recuperacion y se comprueba
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .proc import Runner
from .status import Verdict
from .versions import ProductVersion, semver


@dataclass
class AuxResult:
    product: str
    verdict: Verdict = Verdict.SKIPPED
    before: Optional[str] = None
    after: Optional[str] = None
    recovery: dict = field(default_factory=dict)
    rolled_back: Optional[bool] = None
    detail: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["verdict"] = self.verdict.value
        return d


def inside_claude() -> bool:
    return any(os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"))


def processes_using(realpath: str, proc_root: Path = Path("/proc")) -> int:
    n = 0
    for d in proc_root.iterdir() if proc_root.is_dir() else []:
        if not d.name.isdigit():
            continue
        try:
            if os.path.realpath(d / "exe") == realpath:
                n += 1
        except OSError:
            continue
    return n


def recovery_point(pv: ProductVersion, store_dir: Path) -> dict:
    binary = Path(pv.extra.get("binary", ""))
    real = pv.extra.get("realpath")
    if pv.product == "claude":
        # ~/.local/bin/claude -> ~/.local/share/claude/versions/X (el viejo se conserva)
        return {"kind": "symlink", "link": str(binary), "target": os.readlink(binary) if binary.is_symlink() else None}
    if pv.product == "codex":
        cur = Path.home() / ".codex" / "packages" / "standalone" / "current"
        if cur.is_symlink():
            return {"kind": "symlink", "link": str(cur), "target": os.readlink(cur)}
        return {"kind": "none"}
    if pv.product == "opencode" and real:
        store_dir.mkdir(parents=True, exist_ok=True)
        dst = store_dir / f"opencode-{pv.installed}"
        if not dst.exists():
            shutil.copy2(real, dst)
        return {"kind": "binary", "path": real, "copy": str(dst)}
    return {"kind": "none"}


def restore(rp: dict) -> bool:
    try:
        if rp.get("kind") == "symlink" and rp.get("target"):
            link = Path(rp["link"])
            tmp = link.with_name(link.name + ".hermes-updater-tmp")
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(rp["target"], tmp)
            os.replace(tmp, link)                    # atomico
            return True
        if rp.get("kind") == "binary":
            tmp = Path(rp["path"] + ".hermes-updater-tmp")
            shutil.copy2(rp["copy"], tmp)
            os.replace(tmp, rp["path"])
            return True
    except OSError:
        return False
    return False


def update_one(cfg: Config, runner: Runner, pv: ProductVersion, *,
               version_of: Callable[[], Optional[str]], in_use: Callable[[str], int] = processes_using,
               allow_in_use: tuple = ("claude", "codex")) -> AuxResult:
    res = AuxResult(pv.product, before=pv.installed)
    if pv.verdict != Verdict.PASS or pv.update_available is None:
        res.verdict, res.detail = Verdict.INSUFFICIENT_DATA, f"version no determinada: {pv.detail}"
        return res
    if not pv.update_available:
        res.verdict, res.detail = Verdict.PASS, "actualizado; nada que hacer"
        res.after = pv.installed
        return res
    if pv.product not in cfg.policy.aux_auto_update:
        res.verdict, res.detail = Verdict.DEFERRED, "actualizacion automatica desactivada por politica"
        return res
    if pv.major_jump and not cfg.policy.aux_allow_major:
        res.verdict, res.detail = Verdict.DEFERRED, f"salto de version mayor {pv.installed} -> {pv.available}: requiere aprobacion"
        return res
    if not pv.update_command:
        res.verdict, res.detail = Verdict.DEFERRED, f"metodo de instalacion sin mecanismo automatizable ({pv.method})"
        return res
    if pv.product == "claude" and inside_claude():
        res.verdict, res.detail = Verdict.DEFERRED, "no se actualiza Claude Code desde una sesion de Claude"
        return res
    users = in_use(pv.extra.get("realpath", ""))
    if users and pv.product not in allow_in_use:
        # Claude y Codex instalan cada version en su directorio y cambian un
        # symlink: los procesos vivos siguen con su binario. OpenCode reemplaza
        # el binario: con procesos vivos se pospone.
        res.verdict, res.detail = Verdict.DEFERRED, f"en uso por {users} proceso(s); se reintenta en la proxima revision"
        return res

    rp = recovery_point(pv, cfg.state_dir / "aux-recovery")
    res.recovery = rp
    cmd = list(pv.update_command)
    cmd[0] = pv.extra.get("binary", cmd[0])
    r = runner.run(cmd, timeout=900, env={"CI": "1", "NO_COLOR": "1"})
    after = version_of()
    res.after = after
    smoke_ok = False
    if after:
        b = pv.extra.get("binary", pv.product)
        smoke_ok = runner.run([b, "--version"], timeout=60).ok and runner.run([b, "--help"], timeout=60).ok
    upgraded = bool(after and semver(after) and semver(after) >= semver(pv.available or "0.0.0"))
    if r.ok and upgraded and smoke_ok:
        res.verdict, res.detail = Verdict.PASS, f"{pv.installed} -> {after} ({' '.join(pv.update_command)}), smoke ok"
        return res
    # Fallo o actualizacion parcial: volver al punto de recuperacion.
    why = (f"rc={r.code}{' timeout' if r.timed_out else ''}, version tras: {after}, smoke={'ok' if smoke_ok else 'FALLO'}")
    changed = after != pv.installed
    if changed or not smoke_ok:
        res.rolled_back = restore(rp)
        back = version_of()
        res.detail = f"FALLO ({why}); restaurado {'OK' if back == pv.installed else 'NO'} -> {back}"
        if back != pv.installed:
            res.rolled_back = False
    else:
        res.detail = f"FALLO ({why}); sin cambios en la instalacion"
    res.verdict = Verdict.FAIL
    return res
