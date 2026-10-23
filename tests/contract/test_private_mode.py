"""Private mode is metadata-only, time-aware and shared by capture, recall and background work."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from scope_recall.contracts import ContractError, validate_payload
from scope_recall.core import CoreConfig, MemoryCore, private
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.maintenance import cli
from scope_recall.maintenance import private as private_cli
from v11_support import context, downgrade_store, recall_request, source_event


class PrivateClock:
    def __init__(self):
        self.now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

    def utc_now(self):
        return self.now.isoformat()

    def monotonic(self):
        return 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def at(self, value):
        self.now = datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def app(tmp_path):
    clock = PrivateClock()
    ctx = context(tmp_path / "TEST-private-store")
    core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    core.initialize()
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    return core, ctx, clock, owner


def _event(key="TEST-private/1", *, occurred_at="2026-09-05T12:05:00Z", content="TEST private colour is white."):
    return source_event(source_event_key=key, content=content, occurred_at=occurred_at, recorded_at=occurred_at)


def _count(core, ctx, table):
    with core.storage.read(ctx) as tx:
        return tx._check().execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_the_table_holds_interval_metadata_and_nothing_spoken(app):
    core, ctx, _clock, _owner = app
    with closing(sqlite3.connect(core.storage.path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(private_intervals)")}
    assert columns == {
        "interval_id",
        "target_kind",
        "session_id",
        "project_path",
        "mode",
        "starts_at",
        "ends_at",
        "created_at",
    }
    assert not columns.intersection({"content", "body", "text", "message", "prompt", "query"})


def test_capture_is_decided_at_event_time_and_survives_turning_the_rule_off(app):
    core, ctx, clock, owner = app
    owner.private_on(target="session", session=ctx.session_id, mode="full")
    refused = core.record_event(ctx, _event(occurred_at="2026-09-05T12:05:00Z"), scope_id="TEST-scope")
    assert (refused.disposition, refused.error_code) == ("rejected", "PRIVATE_MODE")
    assert _count(core, ctx, "source_events") == 0
    # A backfill read after the interval closed is still refused by its own event time.
    clock.advance(3600)
    owner.private_off(target="session", session=ctx.session_id)
    late = core.record_event(ctx, _event(occurred_at="2026-09-05T12:10:00Z"), scope_id="TEST-scope")
    assert (late.disposition, late.error_code) == ("rejected", "PRIVATE_MODE")
    assert _count(core, ctx, "source_events") == 0
    # What was said after the interval is ordinary capture again.
    after = core.record_event(ctx, _event(occurred_at="2026-09-05T13:10:00Z"), scope_id="TEST-scope")
    assert after.disposition == "inserted"


def test_durable_ingress_is_refused_before_its_payload_row_exists(app):
    core, ctx, _clock, owner = app
    owner.private_on(target="session", session=ctx.session_id, mode="full")
    receipt = core.record_host_event(ctx, _event(), scope_id="TEST-scope", host_scope=None)
    assert (receipt.disposition, receipt.error_code) == ("rejected", "PRIVATE_MODE")
    assert _count(core, ctx, "capture_inbox") == 0
    assert _count(core, ctx, "source_events") == 0


def test_full_returns_empty_before_any_embedding_and_read_only_recalls(app):
    core, ctx, clock, owner = app
    clock.at("2026-09-05T11:50:00Z")
    first = core.record_event(
        ctx, _event(occurred_at="2026-09-05T11:50:00Z", content="TEST white"), scope_id="TEST-scope"
    )
    assert first.disposition == "inserted"
    vectors = CountingVector()
    reading_core = MemoryCore(CoreConfig(ctx.binding), storage=core.storage, clock=clock, vectors=vectors)
    owner.private_on(target="global", mode="full")
    packet = reading_core.recall_packet(ctx, recall_request(query="TEST white", mode="current"))
    assert packet["status"] == "no_match" and packet["items"] == []
    assert "private_mode:full" in packet["gaps"]
    assert vectors.calls == 0
    assert core.record_event(ctx, _event(), scope_id="TEST-scope").error_code == "PRIVATE_MODE"
    owner.private_off(target="global")
    read_only = owner.private_on(target="session", session=ctx.session_id, mode="read-only")
    assert read_only["mode"] == "read-only"
    packet = reading_core.recall_packet(ctx, recall_request(query="TEST white", mode="current"))
    assert packet["items"], packet
    assert core.record_event(ctx, _event(), scope_id="TEST-scope").error_code == "PRIVATE_MODE"


def test_windows_project_paths_match_by_whole_components(app):
    core, ctx, _clock, owner = app
    owner.private_on(target="project", project=r"C:\Work\Project", mode="full")
    inside = replace(ctx, project_root="c:/work/project/src")
    sibling = replace(ctx, project_root=r"C:\Work\Project2")
    assert core.record_event(inside, _event(key="TEST-inside/1"), scope_id="TEST-scope").error_code == "PRIVATE_MODE"
    assert core.record_event(sibling, _event(key="TEST-sibling/1"), scope_id="TEST-scope").disposition == "inserted"
    assert private.project_contains(r"C:\Work\Project", r"c:/work/project/src")
    assert not private.project_contains(r"C:\Work\Project", r"C:\Work\Project2")


def test_overlapping_rules_take_the_stricter_mode_and_close_independently(app):
    _core, _ctx, _clock, owner = app
    owner.private_on(target="session", session="TEST-session", mode="read-only")
    owner.private_on(target="global", mode="full")
    assert owner.private_status(session="TEST-session")["mode"] == "full"
    owner.private_off(target="global")
    assert owner.private_status(session="TEST-session")["mode"] == "read-only"


def test_a_global_duration_expires_without_any_other_command(app):
    _core, _ctx, clock, owner = app
    rule = owner.private_on(target="global", mode="full", duration_seconds=60)
    assert rule["ends_at"] is not None
    clock.advance(30)
    assert owner.private_status()["mode"] == "full"
    clock.advance(31)
    assert owner.private_status()["mode"] == "none"


class CountingVector:
    def __init__(self):
        self.calls = 0

    def search(self, context, *, limit, remaining_seconds):
        self.calls += 1
        return ()


class CatchingModel:
    def __init__(self):
        self.calls = 0

    def propose(self, sources, **_kwargs):
        self.calls += 1
        raise AssertionError("a full private interval must not call the consolidation model")


class CountingEmbed:
    def __init__(self):
        self.sources = []

    def prepare_source(self, source, *, remaining_seconds):
        self.sources.append(source.event["content"])
        return source.event["content"]

    def publish_source(self, prepared, **kwargs):
        return None


def test_global_full_stops_background_outbound_after_a_capture_was_queued(app):
    core, ctx, _clock, owner = app
    core.record_event(ctx, _event(), scope_id="TEST-scope")
    owner.private_on(target="global", mode="full")
    model, embed = CatchingModel(), CountingEmbed()
    core.drain_worker(ctx, max_items=4, consolidation=model, embed=embed)
    assert (model.calls, embed.sources) == (0, [])


def test_a_session_full_holds_its_work_and_lets_another_session_embed(app):
    core, ctx, _clock, owner = app
    first = replace(ctx, session_id="TEST-session-a")
    second = replace(ctx, session_id="TEST-session-b")
    core.record_event(first, _event(key="TEST-a/1", content="TEST session a"), scope_id="TEST-scope")
    core.record_event(second, _event(key="TEST-b/1", content="TEST session b"), scope_id="TEST-scope")
    owner.private_on(target="session", session="TEST-session-a", mode="full")
    embed = CountingEmbed()
    core.drain_worker(ctx, max_items=8, embed=embed)
    assert embed.sources == ["TEST session b"]


def test_cli_contract_covers_on_status_list_off(app, monkeypatch, capsys):
    _core, _ctx, _clock, owner = app
    monkeypatch.setattr(private_cli, "open_management", lambda _args: owner)
    calls = [
        ("on", "--session", "TEST-session", "--mode", "read-only"),
        ("status", "--session", "TEST-session"),
        ("list",),
        ("off", "--session", "TEST-session"),
    ]
    for action, *arguments in calls:
        assert cli.main(["private", action, *arguments, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["format"] == private_cli.FORMAT and payload["action"] == action
        validate_payload("private_mode", payload)
    assert private_cli.FORMAT == "scope-recall-private/1"


def test_previous_schema_migrates_to_private_intervals_without_losing_sources(app):
    core, ctx, _clock, _owner = app
    source = core.record_event(ctx, _event(), scope_id="TEST-scope")
    for index in range(4):
        core.record_event(ctx, _event(key=f"TEST-kept/{index}", content=f"TEST memory {index}"), scope_id="TEST-scope")
    downgrade_store(core.storage.path, 1111)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        before = conn.execute("SELECT * FROM source_events ORDER BY event_id,source_revision").fetchall()
    assert core.initialize().schema_version == SCHEMA_VERSION
    assert core.source(ctx, source.event_refs[0].ref, 1).event["content"] == "TEST private colour is white."
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("SELECT count(*) FROM private_intervals").fetchone()[0] == 0
        after = conn.execute("SELECT * FROM source_events ORDER BY event_id,source_revision").fetchall()
        assert [row[:-1] for row in after] == before


def test_timed_global_can_be_ended_early_and_preserves_its_history(app):
    core, ctx, clock, owner = app
    owner.private_on(target="global", duration_seconds=3600)
    clock.advance(60)
    assert owner.private_off(target="global")["closed"] == 1
    assert owner.private_status()["mode"] == "none"
    assert core.record_event(ctx, _event(occurred_at=clock.utc_now()), scope_id="TEST-scope").disposition == "inserted"
    assert (
        core.record_event(
            ctx, _event(key="TEST-late/1", occurred_at="2026-09-05T12:00:30Z"), scope_id="TEST-scope"
        ).error_code
        == "PRIVATE_MODE"
    )


@pytest.mark.parametrize(
    "target,extra", [("session", {"session": "TEST-session"}), ("project", {"project": "C:/work/app"})]
)
def test_only_global_intervals_accept_a_duration(app, target, extra):
    _core, _ctx, _clock, owner = app
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        owner.private_on(target=target, duration_seconds=60, **extra)


@pytest.mark.parametrize("method", ["profile", "entity", "trace", "collection"])
def test_full_gates_read_tools_before_their_request_is_read(app, method):
    core, ctx, _clock, owner = app
    owner.private_on(target="global")
    with pytest.raises(ContractError) as refused:
        getattr(core, method)(ctx, {})
    assert (refused.value.code, refused.value.field) == ("ACCESS_DENIED", "private_mode")


def test_read_only_alone_refuses_durable_ingress_and_makes_a_query_call(app):
    core, ctx, _clock, owner = app
    vectors = CountingVector()
    reading = MemoryCore(CoreConfig(ctx.binding), storage=core.storage, clock=core.clock, vectors=vectors)
    owner.private_on(target="global", mode="read-only")
    refused = reading.record_host_event(ctx, _event(), scope_id="TEST-scope", host_scope=None)
    assert refused.error_code == "PRIVATE_MODE"
    assert _count(core, ctx, "capture_inbox") == _count(core, ctx, "source_events") == 0
    reading.recall_packet(ctx, recall_request(query="TEST query", mode="current"))
    assert vectors.calls == 1


def test_failed_private_migration_rolls_back_every_schema_write(app, monkeypatch):
    from scope_recall.core import storage

    core, ctx, _clock, _owner = app
    captured = core.record_event(ctx, _event(), scope_id="TEST-scope")
    downgrade_store(core.storage.path, 1111)
    migrate = storage.upgrade_1111

    def interrupted(connection):
        migrate(connection)
        raise RuntimeError("TEST migration interrupted")

    monkeypatch.setattr(storage, "upgrade_1111", interrupted)
    with pytest.raises(RuntimeError, match="TEST migration interrupted"):
        core.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1111
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1111
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='private_intervals'").fetchone()[0] == 0
        assert "project_root" not in {row[1] for row in conn.execute("PRAGMA table_info(source_events)")}
        assert conn.execute("SELECT content FROM source_events").fetchone()[0] == _event()["content"]
    monkeypatch.setattr(storage, "upgrade_1111", migrate)
    core.initialize()
    assert core.source(ctx, captured.event_refs[0].ref, 1).event["content"] == _event()["content"]


@pytest.mark.parametrize("target", ["global", "project", "session"])
def test_background_work_resumes_after_full_ends_without_holding_other_projects(app, target):
    core, ctx, clock, owner = app
    first = replace(ctx, session_id="TEST-session-1", project_root="C:/work/app")
    second = replace(ctx, session_id="TEST-session-2", project_root="C:/work/app2")
    core.record_event(first, _event(key="TEST-one/1", content="TEST one"), scope_id="TEST-scope")
    core.record_event(second, _event(key="TEST-two/1", content="TEST two"), scope_id="TEST-scope")
    args = (
        {"session": first.session_id}
        if target == "session"
        else {"project": first.project_root}
        if target == "project"
        else {}
    )
    owner.private_on(target=target, **args)
    embed = CountingEmbed()
    core.drain_worker(ctx, max_items=8, embed=embed)
    assert embed.sources == ([] if target == "global" else ["TEST two"])
    owner.private_off(target=target, **args)
    clock.advance(301)
    core.drain_worker(ctx, max_items=8, embed=embed)
    assert sorted(embed.sources) == ["TEST one", "TEST two"]


def test_backfill_before_a_private_interval_keeps_its_original_eligibility(app):
    core, ctx, _clock, owner = app
    owner.private_on(target="global")
    receipt = core.record_event(ctx, _event(occurred_at="2026-09-05T11:00:00Z"), scope_id="TEST-scope")
    assert receipt.disposition == "inserted"


def test_pre_upgrade_snapshot_is_accepted_by_the_documented_rollback_path(app, tmp_path):
    from scope_recall.maintenance.backup import backup_sqlite
    from scope_recall.maintenance.restore import apply_restore, plan_restore

    core, ctx, clock, _owner = app
    core.record_event(ctx, _event(occurred_at="2026-09-05T11:00:00Z"), scope_id="TEST-scope")
    downgrade_store(core.storage.path, 1111)
    snapshot = tmp_path / "TEST-before-private.sqlite3"
    backup_sqlite(core.storage.path, snapshot)
    core.initialize()
    _owner.private_on(target="global")
    clock.advance(60)
    _owner.private_off(target="global")
    assert plan_restore(ctx.binding, snapshot)["action"] == "prepare"
    receipt = apply_restore(ctx.binding, snapshot, backup_dir=tmp_path / "TEST-rollback-backups", now=clock.now)
    assert receipt["status"] == "prepared"
    assert receipt["snapshot_schema"] == 1111
    assert (ctx.binding.data_directory / "restore-required.json").is_file()
    with closing(sqlite3.connect(receipt["backup"]["path"])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        mode, ended = conn.execute("SELECT mode,ends_at FROM private_intervals").fetchone()
        assert mode == "full" and ended is not None


def test_worker_interval_times_compare_utc_instants_with_different_offsets(app):
    core, ctx, _clock, owner = app
    owner.private_on(target="global", duration_seconds=60)
    with core.storage.read(ctx) as tx:
        assert private.active_intervals(tx, "2026-09-05T08:00:30-04:00")
        assert not private.active_intervals(tx, "2026-09-05T08:01:00-04:00")


def test_retry_cannot_change_a_recorded_project_boundary(app):
    from scope_recall.core.capture_inbox import enqueue

    core, ctx, clock, _owner = app
    first = replace(ctx, project_root="C:/work/app")
    second = replace(ctx, project_root="C:/work/app2")
    enqueue(core.storage, clock, first, _event(), scope_id="TEST-scope", host_scope=None, remaining_seconds=1)
    with pytest.raises(ContractError) as refused:
        enqueue(core.storage, clock, second, _event(), scope_id="TEST-scope", host_scope=None, remaining_seconds=1)
    assert (refused.value.code, refused.value.field) == ("VERSION_CONFLICT", "ingress_identity")


def test_inbox_without_project_metadata_keeps_its_legacy_retry_identity(app):
    from scope_recall.core.capture_inbox import enqueue

    core, ctx, clock, _owner = app
    first = replace(ctx, project_root="C:/work/app")
    token, _prepared = enqueue(
        core.storage, clock, first, _event(), scope_id="TEST-scope", host_scope=None, remaining_seconds=1
    )
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        body = json.loads(conn.execute("SELECT payload_json FROM capture_inbox WHERE token=?", (token,)).fetchone()[0])
        body["context"].pop("project_root")
        conn.execute("UPDATE capture_inbox SET payload_json=? WHERE token=?", (json.dumps(body), token))
    retried, _prepared = enqueue(
        core.storage, clock, first, _event(), scope_id="TEST-scope", host_scope=None, remaining_seconds=1
    )
    assert retried == token
