"""Persistent frequency + recency for slash commands.

Two numbers in one. A plain counter would leave a command you hammered six
months ago permanently above one you use daily, so the score decays
exponentially: every use adds 1 to whatever the old score has decayed to.

The decay is applied **on read as well as on write**. That is the part that is
easy to get wrong: decaying only on use leaves an abandoned command frozen at
its last value forever, because nothing ever touches its row again. A command
at 100 points, unused for six months, must read as ~1.6 -- not 100.

Storage and scoring only. Ordering a menu is the caller's business.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence

from hermes_layer2.state_schema import (
    Layer2MigrationError, migrate_component, register_component)

logger = logging.getLogger(__name__)

COMPONENT = "command_usage"
TABLE = "layer2_command_usage"

#: Half-life of the score. Defined once; every decay reads it from here.
HALF_LIFE_DAYS = 30.0
HALF_LIFE_SECONDS = HALF_LIFE_DAYS * 86400.0

_V1_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    command          TEXT NOT NULL,
    surface          TEXT NOT NULL,
    use_count        INTEGER NOT NULL DEFAULT 0,
    score            REAL NOT NULL DEFAULT 0,
    last_used_at     INTEGER NOT NULL,
    score_updated_at INTEGER NOT NULL,
    PRIMARY KEY (command, surface)
)
"""


def _migrate_v1(cur: sqlite3.Cursor) -> None:
    """v1: the usage table. No index: at a few hundred rows the primary key is
    the whole working set and a scan is cheaper than maintaining anything else."""
    cur.execute(_V1_DDL)


register_component(COMPONENT, {1: _migrate_v1})


class CommandUsage(NamedTuple):
    """A command's usage, with the score already decayed to the asked-for time."""

    command: str
    surface: str
    use_count: int
    effective_score: float
    last_used_at: int
    score_updated_at: int


def _now(now: Optional[float]) -> int:
    return int(time.time() if now is None else now)


def _surface(surface: str) -> str:
    """Any surface name is allowed, but it must be a real, non-empty one.

    Deliberately not an enum: a new platform should not need a migration. It is
    validated rather than free-form so a typo becomes a separate row silently.
    """
    if not isinstance(surface, str) or not surface.strip():
        raise ValueError("surface must be a non-empty string")
    cleaned = surface.strip().lower()
    if not cleaned.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"surface must be alphanumeric: {surface!r}")
    return cleaned


def _command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    return command.strip().lstrip("/")


def decay(score: float, elapsed_seconds: float) -> float:
    """*score* decayed over *elapsed_seconds*.

    Negative elapsed is clamped to zero: a clock that jumps backwards would
    otherwise *raise* the score, rewarding a wrong clock with permanent
    priority.
    """
    if elapsed_seconds <= 0:
        return float(score)
    return float(score) * (0.5 ** (elapsed_seconds / HALF_LIFE_SECONDS))


def ensure_ready(conn: sqlite3.Connection) -> int:
    """Bring the component to its current version. Returns that version."""
    return migrate_component(conn, COMPONENT)


def record_command_use(conn: sqlite3.Connection, command: str, surface: str,
                       now: Optional[float] = None) -> CommandUsage:
    """Register one use: decay what was stored, then add 1.

    Read and write happen inside one IMMEDIATE transaction rather than a clever
    UPSERT expression, so two concurrent uses cannot both read the same old
    score and lose one of the increments.
    """
    cmd, surf, ts = _command(command), _surface(surface), _now(now)
    ensure_ready(conn)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute(
            f"SELECT use_count, score, score_updated_at FROM {TABLE} "
            "WHERE command = ? AND surface = ?", (cmd, surf)).fetchone()
        if row is None:
            use_count, score = 1, 1.0
        else:
            use_count = int(row[0]) + 1
            score = decay(float(row[1]), ts - int(row[2])) + 1.0
        cur.execute(
            f"INSERT INTO {TABLE} (command, surface, use_count, score, last_used_at, score_updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(command, surface) DO UPDATE SET "
            "use_count = excluded.use_count, score = excluded.score, "
            "last_used_at = excluded.last_used_at, score_updated_at = excluded.score_updated_at",
            (cmd, surf, use_count, score, ts, ts))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return CommandUsage(cmd, surf, use_count, score, ts, ts)


def get_command_usage(conn: sqlite3.Connection, command: str, surface: str,
                      now: Optional[float] = None) -> Optional[CommandUsage]:
    """Usage for one command, decayed to *now*. ``None`` when it has no history.

    The decayed value is **not** written back: a read must not change the row,
    or merely opening a menu would rewrite the whole table.
    """
    cmd, surf, ts = _command(command), _surface(surface), _now(now)
    try:
        row = conn.execute(
            f"SELECT use_count, score, last_used_at, score_updated_at FROM {TABLE} "
            "WHERE command = ? AND surface = ?", (cmd, surf)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return CommandUsage(cmd, surf, int(row[0]),
                        decay(float(row[1]), ts - int(row[3])), int(row[2]), int(row[3]))


def _stored_rows(conn: sqlite3.Connection, surface: str) -> Dict[str, tuple]:
    try:
        return {r[0]: (float(r[1]), int(r[2]), int(r[3])) for r in conn.execute(
            f"SELECT command, score, last_used_at, score_updated_at FROM {TABLE} "
            "WHERE surface = ?", (surface,))}
    except sqlite3.OperationalError:
        return {}


def rank_commands(conn: sqlite3.Connection, commands: Sequence[str], surface: str,
                  now: Optional[float] = None) -> List[str]:
    """*commands* reordered by effective score, most used first.

    The caller passes the commands that currently exist, and only those come
    back: a row for a command that has since been removed must not resurrect
    it, and a command with no history must still appear -- at score 0, in its
    original position relative to other unused ones.

    Ties break by last use, then by the order given, so a menu where everything
    scores 0 looks exactly as it does today instead of being alphabetized.
    """
    surf, ts = _surface(surface), _now(now)
    stored = _stored_rows(conn, surf)
    ranked = []
    for index, raw in enumerate(commands):
        cmd = _command(raw)
        score, last_used = 0.0, 0
        if cmd in stored:
            s, last_used, updated = stored[cmd]
            score = decay(s, ts - updated)
        ranked.append((-score, -last_used, index, raw))
    ranked.sort()
    return [raw for _, _, _, raw in ranked]


def all_usage(conn: sqlite3.Connection, surface: str,
              now: Optional[float] = None) -> List[CommandUsage]:
    """Every stored row for *surface*, decayed, highest first. Diagnostics."""
    surf, ts = _surface(surface), _now(now)
    out = [CommandUsage(c, surf, 0, decay(s, ts - u), l, u)
           for c, (s, l, u) in _stored_rows(conn, surf).items()]
    out.sort(key=lambda r: (-r.effective_score, -r.last_used_at, r.command))
    return out


__all__ = ["COMPONENT", "TABLE", "HALF_LIFE_DAYS", "HALF_LIFE_SECONDS", "CommandUsage",
           "Layer2MigrationError", "decay", "ensure_ready", "record_command_use",
           "get_command_usage", "rank_commands", "all_usage"]
