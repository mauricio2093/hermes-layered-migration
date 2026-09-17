"""Layer-2 schema management, centralized.

Upstream owns ``schema_version``. We never take a number from it: the day
upstream ships its own next version, two different schemas would share one
number and neither side could tell them apart.

So layer 2 keeps its own line, **per component**, in the same database:

    hermes_layer2_schema(component, version, updated_at)

Per component rather than one global number, so ``command_usage`` can evolve
without forcing every other component to be versioned alongside it.

No module outside this one creates layer-2 tables. A module asks for the
component it needs and gets a schema at the version it expects, or an error --
never a half-applied one.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable, Dict, Mapping

logger = logging.getLogger(__name__)

REGISTRY_TABLE = "hermes_layer2_schema"

_REGISTRY_DDL = f"""
CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
    component  TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

# component -> {target_version: migration}. A migration takes a cursor and is
# applied inside the caller's transaction; it must be deterministic and safe to
# re-run, since a crash between steps replays the last one.
#
# ``command_usage`` is deliberately absent: step 3 builds the machinery, step 4
# is its first real consumer.
MIGRATIONS: Dict[str, Dict[int, Callable[[sqlite3.Cursor], None]]] = {}


class Layer2MigrationError(RuntimeError):
    """A component migration failed; its transaction was rolled back."""


def register_component(component: str, migrations: Mapping[int, Callable[[sqlite3.Cursor], None]]) -> None:
    """Register a component's migrations, keyed by the version they produce."""
    if not component or not component.strip():
        raise ValueError("component name must be non-empty")
    MIGRATIONS.setdefault(component, {}).update(dict(migrations))


def target_version(component: str) -> int:
    """Highest version registered for *component*; 0 when it has no migrations."""
    return max(MIGRATIONS.get(component, {}) or {0: None}, default=0)


def ensure_layer2_schema(conn: sqlite3.Connection) -> None:
    """Create the registry table. Idempotent, and safe under concurrency.

    ``CREATE TABLE IF NOT EXISTS`` inside IMMEDIATE takes the write lock up
    front, so a second process either waits and finds the table already there,
    or gets ``database is locked`` -- never a partially created registry.
    """
    with _immediate(conn) as cur:
        cur.execute(_REGISTRY_DDL)


def get_component_version(conn: sqlite3.Connection, component: str) -> int:
    """Installed version of *component*; 0 when unknown or absent."""
    try:
        row = conn.execute(
            f"SELECT version FROM {REGISTRY_TABLE} WHERE component = ?", (component,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def migrate_component(conn: sqlite3.Connection, component: str) -> int:
    """Bring *component* up to its registered target. Returns the version now installed.

    Every step runs inside one transaction with the registry bump, so a failure
    leaves the component exactly where it was -- never half applied.
    """
    ensure_layer2_schema(conn)
    current = get_component_version(conn, component)
    target = target_version(component)
    if current >= target:
        return current

    steps = MIGRATIONS.get(component, {})
    for version in range(current + 1, target + 1):
        migration = steps.get(version)
        if migration is None:
            raise Layer2MigrationError(
                f"{component}: no migration produces version {version} "
                f"(installed {current}, target {target})")
        try:
            with _immediate(conn) as cur:
                migration(cur)
                cur.execute(
                    f"INSERT INTO {REGISTRY_TABLE} (component, version, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(component) DO UPDATE SET version = excluded.version, "
                    "updated_at = excluded.updated_at",
                    (component, version, int(time.time())))
        except Exception as exc:  # the transaction already rolled back
            installed = get_component_version(conn, component)
            raise Layer2MigrationError(
                f"{component}: migration to v{version} failed and was rolled back "
                f"(still at v{installed}): {exc}") from exc
        logger.debug("layer2: %s migrated to v%s", component, version)
    return get_component_version(conn, component)


def installed_components(conn: sqlite3.Connection) -> Dict[str, int]:
    """Every component the registry knows about, with its installed version."""
    try:
        return {r[0]: int(r[1]) for r in conn.execute(
            f"SELECT component, version FROM {REGISTRY_TABLE} ORDER BY component")}
    except sqlite3.OperationalError:
        return {}


class _immediate:
    """BEGIN IMMEDIATE ... COMMIT / ROLLBACK, independent of isolation_level.

    Python's implicit transaction handling does not cover DDL, so layer-2
    migrations manage their own: DDL and the registry bump must commit or roll
    back together, or a crash between them leaves a table whose version says it
    does not exist.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Cursor:
        self._cur = self._conn.cursor()
        if not self._conn.in_transaction:
            self._cur.execute("BEGIN IMMEDIATE")
        return self._cur

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._cur.close()
        return False
