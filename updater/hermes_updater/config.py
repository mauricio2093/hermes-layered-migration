"""Rutas y politica. Todo con valores por defecto del host real y sobrescribible
por un TOML (``$HERMES_UPDATER_CONFIG`` o ``<state_dir>/config.toml``) para
tests y para cambiar de opinion sin tocar codigo.

El estado del orquestador vive FUERA de ``$HERMES_HOME`` (en
``~/.local/state/hermes-updater``): una base SQLite nueva dentro de
``~/.hermes`` haria fallar -- con razon -- la deteccion de bases no declaradas
del backup independiente.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping

HOME = Path(os.environ.get("HOME", str(Path.home())))


def _p(v) -> Path:
    return Path(os.path.expanduser(str(v)))


@dataclass(frozen=True)
class Policy:
    # recursos: por debajo de esto no se prepara ni se actualiza nada
    min_free_disk_gb: float = 5.0
    min_avail_mem_mb: int = 600
    # red
    net_timeout_s: float = 20.0
    net_retries: int = 3
    net_backoff_s: float = 2.0
    # backups
    daily_backup: bool = True
    keep_daily_backups: int = 7          # nunca borra el ultimo verificado
    keep_preupdate_backups: int = 3
    backup_timeout_s: int = 1800
    backup_b: bool = True                # zip oficial `hermes backup` antes de actualizar
    # actualizaciones de Hermes
    hermes_target_policy: str = "stable"  # solo tags vX.Y.Z (sin rc, sin main)
    retry_blocked_after_h: int = 72       # no reintentar el mismo bloqueo cada dia
    candidate_ttl_h: int = 72
    approval_ttl_s: int = 3600
    test_timeout_s: int = 3600
    test_memory_max: str = "1800M"
    # IA (auditor, nunca autoridad)
    ai_provider: str = "claude"           # claude (sin herramientas) | codex | none
    ai_timeout_s: int = 900
    ai_required: bool = False             # True -> sin IA el candidato queda BLOCKED; False -> DEFERRED
    # herramientas auxiliares
    aux_auto_update: tuple = ("claude", "codex", "opencode")
    aux_allow_major: bool = False
    # despliegue
    service_ready_timeout_s: int = 120
    service_stable_s: int = 20
    # notificaciones
    notify_transport: str = "hermes-send"  # hermes-send | file | none
    notify_target: str = "telegram"
    notify_retries: int = 3


@dataclass(frozen=True)
class Config:
    hermes_home: Path = HOME / ".hermes"
    prod_repo: Path = HOME / ".hermes" / "hermes-oficial-clean"
    prod_branch: str = "feat/layer2-schema"
    upstream_url: str = "https://github.com/NousResearch/hermes-agent.git"
    state_dir: Path = HOME / ".local" / "state" / "hermes-updater"
    backup_script: Path = HOME / ".hermes" / "scripts" / "backup-hermes-home.sh"
    daily_backups: Path = HOME / ".hermes" / "backups" / "independiente"
    preupdate_backups: Path = HOME / ".hermes" / "backups" / "pre-update"
    gateway_unit: str = "hermes-gateway.service"
    systemd_user_dir: Path = HOME / ".config" / "systemd" / "user"
    #: Aprobaciones escritas por el helper root (hermes-priv-helper update-approve).
    approvals_dir: Path = Path("/var/lib/hermes-update-approvals")
    #: Dueño exigido de las aprobaciones (0 = root). Los tests lo cambian.
    approvals_owner_uid: int = 0
    hermes_control_repo: Path = HOME / "repos" / "hermes-control"
    policy: Policy = field(default_factory=Policy)

    # derivadas
    @property
    def db_path(self) -> Path:
        return self.state_dir / "updater.db"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "updater.lock"

    @property
    def work_dir(self) -> Path:
        return self.state_dir / "work"

    @property
    def outbox_dir(self) -> Path:
        return self.state_dir / "outbox"

    @property
    def evidence_dir(self) -> Path:
        return self.state_dir / "evidence"

    @property
    def resolutions_dir(self) -> Path:
        """``rr-cache`` de git rerere con resoluciones de conflicto REVISADAS.
        Solo se reutilizan si el conflicto es identico al revisado."""
        return self.state_dir / "resolutions" / "rr-cache"

    @property
    def prod_python(self) -> Path:
        return self.prod_repo / ".venv" / "bin" / "python"


_PATH_FIELDS = {f.name for f in fields(Config) if f.type in ("Path", Path)}


def _coerce(cls, data: Mapping[str, Any]):
    out = {}
    names = {f.name: f for f in fields(cls)}
    for k, v in data.items():
        if k not in names:
            raise ValueError(f"clave de configuracion desconocida: {k}")
        if cls is Config and k in _PATH_FIELDS:
            v = _p(v)
        if isinstance(v, list):
            v = tuple(v)
        out[k] = v
    return out


def load(path: Path | None = None, **overrides) -> Config:
    cfg = Config()
    env_path = os.environ.get("HERMES_UPDATER_CONFIG")
    candidates = [_p(path)] if path else ([_p(env_path)] if env_path else [cfg.state_dir / "config.toml"])
    for c in candidates:
        if c and c.exists():
            raw = tomllib.loads(c.read_text())
            pol = raw.pop("policy", {})
            cfg = replace(cfg, **_coerce(Config, raw))
            cfg = replace(cfg, policy=replace(cfg.policy, **_coerce(Policy, pol)))
    pol_over = overrides.pop("policy", None)
    if overrides:
        cfg = replace(cfg, **_coerce(Config, overrides))
    if pol_over:
        cfg = replace(cfg, policy=replace(cfg.policy, **_coerce(Policy, pol_over)))
    return cfg


def ensure_dirs(cfg: Config) -> None:
    for d in (cfg.state_dir, cfg.work_dir, cfg.outbox_dir, cfg.evidence_dir):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
