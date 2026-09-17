"""Frequency + recency scoring: decay on write, decay on read, stable order."""

import sqlite3
import threading

import pytest

from hermes_layer2 import command_usage as cu
from hermes_layer2 import state_schema as l2

DAY = 86400.0
T0 = 1_700_000_000.0          # fixed epoch: no test depends on the wall clock


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "s.db", timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    cu.ensure_ready(c)
    return c


# 13/14 — the component migrates, and again is a no-op
def test_component_migrates_to_v1_and_is_idempotent(tmp_path):
    c = sqlite3.connect(tmp_path / "s.db")
    assert l2.get_component_version(c, cu.COMPONENT) == 0
    assert cu.ensure_ready(c) == 1
    assert c.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE name='{cu.TABLE}'").fetchone()[0] == 1
    assert cu.ensure_ready(c) == 1          # second run changes nothing


# 1 — the first use creates the row correctly
def test_first_use_creates_the_row(conn):
    u = cu.record_command_use(conn, "status", "cli", now=T0)
    assert (u.use_count, u.last_used_at, u.score_updated_at) == (1, int(T0), int(T0))
    assert u.effective_score == pytest.approx(1.0)


# 2/3 — a second use increments the count and lands near 2
def test_second_immediate_use_counts_and_scores_two(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)
    u = cu.record_command_use(conn, "status", "cli", now=T0)
    assert u.use_count == 2
    assert u.effective_score == pytest.approx(2.0, abs=1e-9)


# 4 — one half-life halves the score
def test_one_half_life_halves_the_score(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)
    later = cu.get_command_usage(conn, "status", "cli", now=T0 + 30 * DAY)
    assert later.effective_score == pytest.approx(0.5, abs=1e-6)


# 5 — two half-lives quarter it
def test_two_half_lives_quarter_the_score(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)
    later = cu.get_command_usage(conn, "status", "cli", now=T0 + 60 * DAY)
    assert later.effective_score == pytest.approx(0.25, abs=1e-6)


# 6 — reading decays without touching the row
def test_reading_decays_without_writing(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)
    before = conn.execute(
        f"SELECT score, score_updated_at FROM {cu.TABLE}").fetchone()
    cu.get_command_usage(conn, "status", "cli", now=T0 + 90 * DAY)
    cu.rank_commands(conn, ["status"], "cli", now=T0 + 90 * DAY)
    after = conn.execute(
        f"SELECT score, score_updated_at FROM {cu.TABLE}").fetchone()
    assert tuple(before) == tuple(after), "a read must not rewrite the row"


# 7 — a use after months decays first, then adds one
def test_use_after_months_decays_then_adds_one(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)          # score 1.0
    u = cu.record_command_use(conn, "status", "cli", now=T0 + 60 * DAY)
    assert u.effective_score == pytest.approx(1.25, abs=1e-6)     # 1*0.25 + 1
    assert u.use_count == 2


# the case that drove the design: 100 points, six months untouched
def test_abandoned_command_does_not_stay_at_one_hundred(conn):
    for _ in range(100):
        cu.record_command_use(conn, "debug", "cli", now=T0)
    assert cu.get_command_usage(conn, "debug", "cli", now=T0).effective_score == pytest.approx(100.0)

    six_months = cu.get_command_usage(conn, "debug", "cli", now=T0 + 180 * DAY)
    assert six_months.effective_score == pytest.approx(100 * 0.5 ** 6, rel=1e-6)
    assert six_months.effective_score < 2.0, "a read must not report the frozen 100"
    assert six_months.use_count == 100, "history is kept, it just stops ranking"


# 8 — the same command on two surfaces is two independent rows
def test_surfaces_are_independent(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)
    cu.record_command_use(conn, "status", "cli", now=T0)
    cu.record_command_use(conn, "status", "telegram", now=T0)
    assert cu.get_command_usage(conn, "status", "cli", now=T0).use_count == 2
    assert cu.get_command_usage(conn, "status", "telegram", now=T0).use_count == 1
    assert conn.execute(f"SELECT COUNT(*) FROM {cu.TABLE}").fetchone()[0] == 2


# 9 — a command with no history still appears, at zero
def test_command_without_history_scores_zero(conn):
    assert cu.get_command_usage(conn, "never", "cli", now=T0) is None
    assert cu.rank_commands(conn, ["never", "other"], "cli", now=T0) == ["never", "other"]


# 10 — a row for a command that no longer exists must not resurrect it
def test_removed_command_does_not_contaminate_the_ranking(conn):
    for _ in range(50):
        cu.record_command_use(conn, "gone", "cli", now=T0)
    cu.record_command_use(conn, "status", "cli", now=T0)
    ranked = cu.rank_commands(conn, ["status", "model"], "cli", now=T0)
    assert "gone" not in ranked
    assert ranked == ["status", "model"]


# 11 — ties keep the order they were given, not alphabetical
def test_tiebreak_is_deterministic_and_preserves_original_order(conn):
    given = ["zebra", "alpha", "mango"]
    assert cu.rank_commands(conn, given, "cli", now=T0) == given
    for _ in range(5):
        assert cu.rank_commands(conn, given, "cli", now=T0) == given
    # equal scores, different recency: the more recent wins
    cu.record_command_use(conn, "alpha", "cli", now=T0)
    cu.record_command_use(conn, "zebra", "cli", now=T0 + 1)
    assert cu.rank_commands(conn, given, "cli", now=T0 + 2)[:2] == ["zebra", "alpha"]


def test_ranking_puts_the_most_used_first(conn):
    for _ in range(10):
        cu.record_command_use(conn, "resume", "cli", now=T0)
    for _ in range(3):
        cu.record_command_use(conn, "model", "cli", now=T0)
    assert cu.rank_commands(conn, ["debug", "model", "resume"], "cli", now=T0) == [
        "resume", "model", "debug"]


# 12 — concurrent writes lose no uses
def test_concurrent_writes_lose_no_uses(tmp_path):
    p = str(tmp_path / "s.db")
    c0 = sqlite3.connect(p, timeout=30)
    c0.execute("PRAGMA journal_mode=WAL")
    cu.ensure_ready(c0)
    errors, barrier = [], threading.Barrier(6)

    def worker():
        c = sqlite3.connect(p, timeout=30)
        try:
            barrier.wait(timeout=15)
            for _ in range(5):
                cu.record_command_use(c, "status", "cli", now=T0)
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert cu.get_command_usage(c0, "status", "cli", now=T0).use_count == 30


# a backwards clock must not inflate the score
def test_clock_going_backwards_does_not_raise_the_score(conn):
    cu.record_command_use(conn, "status", "cli", now=T0)
    assert cu.decay(1.0, -999999) == pytest.approx(1.0)
    earlier = cu.get_command_usage(conn, "status", "cli", now=T0 - 365 * DAY)
    assert earlier.effective_score == pytest.approx(1.0), "never above the stored value"


def test_surface_is_validated(conn):
    for bad in ("", "   ", "with space", None):
        with pytest.raises((ValueError, TypeError)):
            cu.record_command_use(conn, "status", bad, now=T0)


def test_leading_slash_is_normalized(conn):
    cu.record_command_use(conn, "/status", "cli", now=T0)
    assert cu.get_command_usage(conn, "status", "cli", now=T0).use_count == 1


# 15 — upstream's schema_version is never touched
def test_upstream_schema_version_untouched(tmp_path):
    c = sqlite3.connect(tmp_path / "s.db")
    c.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL);"
                    "INSERT INTO schema_version (version) VALUES (30);")
    c.commit()
    cu.ensure_ready(c)
    cu.record_command_use(c, "status", "cli", now=T0)
    assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 30
