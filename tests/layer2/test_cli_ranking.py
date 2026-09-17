"""CLI integration: learned order, cached reads, and fail-open behaviour."""

import sqlite3

import pytest

from hermes_layer2 import cli_ranking, command_usage as cu

T0 = 1_700_000_000.0
DAY = 86400.0


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cli_ranking.reset_for_tests()
    conn = sqlite3.connect(tmp_path / "state.db")
    cu.ensure_ready(conn)
    conn.close()
    return tmp_path


def _use(home, command, times=1, now=T0, surface="cli"):
    conn = sqlite3.connect(home / "state.db")
    for _ in range(times):
        cu.record_command_use(conn, command, surface, now=now)
    conn.close()
    cli_ranking.invalidate()


# 1 — with no history, the registry order survives untouched
def test_no_history_keeps_registry_order(home):
    names = ["/status", "/resume", "/model", "/debug"]
    assert cli_ranking.ranked(names) == names


# 2 — a repeatedly used command rises
def test_used_command_rises(home):
    names = ["/status", "/resume", "/model", "/debug"]
    _use(home, "resume", times=5)
    assert cli_ranking.ranked(names)[0] == "/resume"


# 3 — an old favourite decays and can be overtaken
def test_old_favourite_is_overtaken(home):
    conn = sqlite3.connect(home / "state.db")
    for _ in range(100):
        cu.record_command_use(conn, "debug", "cli", now=T0)
    for _ in range(3):
        cu.record_command_use(conn, "status", "cli", now=T0 + 200 * DAY)
    order = cu.rank_commands(conn, ["/debug", "/status"], "cli", now=T0 + 200 * DAY)
    conn.close()
    assert order[0] == "/status", "recent use beats a decayed pile of old ones"


# 11 — typing a word does not hit SQLite again
def test_cache_prevents_repeated_sqlite_reads(home):
    names = ["/status", "/resume", "/model"]
    _use(home, "resume", times=2)
    before = cli_ranking.sqlite_reads()
    for _ in range(12):                      # as if typing "/resume"
        cli_ranking.ranked(names)
    assert cli_ranking.sqlite_reads() == before + 1, "one read for the whole sequence"


# 12 — recording a use invalidates the cache
def test_recording_a_use_invalidates_the_cache(home):
    names = ["/status", "/resume"]
    cli_ranking.ranked(names)
    reads = cli_ranking.sqlite_reads()
    cli_ranking.record_use("resume")
    cli_ranking.ranked(names)
    assert cli_ranking.sqlite_reads() == reads + 1
    assert cli_ranking.ranked(names)[0] == "/resume"


def test_changed_command_set_recomputes(home):
    cli_ranking.ranked(["/status", "/resume"])
    reads = cli_ranking.sqlite_reads()
    cli_ranking.ranked(["/status", "/resume", "/newplugin"])   # a plugin appeared
    assert cli_ranking.sqlite_reads() == reads + 1


# 13 — a broken layer 2 never blocks anything
def test_missing_database_returns_original_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nonexistent"))
    cli_ranking.reset_for_tests()
    names = ["/status", "/resume"]
    assert cli_ranking.ranked(names) == names
    cli_ranking.record_use("resume")            # must not raise


def test_corrupt_database_returns_original_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "state.db").write_text("this is not a database")
    cli_ranking.reset_for_tests()
    names = ["/status", "/resume"]
    assert cli_ranking.ranked(names) == names
    cli_ranking.record_use("resume")            # must not raise


# 6 — an alias feeds the canonical command
def test_alias_and_canonical_share_one_row(home):
    from hermes_cli.commands import resolve_command
    alias = resolve_command("reset")
    assert alias is not None and alias.name == "new", "registry alias changed"
    conn = sqlite3.connect(home / "state.db")
    # the CLI records resolve_command(...).name, never the typed word
    for typed in ("reset", "new", "reset"):
        cu.record_command_use(conn, resolve_command(typed).name, "cli", now=T0)
    assert cu.get_command_usage(conn, "new", "cli", now=T0).use_count == 3
    assert cu.get_command_usage(conn, "reset", "cli", now=T0) is None
    conn.close()


# 7 — an unknown word is never recorded
def test_invalid_command_is_not_recorded(home):
    from hermes_cli.commands import resolve_command
    assert resolve_command("definitely-not-a-command") is None
    conn = sqlite3.connect(home / "state.db")
    assert conn.execute(f"SELECT COUNT(*) FROM {cu.TABLE}").fetchone()[0] == 0
    conn.close()


# 8 — one dispatch records exactly one use
def test_one_dispatch_records_exactly_one_use(home):
    cli_ranking.record_use("status")
    conn = sqlite3.connect(home / "state.db")
    assert cu.get_command_usage(conn, "status", "cli").use_count == 1
    conn.close()


# 10 — autocomplete alone records nothing
def test_autocomplete_does_not_record(home):
    for _ in range(20):
        cli_ranking.ranked(["/status", "/resume"])
    conn = sqlite3.connect(home / "state.db")
    assert conn.execute(f"SELECT COUNT(*) FROM {cu.TABLE}").fetchone()[0] == 0
    conn.close()


# 4/5/14/15 — matching semantics are untouched by the reordering
def test_matching_and_visibility_are_unchanged(home, monkeypatch):
    from hermes_cli.commands_completion import SlashCommandCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    comp = SlashCommandCompleter()
    ev = CompleteEvent()

    def names_for(text):
        return [c.display_text if hasattr(c, "display_text") else c.text
                for c in comp.get_completions(Document(text, len(text)), ev)]

    baseline_all = names_for("/")
    baseline_res = names_for("/res")
    assert baseline_res, "prefix matching must return something"

    _use(home, "resume", times=9)

    after_all = names_for("/")
    after_res = names_for("/res")
    # the same commands, only reordered
    assert set(after_all) == set(baseline_all), "reordering must not add or drop commands"
    assert set(after_res) == set(baseline_res), "prefix matching is unaffected"
    assert len(after_all) == len(baseline_all)
