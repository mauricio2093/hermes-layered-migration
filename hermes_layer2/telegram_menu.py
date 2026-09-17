"""Publish the Telegram menu only when the rendered payload actually changes.

Learning is cheap; publishing is not. A score moving from 8.41 to 8.42 changes
nothing a user can see, so the trigger is the **rendered payload** -- the exact
(name, description) pairs in their final order -- not the scores behind it.

    use a command  ->  usage recorded, ranking dirty
    publish        ->  only when the payload differs from what was last sent

The fingerprint is persisted: kept in memory alone, every restart would believe
it had never published and call the Bot API again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, Sequence, Tuple

from hermes_layer2.state_schema import migrate_component, register_component

logger = logging.getLogger(__name__)

COMPONENT = "telegram_command_menu"
TABLE = "layer2_telegram_menu_state"

#: Minimum seconds between publishes for one key. Learning stays fast; the menu
#: settles slowly, so two commands swapping places repeatedly cost one call.
PUBLISH_COOLDOWN_SECONDS = 3600

_V1_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    menu_key     TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    published_at INTEGER NOT NULL
)
"""


def _migrate_v1(cur: sqlite3.Cursor) -> None:
    """v1: what was last published, per menu key. Separate from usage: one is
    learning, the other is delivery, and they change for different reasons."""
    cur.execute(_V1_DDL)


register_component(COMPONENT, {1: _migrate_v1})


def fingerprint(payload: Sequence[Tuple[str, str]]) -> str:
    """Stable digest of the rendered menu. Order is part of it; scores are not.

    JSON rather than a hand-rolled separator: a description containing the
    separator would otherwise collide with a genuinely different payload.
    """
    canonical = json.dumps([[str(name), str(desc)] for name, desc in payload],
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _db_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"


def _connect() -> Optional[sqlite3.Connection]:
    path = _db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=5)
    migrate_component(conn, COMPONENT)
    return conn


def last_published(menu_key: str = "default") -> Tuple[Optional[str], int]:
    """``(fingerprint, published_at)`` last confirmed for *menu_key*."""
    try:
        conn = _connect()
        if conn is None:
            return None, 0
        try:
            row = conn.execute(
                f"SELECT fingerprint, published_at FROM {TABLE} WHERE menu_key = ?",
                (menu_key,)).fetchone()
        finally:
            conn.close()
        return (row[0], int(row[1])) if row else (None, 0)
    except Exception as exc:
        logger.debug("layer2: could not read menu state: %s", exc)
        return None, 0


def should_publish(payload: Sequence[Tuple[str, str]], menu_key: str = "default",
                   now: Optional[float] = None) -> bool:
    """True when *payload* differs from the last confirmed publish and the
    cooldown has elapsed.

    Fails **open**: if the state cannot be read, publishing proceeds. A menu
    published once too often is a smaller problem than one that never updates.
    """
    try:
        stored, published_at = last_published(menu_key)
        if stored is None:
            return True
        if stored == fingerprint(payload):
            return False
        elapsed = (time.time() if now is None else now) - published_at
        if elapsed < PUBLISH_COOLDOWN_SECONDS:
            logger.debug("layer2: menu changed but cooling down (%ds left)",
                         int(PUBLISH_COOLDOWN_SECONDS - elapsed))
            return False
        return True
    except Exception as exc:
        logger.debug("layer2: could not decide on publishing: %s", exc)
        return True


def mark_published(payload: Sequence[Tuple[str, str]], menu_key: str = "default",
                   now: Optional[float] = None) -> None:
    """Record a publish that Telegram **confirmed**.

    Only ever called after a successful Bot API call: recording a publish that
    failed would make the next evaluation skip the retry, leaving the menu
    permanently stale.
    """
    try:
        conn = _connect()
        if conn is None:
            return
        try:
            conn.execute(
                f"INSERT INTO {TABLE} (menu_key, fingerprint, published_at) VALUES (?, ?, ?) "
                "ON CONFLICT(menu_key) DO UPDATE SET fingerprint = excluded.fingerprint, "
                "published_at = excluded.published_at",
                (menu_key, fingerprint(payload),
                 int(time.time() if now is None else now)))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("layer2: could not record publish: %s", exc)
