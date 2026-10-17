"""Ejecucion de procesos: siempre argv (nunca shell), siempre con timeout, y
con reintentos acotados SOLO para lo que es transitorio (red).

`Runner` es inyectable: los tests sustituyen los comandos del host (systemctl,
hermes send, claude update...) sin tocar nada real.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

#: Marcas de un fallo de red/transitorio en la salida de git/curl/pip.
TRANSIENT_MARKERS = (
    "could not resolve host", "temporary failure in name resolution", "connection timed out",
    "connection reset", "network is unreachable", "failed to connect", "operation timed out",
    "the remote end hung up unexpectedly", "tls connection was non-properly terminated",
    "http 429", "http 502", "http 503", "http 504", "rpc failed", "early eof",
)

#: Salida maxima que se guarda como evidencia (la cola es lo que importa).
MAX_KEEP = 64 * 1024


@dataclass(frozen=True)
class Result:
    argv: tuple
    code: Optional[int]          # None = no llego a terminar (timeout / no se pudo lanzar)
    out: str
    duration_s: float
    timed_out: bool = False
    error: Optional[str] = None  # no se pudo lanzar

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out and self.error is None

    @property
    def transient(self) -> bool:
        low = self.out.lower()
        return self.timed_out or any(m in low for m in TRANSIENT_MARKERS)

    def tail(self, n: int = 2000) -> str:
        return self.out[-n:]


class Runner:
    """Lanza procesos de verdad. Grupo de procesos propio: un timeout mata al
    arbol entero, no solo al padre (pytest y pip dejan hijos)."""

    def run(self, argv: Sequence[str], *, timeout: float, cwd: Path | str | None = None,
            env: Mapping[str, str] | None = None, stdin: str | None = None,
            keep: int | None = MAX_KEEP) -> Result:
        """``keep``: cuanta salida conservar (la cola). ``None`` = toda: para
        salidas que se PARSEAN (ls-remote, rev-list), nunca se recortan."""
        argv = tuple(str(a) for a in argv)
        started = time.monotonic()
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=full_env,
                                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            return Result(argv, None, "", 0.0, error=f"{type(exc).__name__}: {exc}")
        try:
            out, _ = proc.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    out, _ = proc.communicate(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                out = b""
            timed_out = True
        text = (out or b"").decode("utf-8", errors="replace")
        if keep is not None:
            text = text[-keep:]
        return Result(argv, None if timed_out else proc.returncode, text,
                      round(time.monotonic() - started, 3), timed_out=timed_out)


def retry(fn: Callable[[], Result], *, attempts: int, backoff_s: float,
          sleep: Callable[[float], None] = time.sleep) -> Result:
    """Reintenta SOLO fallos transitorios, con espera exponencial y un tope.
    Un fallo no transitorio (p. ej. ref inexistente) vuelve en el primer intento."""
    last = fn()
    for i in range(1, max(1, attempts)):
        if last.ok or not last.transient:
            return last
        sleep(backoff_s * (2 ** (i - 1)))
        last = fn()
    return last
