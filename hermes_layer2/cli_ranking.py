"""CLI-facing layer over command usage: one cached order, never per keystroke.

The completer runs on every character typed. Opening SQLite, ranking and
closing for each of them would be wasteful even at this size, so the order is
computed once and reused until a command is actually run.

Everything here is fail-open. An adaptive menu is a convenience; it must never
be the reason a command does not run.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SURFACE = "cli"

_lock = threading.Lock()
_cached_order: Optional[Tuple[str, ...]] = None
_cached_key: Optional[Tuple[str, ...]] = None
_dirty = True
_reads = 0          # SQLite reads performed; tests assert the cache actually holds


def _db_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"


def _connect() -> Optional[sqlite3.Connection]:
    path = _db_path()
    if not path.exists():
        return None
    return sqlite3.connect(path, timeout=5)


def invalidate() -> None:
    """Drop the cached order; the next menu recomputes it once."""
    global _dirty
    with _lock:
        _dirty = True


def reset_for_tests() -> None:
    global _cached_order, _cached_key, _dirty, _reads
    with _lock:
        _cached_order, _cached_key, _dirty, _reads = None, None, True, 0


def sqlite_reads() -> int:
    """How many times the ranking has hit SQLite. Test instrumentation."""
    return _reads


def record_use(command: str) -> None:
    """Record one CLI execution of *command* and invalidate the cached order.

    Called from the single place the CLI dispatches a resolved command, so the
    name is already canonical and aliases merge into one row.
    """
    global _reads
    try:
        from hermes_layer2 import command_usage
        conn = _connect()
        if conn is None:
            return
        try:
            command_usage.record_command_use(conn, command, SURFACE)
        finally:
            conn.close()
        invalidate()
    except Exception as exc:                       # fail-open, always
        logger.debug("layer2: could not record use of /%s: %s", command, exc)


def ranked(names: Sequence[str]) -> List[str]:
    """*names* ordered by learned usage. Returns them unchanged on any failure.

    Recomputes only when a use was recorded or the set of commands changed --
    a different set means a plugin or skill appeared and the cached order no
    longer covers it.
    """
    global _cached_order, _cached_key, _dirty, _reads
    key = tuple(names)
    with _lock:
        if not _dirty and _cached_key == key and _cached_order is not None:
            return list(_cached_order)
    try:
        from hermes_layer2 import command_usage
        conn = _connect()
        if conn is None:
            return list(names)
        try:
            order = command_usage.rank_commands(conn, list(names), SURFACE)
        finally:
            conn.close()
        with _lock:
            _reads += 1
            _cached_order, _cached_key, _dirty = tuple(order), key, False
        return order
    except Exception as exc:                       # fail-open: original order
        logger.debug("layer2: could not rank commands: %s", exc)
        return list(names)
