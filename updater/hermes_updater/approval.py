"""Autorizacion de produccion, atada a un candidato concreto.

El secreto TOTP es root-only (/etc/hermes-priv/totp.key) y solo lo verifica el
helper privilegiado de hermes-control. Por eso la aprobacion es un hecho que
ESCRIBE root y que este proceso (usuario) solo puede leer:

    Telegram  `! hermes-update aprobar <codigo>`
      -> hermes-control: grant + [Autorizar] + codigo TOTP   (flujo existente)
      -> sudo -n hermes-priv-helper update-approve <codigo>  (verifica TOTP)
      -> /var/lib/hermes-update-approvals/<codigo>.json (root:root 0644)

El <codigo> es el sha256 (24 hex) del payload canonico del candidato:
version origen, destino, commit candidato, backup verificado, resultado de
tests, revision IA, caducidad y un nonce. Otro candidato -> otro codigo: una
aprobacion vieja no sirve para nada distinto.

Un proceso del usuario (incluido un LLM con terminal) no puede fabricarla: no
puede escribir como root ni conoce el TOTP. Un boton de Telegram tampoco basta:
sin codigo no hay fichero.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Optional

CODE_LEN = 24


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def candidate_code(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode()).hexdigest()[:CODE_LEN]


def make_payload(*, source_commit: str, source_version: str, target_tag: str, target_commit: str,
                 candidate_commit: str, candidate_branch: str, staging_repo: str, staging_venv: str,
                 lock_sha256: str, backup_id: str, preupdate_dir: str, preupdate_manifest: str,
                 tests: dict, review: str, created_at: float, ttl_s: float) -> dict:
    return {
        "v": 1,
        "source": {"commit": source_commit, "version": source_version},
        "target": {"tag": target_tag, "commit": target_commit},
        "candidate": {"commit": candidate_commit, "branch": candidate_branch, "repo": staging_repo,
                      "venv": staging_venv, "lock_sha256": lock_sha256},
        "backup": {"id": backup_id, "preupdate_dir": preupdate_dir, "manifest_sha256": preupdate_manifest},
        "tests": tests,
        "review": review,
        "created_at": round(created_at, 3),
        "expires_at": round(created_at + ttl_s, 3),
        "nonce": secrets.token_hex(8),
    }


def _owner_ok(st: os.stat_result, owner_uid: int) -> bool:
    return st.st_uid == owner_uid and not (st.st_mode & 0o022)


def check(approvals_dir: Path, payload: dict, *, owner_uid: int = 0, approval_ttl_s: float = 3600,
          now: Optional[float] = None) -> tuple[bool, str, Optional[dict]]:
    """(valida, motivo, contenido). No consume: eso lo hace el store, una vez."""
    now = time.time() if now is None else now
    code = candidate_code(payload)
    if now > payload["expires_at"]:
        return False, "el candidato ha caducado; hay que prepararlo de nuevo", None
    try:
        dst = os.lstat(approvals_dir)
    except FileNotFoundError:
        return False, f"no hay aprobaciones ({approvals_dir} no existe: helper sin la operacion update-approve)", None
    if not stat.S_ISDIR(dst.st_mode) or not _owner_ok(dst, owner_uid):
        return False, f"{approvals_dir} no es un directorio de root protegido: se ignora", None
    p = approvals_dir / f"{code}.json"
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False, f"sin aprobacion para el codigo {code}", None
    if not stat.S_ISREG(st.st_mode) or not _owner_ok(st, owner_uid):
        return False, "la aprobacion no es un fichero de root protegido: se ignora", None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return False, "aprobacion ilegible", None
    if data.get("code") != code:
        return False, "la aprobacion es de otro candidato", data
    if not data.get("factor"):
        return False, "la aprobacion no dice que segundo factor se verifico", data
    approved_at = float(data.get("approved_at") or 0)
    expires_at = float(data.get("expires_at") or 0)
    if approved_at < payload["created_at"]:
        return False, "la aprobacion es anterior al candidato", data
    if now > min(expires_at, approved_at + approval_ttl_s):
        return False, "la aprobacion ha caducado", data
    if approved_at > now + 300:
        return False, "la aprobacion esta fechada en el futuro", data
    return True, f"aprobacion valida ({data['factor']}; registrada por el helper a las {time.strftime('%H:%M:%S', time.localtime(approved_at))})", data


def approve_command(code: str) -> str:
    return f"! hermes-update aprobar {code}"
