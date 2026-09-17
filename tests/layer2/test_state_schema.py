"""Layer-2 schema machinery: idempotence, concurrency, atomic migrations."""

import sqlite3
import threading

import pytest

from hermes_layer2 import state_schema as l2


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = {k: dict(v) for k, v in l2.MIGRATIONS.items()}
    l2.MIGRATIONS.clear()
    yield
    l2.MIGRATIONS.clear()
    l2.MIGRATIONS.update(saved)


def _conn(path):
    c = sqlite3.connect(path, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _upstream_like(path):
    """A database shaped like upstream's: its own schema_version at 30."""
    c = _conn(path)
    c.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);"
        "INSERT INTO schema_version (version) VALUES (30);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);")
    c.commit()
    return c


# 1 — a fresh database with no layer-2 tables at all
def test_fresh_database_gets_the_registry(tmp_path):
    c = _conn(tmp_path / "s.db")
    assert l2.installed_components(c) == {}
    l2.ensure_layer2_schema(c)
    assert l2.installed_components(c) == {}
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert l2.REGISTRY_TABLE in tables


# 2 — an existing upstream v30 database
def test_existing_upstream_database_is_not_disturbed(tmp_path):
    p = tmp_path / "s.db"
    c = _upstream_like(p)
    l2.ensure_layer2_schema(c)
    assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 30
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='sessions'").fetchone()[0] == 1


# 3 — initializing twice changes nothing
def test_second_initialization_is_a_no_op(tmp_path):
    c = _conn(tmp_path / "s.db")
    l2.ensure_layer2_schema(c)
    before = c.execute(f"SELECT sql FROM sqlite_master WHERE name='{l2.REGISTRY_TABLE}'").fetchone()[0]
    l2.ensure_layer2_schema(c)
    after = c.execute(f"SELECT sql FROM sqlite_master WHERE name='{l2.REGISTRY_TABLE}'").fetchone()[0]
    assert before == after
    assert c.execute(f"SELECT COUNT(*) FROM {l2.REGISTRY_TABLE}").fetchone()[0] == 0


# 4 — two processes initializing at once leave no half-built schema
def test_concurrent_initialization_never_half_builds(tmp_path):
    p = str(tmp_path / "s.db")
    _conn(p).close()
    errors, barrier = [], threading.Barrier(8)

    def worker():
        conn = _conn(p)
        try:
            barrier.wait(timeout=10)
            l2.ensure_layer2_schema(conn)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():   # contention is acceptable
                errors.append(exc)
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, errors
    c = _conn(p)
    rows = c.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{l2.REGISTRY_TABLE}'").fetchone()[0]
    assert rows == 1, "the registry must exist exactly once"


# 5 — a component migrates 0 -> 1
def test_component_migrates_from_zero_to_one(tmp_path):
    c = _conn(tmp_path / "s.db")
    l2.register_component("demo", {1: lambda cur: cur.execute(
        "CREATE TABLE IF NOT EXISTS layer2_demo (k TEXT PRIMARY KEY)")})
    assert l2.get_component_version(c, "demo") == 0
    assert l2.migrate_component(c, "demo") == 1
    assert l2.get_component_version(c, "demo") == 1
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='layer2_demo'").fetchone()[0] == 1
    # re-running is a no-op, not a second application
    assert l2.migrate_component(c, "demo") == 1


# 6 — a failing migration leaves nothing behind
def test_failed_migration_rolls_back_entirely(tmp_path):
    c = _conn(tmp_path / "s.db")

    def broken(cur):
        cur.execute("CREATE TABLE layer2_broken (k TEXT)")
        raise RuntimeError("boom halfway through")

    l2.register_component("brk", {1: broken})
    with pytest.raises(l2.Layer2MigrationError) as err:
        l2.migrate_component(c, "brk")
    assert "rolled back" in str(err.value)
    # neither the table nor the version survived
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='layer2_broken'").fetchone()[0] == 0
    assert l2.get_component_version(c, "brk") == 0


# 7 — upstream's own version is untouched by all of the above
def test_upstream_schema_version_stays_exactly_30(tmp_path):
    p = tmp_path / "s.db"
    c = _upstream_like(p)
    l2.register_component("demo", {1: lambda cur: cur.execute(
        "CREATE TABLE IF NOT EXISTS layer2_demo (k TEXT PRIMARY KEY)")})
    l2.migrate_component(c, "demo")
    assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 30
    assert c.execute(f"SELECT version FROM {l2.REGISTRY_TABLE} WHERE component='demo'").fetchone()[0] == 1
