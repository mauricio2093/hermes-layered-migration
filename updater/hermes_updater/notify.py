"""Notificaciones por el sistema EXISTENTE: ``hermes send`` (CLI oficial de
Hermes para scripts/cron; reutiliza las credenciales del gateway, sin LLM y
sin necesitar el gateway vivo). No hay bot alternativo.

Durabilidad: todo mensaje entra primero en la bandeja (SQLite) con una clave
de deduplicacion; el envio es un paso aparte con reintentos acotados. Si
Telegram falla, el informe queda guardado y se reintenta en la proxima
ejecucion: un fallo de notificacion nunca bloquea una recuperacion.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .config import Config
from .proc import Runner
from .store import Store


class Transport:
    name = "base"

    def send(self, body: str) -> tuple[bool, str]:
        raise NotImplementedError


class HermesSend(Transport):
    name = "hermes-send"

    def __init__(self, cfg: Config, runner: Runner):
        self.cfg, self.runner = cfg, runner

    def send(self, body: str) -> tuple[bool, str]:
        py = self.cfg.prod_python
        if not py.exists():
            return False, "sin venv productivo para `hermes send`"
        r = self.runner.run([str(py), str(self.cfg.prod_repo / "hermes_cli" / "main.py"), "send",
                             "--to", self.cfg.policy.notify_target, "--quiet", "--file", "-"],
                            stdin=body, timeout=90, env={"HERMES_HOME": str(self.cfg.hermes_home)})
        return r.ok, ("ok" if r.ok else f"rc={r.code} {r.tail(300)}")


class FileTransport(Transport):
    """Transporte simulado: escribe cada mensaje en la bandeja de ficheros."""
    name = "file"

    def __init__(self, outdir: Path, fail: bool = False):
        self.outdir, self.fail = outdir, fail

    def send(self, body: str) -> tuple[bool, str]:
        if self.fail:
            return False, "transporte simulado caido"
        self.outdir.mkdir(parents=True, exist_ok=True)
        n = len(list(self.outdir.glob("*.txt")))
        (self.outdir / f"{n:04d}-{int(time.time())}.txt").write_text(body)
        return True, "ok"


class NullTransport(Transport):
    name = "none"

    def send(self, body: str) -> tuple[bool, str]:
        return True, "descartado (notify_transport=none)"


def transport_for(cfg: Config, runner: Runner) -> Transport:
    t = cfg.policy.notify_transport
    if t == "hermes-send":
        return HermesSend(cfg, runner)
    if t == "file":
        return FileTransport(cfg.outbox_dir)
    return NullTransport()


def enqueue(store: Store, run_id: Optional[int], dedup_key: str, kind: str, body: str) -> bool:
    return store.enqueue(run_id, dedup_key, kind, body)


def flush(store: Store, transport: Transport, *, retries: int = 3, sleep=time.sleep) -> dict:
    sent, failed = 0, 0
    last_error = None
    for row in store.unsent():
        ok, why = False, ""
        for i in range(max(1, retries)):
            ok, why = transport.send(row["body"])
            if ok:
                break
            if i + 1 < retries:
                sleep(2 * (2 ** i))
        if ok:
            store.mark_sent(row["id"])
            sent += 1
        else:
            store.mark_failed(row["id"], why)
            failed += 1
            last_error = why
    return {"sent": sent, "failed": failed, "pending": len(store.unsent()), "last_error": last_error}
