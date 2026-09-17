"""Telegram menu: learning stays fast, publishing stays slow."""

import sqlite3

import pytest

from hermes_layer2 import command_usage as cu
from hermes_layer2 import telegram_menu as tm

T0 = 1_700_000_000.0
DAY = 86400.0
HOUR = 3600.0

PAYLOAD_A = [("status", "Show status"), ("resume", "Resume a session")]
PAYLOAD_B = [("resume", "Resume a session"), ("status", "Show status")]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "state.db")
    cu.ensure_ready(conn)
    conn.close()
    return tmp_path


# --- fingerprint -----------------------------------------------------------

def test_fingerprint_tracks_order_not_scores():
    assert tm.fingerprint(PAYLOAD_A) == tm.fingerprint(list(PAYLOAD_A))
    assert tm.fingerprint(PAYLOAD_A) != tm.fingerprint(PAYLOAD_B)


def test_fingerprint_separator_cannot_be_forged():
    """A description containing the separator must not collide with a real change."""
    a = [("a", 'x","y'), ("b", "z")]
    b = [("a", "x"), ("y", "b"), ("", "z")]
    assert tm.fingerprint(a) != tm.fingerprint(b)


# --- 6/7/8/9/10 — when to publish -----------------------------------------

def test_first_evaluation_publishes(home):
    assert tm.should_publish(PAYLOAD_A, now=T0) is True


def test_identical_payload_does_not_publish(home):
    tm.mark_published(PAYLOAD_A, now=T0)
    assert tm.should_publish(PAYLOAD_A, now=T0 + 10 * DAY) is False


def test_score_change_without_order_change_does_not_publish(home):
    """The case this exists for: scores move constantly, the menu does not."""
    conn = sqlite3.connect(home / "state.db")
    for _ in range(3):
        cu.record_command_use(conn, "status", "telegram", now=T0)
    tm.mark_published(PAYLOAD_A, now=T0)
    for _ in range(50):                      # scores keep moving
        cu.record_command_use(conn, "status", "telegram", now=T0 + HOUR)
    conn.close()
    assert tm.should_publish(PAYLOAD_A, now=T0 + 10 * DAY) is False


def test_real_order_change_publishes_after_cooldown(home):
    tm.mark_published(PAYLOAD_A, now=T0)
    assert tm.should_publish(PAYLOAD_B, now=T0 + 2 * HOUR) is True


def test_churn_is_absorbed_by_the_cooldown(home):
    tm.mark_published(PAYLOAD_A, now=T0)
    for minute in range(1, 40):              # two commands trading places
        payload = PAYLOAD_B if minute % 2 else PAYLOAD_A
        assert tm.should_publish(payload, now=T0 + minute * 60) is False


def test_fingerprint_survives_a_restart(home):
    tm.mark_published(PAYLOAD_A, now=T0)
    stored, at = tm.last_published()          # fresh connection, as after a restart
    assert stored == tm.fingerprint(PAYLOAD_A)
    assert at == int(T0)
    assert tm.should_publish(PAYLOAD_A, now=T0 + 30 * DAY) is False


def test_failed_publish_leaves_the_old_fingerprint(home):
    """mark_published is only called on success, so a failure must retry later."""
    tm.mark_published(PAYLOAD_A, now=T0)
    # publishing B fails -> mark_published is never called
    assert tm.should_publish(PAYLOAD_B, now=T0 + 2 * HOUR) is True
    assert tm.should_publish(PAYLOAD_B, now=T0 + 3 * HOUR) is True, "the retry must stay available"
    tm.mark_published(PAYLOAD_B, now=T0 + 3 * HOUR)
    assert tm.should_publish(PAYLOAD_B, now=T0 + 20 * DAY) is False


# --- 16 — fail-open --------------------------------------------------------

def test_missing_database_publishes_rather_than_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))
    assert tm.should_publish(PAYLOAD_A, now=T0) is True
    tm.mark_published(PAYLOAD_A, now=T0)      # must not raise


def test_corrupt_database_publishes_rather_than_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "state.db").write_text("not a database")
    assert tm.should_publish(PAYLOAD_A, now=T0) is True
    tm.mark_published(PAYLOAD_A, now=T0)      # must not raise


# --- 4/12/11 — the ranking side -------------------------------------------

def test_cli_and_telegram_stay_separate(home):
    conn = sqlite3.connect(home / "state.db")
    for _ in range(5):
        cu.record_command_use(conn, "resume", "telegram", now=T0)
    cu.record_command_use(conn, "status", "cli", now=T0)
    assert cu.rank_commands(conn, ["status", "resume"], "telegram", now=T0)[0] == "resume"
    assert cu.rank_commands(conn, ["status", "resume"], "cli", now=T0)[0] == "status"
    conn.close()


def test_removed_command_is_not_resurrected_in_the_menu(home):
    conn = sqlite3.connect(home / "state.db")
    for _ in range(90):
        cu.record_command_use(conn, "retired", "telegram", now=T0)
    ranked = cu.rank_commands(conn, ["status", "resume"], "telegram", now=T0)
    conn.close()
    assert "retired" not in ranked


def test_new_command_without_history_still_appears(home):
    conn = sqlite3.connect(home / "state.db")
    for _ in range(9):
        cu.record_command_use(conn, "status", "telegram", now=T0)
    ranked = cu.rank_commands(conn, ["status", "brandnew"], "telegram", now=T0)
    conn.close()
    assert ranked == ["status", "brandnew"]


# --- the headline: 100 uses must not mean 100 Bot API calls ---------------

def test_a_burst_of_uses_produces_a_single_publish(home):
    """Instrumented: uses, writes, recomputations and Bot API calls."""
    conn = sqlite3.connect(home / "state.db")
    commands = ["status", "resume", "model", "debug"]
    executed = writes = recomputes = publishes = 0
    now = T0

    for i in range(100):
        command = commands[i % len(commands)]
        cu.record_command_use(conn, command, "telegram", now=now)
        executed += 1
        writes += 1
        now += 30                                    # half a minute apart

        if i % 10 == 9:                              # periodic re-evaluation
            ordered = cu.rank_commands(conn, commands, "telegram", now=now)
            recomputes += 1
            payload = [(c, f"desc {c}") for c in ordered]
            if tm.should_publish(payload, now=now):
                tm.mark_published(payload, now=now)
                publishes += 1
    conn.close()

    print(f"\n  executed={executed} writes={writes} "
          f"recomputes={recomputes} setMyCommands={publishes}")
    assert executed == 100
    assert writes == 100
    assert recomputes == 10
    assert publishes == 1, "one publish for a hundred uses"
    total = sum(cu.get_command_usage(
        sqlite3.connect(home / "state.db"), c, "telegram", now=now).use_count
        for c in commands)
    assert total == 100, "every use was recorded"
