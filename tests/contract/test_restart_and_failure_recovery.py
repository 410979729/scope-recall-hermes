"""Release-critical offline failure/restart checks, without model/network calls."""

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore, capture_inbox, delete_storage, inbox_rules
from scope_recall.core.episodes import source_watermark
from scope_recall.core.worker_consolidation import decode_consolidation_result
from scope_recall.maintenance.autostart import plan
from scope_recall.runtime.resume_entry import resume_once
from scope_recall.runtime.scheduling import control_path
from test_consolidation_chunks import long_source, row
from test_deletion import authorize, request
from test_finite_supervisor import NOW, fixture, queue
from test_worker import (
    FakeConsolidation,
    capture,
    consolidation_payload,
    draft,
)
from test_worker import (
    app as app,
)
from test_worker import (
    worker_app as worker_app,
)
from v11_support import downgrade_store, source_event


def test_capture_commit_failure_survives_fresh_process_and_dedupes(worker_app, monkeypatch, tmp_path):
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-durable", content="TEST-project 配色 蓝色。")
    real = capture_inbox.record_event

    def broken(*args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE")

    monkeypatch.setattr(capture_inbox, "record_event", broken)
    receipt = capture_inbox.durable_record_event(
        core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None
    )
    assert receipt.durability == "queued"
    with core.storage.read(ctx) as tx:
        assert tx.status().sources == 0
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1
    monkeypatch.setattr(capture_inbox, "record_event", real)
    # A new OS process gets only binding/context metadata; no in-memory event.
    metadata = dict(
        agent_id=ctx.binding.agent_id,
        installation_id=ctx.binding.installation_id,
        data_directory=str(ctx.binding.data_directory),
        scope_ids=sorted(ctx.binding.scope_ids),
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        session_id=ctx.session_id,
    )
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    code = """import json,sys
from pathlib import Path
from scope_recall.contracts import InstanceBinding,TrustedContext
from scope_recall.core import CoreConfig,MemoryCore
from scope_recall.core.capture_inbox import replay_inbox
m=json.loads(Path(sys.argv[1]).read_text()); b=InstanceBinding(m['agent_id'],m['installation_id'],Path(m['data_directory']),frozenset(m['scope_ids']),True)
c=TrustedContext(b,m['session_id'],b.scope_ids,'host_generated',project_id=m['project_id'],branch_id=m['branch_id'])
core=MemoryCore(CoreConfig(b));r=replay_inbox(core.storage,core.clock,c,authorize=lambda _: b.scope_ids,remaining_seconds=5)
assert len(r)==1 and r[0].durability=='persisted'
print('fresh-process-replay-ok')"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "fresh-process-replay-ok" in result.stdout
    duplicate = capture_inbox.durable_record_event(
        core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None
    )
    assert duplicate.disposition == "duplicate"
    with core.storage.read(ctx) as tx:
        assert tx.status().sources == 1
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_revocation_and_deletion_cancel_pending_ingress(worker_app):
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-revoked", content="TEST-project 配色 蓝色。")
    capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    receipt = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: frozenset())[0]
    assert receipt.disposition == "cancelled" and core.status(ctx).sources == 0
    source = capture(core, ctx, "TEST 已有资料。")
    # A capture of the deleted words waiting in the inbox is cancelled with them; another is stored (rc13).
    copy = source_event(source_event_key="TEST-copy", content="TEST 已有资料。")
    capture_inbox.enqueue(core.storage, clock, ctx, copy, scope_id="TEST-scope", host_scope=None)
    capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    authorize(core, ctx, source)
    core.forget(ctx, request(source), remaining_seconds=5)
    receipts = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert [receipt.durability for receipt in receipts] == ["persisted"]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute(
            "SELECT source_event_key FROM source_events WHERE source_event_key IN ('TEST-revoked','TEST-copy')"
        ).fetchall() == [("TEST-revoked",)]


def test_ingress_rejects_secrets_conflicts_and_other_partitions(worker_app):
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-collision", content="TEST original")
    capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    # The same words sent again with other evidence are refused; other words are another message (the next test).
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        capture_inbox.enqueue(
            core.storage, clock, ctx, dict(event, capture_state="partial"), scope_id="TEST-scope", host_scope=None
        )
    assert (
        capture_inbox.replay_inbox(
            core.storage, clock, replace(ctx, project_id="TEST-foreign"), authorize=lambda _: ctx.allowed_scope_ids
        )
        == ()
    )
    token, prepared = capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        dict(event, source_event_key="TEST-secret", content="api_key=sk-" + "abcd" * 12),
        scope_id="TEST-scope",
        host_scope=None,
    )
    assert token is None and prepared.rejection == "plaintext_secret_rejected"


def test_a_row_an_older_release_left_as_source_missing_is_replayed_once(worker_app, monkeypatch):
    """Before 3.4.0rc10 a capture into a task whose episode was deleted failed as a bare ``SOURCE_MISSING`` and stayed
    in the inbox for good (three rows on the pilot, a doctor gap every day).  Such a row is replayed once more; a
    missing source is now written with its field, so a failure after that replay stays final."""
    core, ctx, clock = worker_app
    stored = source_event(source_event_key="TEST-legacy-missing", content="TEST 删完以后接着聊。")
    failing = source_event(source_event_key="TEST-legacy-failing", content="TEST 还是存不进去。")
    for event in (stored, failing):
        capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE capture_inbox SET last_error_code='SOURCE_MISSING'")
        conn.commit()
    original = capture_inbox.record_event

    def record(storage, clock, context, value, **options):
        if options["_prepared"].events[0]["content"] == failing["content"]:
            raise ContractError("SOURCE_MISSING", "TEST-still-missing")
        return original(storage, clock, context, value, **options)

    monkeypatch.setattr(capture_inbox, "record_event", record)
    receipts = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert sorted(receipt.disposition for receipt in receipts) == ["inserted", "queued"]
    with sqlite3.connect(core.storage.path) as conn:
        left = conn.execute("SELECT last_error_code FROM capture_inbox").fetchall()
    assert left == [("SOURCE_MISSING:TEST-still-missing",)]
    assert capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids) == ()


def test_long_resume_appears_only_after_all_pages_and_includes_last_progress(worker_app):
    core, ctx, clock = worker_app
    goal = "请帮我完成 TEST 报告整理。"
    progress = "TEST 报告资料已确认完成。"
    source = long_source(core, ctx, goal + "\n" + "这是一段归档资料；" * 1800 + "\n" + progress)

    def build(sources, episode_ref=None):
        page = sources[0]
        refs = [f"{page.ref}@{page.revision}"]
        seed = getattr(page, "consolidation_seed", ())
        resumes = []
        if goal in page.event["content"] or seed:
            resumes = [
                dict(
                    episode_ref=episode_ref,
                    goal=dict(text=goal, evidence_refs=refs),
                    decisions=[],
                    verified_progress=[dict(text=progress, evidence_refs=refs)]
                    if progress in page.event["content"]
                    else [],
                    open_items=[],
                    blockers=[],
                    next_step=None,
                    next_step_basis="unknown",
                    artifact_refs=[],
                    source_watermark=source_watermark(refs),
                    evidence_refs=refs,
                )
            ]
        return consolidation_payload(page, resume_proposals=resumes)

    for _ in range(40):
        receipt = core.drain_worker(ctx, consolidation=FakeConsolidation(build), max_items=1, remaining_seconds=5)
        assert receipt.failed == receipt.retried == 0, receipt
        with sqlite3.connect(core.storage.path) as db:
            resumes = db.execute("SELECT resume_json FROM episode_versions WHERE resume_json IS NOT NULL").fetchall()
        if row(core, source)[0] == "done":
            break
        assert resumes == []
        core = MemoryCore(CoreConfig(ctx.binding), clock=clock)
    assert row(core, source)[1] == len(source.event["content"])
    assert resumes and progress in resumes[-1][0]
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT disposition FROM consolidation_outcomes").fetchone()[0] == "complete"
        assert db.execute("SELECT count(*) FROM consolidation_fragments").fetchone()[0] == 0


def test_decoder_repairs_unique_serialized_quote_but_never_fuzzy_support(worker_app):
    core, ctx, _ = worker_app
    raw = json.dumps({"note": r"TEST-project 路径 C:\work\report.txt"}, ensure_ascii=False)
    source = capture(core, ctx, raw)
    quote = r"TEST-project 路径 C:\work\report.txt"
    p = draft(
        source,
        value=r"C:\work\report.txt",
        predicate="路径",
        evidence_spans=[dict(source_ref=source.ref, source_revision=1, quote=quote)],
        procedure={},
    )
    value = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[p])), (source,))
    assert "procedure" not in value["claim_proposals"][0]
    assert value["claim_proposals"][0]["evidence_spans"][0]["quote"] in raw
    p["evidence_spans"][0]["quote"] = "TEST invented quote"
    value = decode_consolidation_result(json.dumps(consolidation_payload(source, claims=[p])), (source,))
    assert value["claim_proposals"][0]["evidence_spans"][0]["quote"] == "TEST invented quote"


def test_external_wake_due_future_pause_and_task_plan(tmp_path):
    core, cfg, oldpath = fixture(tmp_path)
    path = cfg.binding.data_directory / "runtime.json"
    path.write_bytes(oldpath.read_bytes())
    prepared = plan(path, Path(sys.executable), user_id="TEST-user")
    assert "LogonTrigger" in prepared["xml"] and "PT5M" in prepared["xml"] and "LeastPrivilege" in prepared["xml"]
    control = {k: v for k, v in prepared.items() if k != "xml"}
    control_path(cfg).write_text(json.dumps(control))
    launched = []

    def launch(*args, **kwargs):
        launched.append((args, kwargs))
        return SimpleNamespace(pid=99)

    assert not resume_once(path, launcher=launch, now=NOW)["launched"]
    queue(core, cfg, due=NOW + timedelta(minutes=1))
    assert not resume_once(path, launcher=launch, now=NOW)["launched"]
    assert resume_once(path, launcher=launch, now=NOW + timedelta(minutes=2))["launched"]
    control["enabled"] = False
    control_path(cfg).write_text(json.dumps(control))
    assert resume_once(path, launcher=launch, now=NOW + timedelta(minutes=3))["status"] == "paused"
    assert len(launched) == 1


def test_external_wake_still_launches_after_ten_thousand_items_in_a_day(tmp_path):
    core, cfg, oldpath = fixture(tmp_path)
    path = cfg.binding.data_directory / "runtime.json"
    path.write_bytes(oldpath.read_bytes())
    prepared = plan(path, Path(sys.executable), user_id="TEST-user")
    control_path(cfg).write_text(json.dumps({k: v for k, v in prepared.items() if k != "xml"}))
    (cfg.binding.data_directory / "runtime-worker-day.json").write_text(
        json.dumps(dict(installation_id=cfg.binding.installation_id, day=NOW.isoformat()[:10], used=10_001))
    )
    queue(core, cfg, due=NOW)
    launched = []

    def launch(*args, **kwargs):
        launched.append((args, kwargs))
        return SimpleNamespace(pid=99)

    assert resume_once(path, launcher=launch, now=NOW)["launched"]
    assert len(launched) == 1


def test_restore_cancels_stale_inbox_and_fences_replay(worker_app, tmp_path):
    from test_deletion import (
        InstallationMaintenance,
        begin_restore,
        export_deletion_ledger,
        ledger_digest,
        replay_deletion_ledger,
        sqlite_backup,
    )

    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-before-restore", content="TEST queued before restore")
    capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    backup = tmp_path / "before.sqlite3"
    sqlite_backup(core.storage.path, backup)
    authority = InstallationMaintenance(ctx)
    ledger = export_deletion_ledger(core.storage, authority)
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup, core.storage.path)
    replay_deletion_ledger(core.storage, authority, ledger)
    assert capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids) == ()
    assert core.status(ctx).sources == 0


def test_1106_failure_upgrade_preserves_history_and_requeues_exactly_once(worker_app):
    core, ctx, _ = worker_app
    source = capture(core, ctx, "TEST upgrade failure evidence")
    with sqlite3.connect(core.storage.path) as db:
        db.execute(
            "UPDATE work_items SET state='failed',attempt=3,last_error_code='DERIVATION_INVALID' WHERE work_type='consolidate'"
        )
    downgrade_store(core.storage.path, 1106)
    core.initialize()
    assert row(core, source) == ("pending", 0, 0)
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT stage,error_code FROM work_error_details").fetchone() == (
            "upgrade_1106",
            "DERIVATION_INVALID",
        )
        db.execute(
            "UPDATE work_items SET state='failed',attempt=3,last_error_code='DERIVATION_INVALID' WHERE work_type='consolidate'"
        )
    core.initialize()
    assert row(core, source) == ("failed", 0, 3)


def test_backup_and_rollback_cli_preview_is_readonly_and_protects_inbox(worker_app, tmp_path, capsys):
    from scope_recall.maintenance.cli import main

    core, ctx, clock = worker_app
    snapshot = tmp_path / "verified.sqlite3"
    assert main(["backup", "--database", str(core.storage.path), "--output", str(snapshot)]) == 0
    manifest = json.loads(snapshot.with_suffix(".sqlite3.json").read_text())
    assert manifest["quick_check"] == "ok"
    capture_inbox.enqueue(
        core.storage, clock, ctx, source_event(content="TEST pending ingress"), scope_id="TEST-scope", host_scope=None
    )
    before = core.storage.path.read_bytes()
    assert main(["rollback", "--current-db", str(core.storage.path), "--snapshot", str(snapshot)]) == 0
    assert (
        core.storage.path.read_bytes() == before and not (ctx.binding.data_directory / "restore-required.json").exists()
    )
    assert main(["rollback", "--current-db", str(core.storage.path), "--snapshot", str(snapshot), "--apply"]) == 0
    assert (ctx.binding.data_directory / "restore-required.json").is_file()
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1


def test_key_collided_capture_is_stored_under_its_own_identity(worker_app):
    """A reused turn number must not lose the second message.

    Storage refuses a second, different message under an existing key: same
    event id and revision, different fingerprint. ``replay_inbox`` then never
    touches the row again, because it only retries failures that could clear on
    their own — so the payload sat in the inbox permanently, captured but never
    stored, visible only as a doctor gap.
    """
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-42", content="TEST first message")
    assert (
        capture_inbox.durable_record_event(
            core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None
        ).durability
        == "persisted"
    )

    # A different session reuses the same key for different content: the inbox
    # token differs, so this enqueues, and the collision only surfaces at commit.
    other = replace(ctx, session_id="TEST-session-2")
    collided = capture_inbox.durable_record_event(
        core.storage,
        clock,
        other,
        dict(first, content="TEST second message"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    assert collided.disposition == "conflict" and collided.error_code == "VERSION_CONFLICT"

    with sqlite3.connect(core.storage.path) as conn:
        blocked = conn.execute(
            "SELECT count(*) FROM capture_inbox WHERE last_error_code='VERSION_CONFLICT'"
        ).fetchone()[0]
    assert blocked == 1, "the payload is held, not discarded"

    # Replay cannot help: the identity collides by construction, so the row is
    # outside its filter entirely.
    assert capture_inbox.replay_inbox(core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids) == ()

    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    )
    assert len(receipts) == 1 and receipts[0].durability == "persisted"

    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        stored = conn.execute("SELECT source_event_key,content FROM source_events ORDER BY rowid").fetchall()
    contents = [row["content"] for row in stored]
    assert "TEST first message" in contents and "TEST second message" in contents, (
        "both messages survive; neither hides the other"
    )
    keys = [row["source_event_key"] for row in stored]
    assert "TEST-turn-42" in keys, "the original keeps its identity"
    rekeyed = [key for key in keys if key.startswith("TEST-turn-42#rekey:")]
    assert len(rekeyed) == 1, "the collision stays visible in the stored key"

    # Idempotent: nothing is left to repair, and a second pass adds nothing.
    assert (
        capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
        )
        == ()
    )


def test_every_message_sent_into_one_running_turn_is_stored(worker_app):
    """Codex gives a message sent into a running turn that turn's id.  The second such message conflicts with the first
    and waits in the inbox for a key of its own; a third sent before that pass came under the second's place in the
    inbox, was refused as changed evidence and lost (the work computer lost two that way on 2026-09-30)."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-7", content="TEST 先看一下日志")
    assert (
        capture_inbox.durable_record_event(
            core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None
        ).durability
        == "persisted"
    )
    later = [dict(first, content=text) for text in ("TEST 顺便查一下锁", "TEST 好")]
    receipts = [
        capture_inbox.durable_record_event(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
        for event in later
    ]
    assert [(receipt.disposition, receipt.error_code) for receipt in receipts] == [("conflict", "VERSION_CONFLICT")] * 2
    stored = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=5
    )
    assert [receipt.durability for receipt in stored] == ["persisted", "persisted"]
    # A hook sent again with the same words (the work computer's client resends what the store was too busy for)
    # finds the message it stored.
    again = capture_inbox.durable_record_event(
        core.storage, clock, ctx, later[1], scope_id="TEST-scope", host_scope=None
    )
    assert again.disposition == "conflict"
    assert [
        receipt.disposition
        for receipt in capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=5
        )
    ] == ["duplicate"]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        contents = sorted(content for (content,) in conn.execute("SELECT content FROM source_events"))
    assert contents == sorted(["TEST 先看一下日志", "TEST 顺便查一下锁", "TEST 好"])


def _a_pass(core, clock, ctx):
    """What a worker pass does with the inbox (``runtime.instance._replay_ingress``)."""
    authorize_all = dict(authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=5)
    return (
        *capture_inbox.replay_inbox(core.storage, clock, ctx, **authorize_all),
        *capture_inbox.resolve_conflicted_ingress(core.storage, clock, ctx, **authorize_all),
    )


def _first_of_the_turn_waits(core, clock, ctx, monkeypatch, key):
    """The turn's first message, whose commit met a busy store: it waits in the inbox for the next pass."""
    first = source_event(source_event_key=key, content="TEST 第一句")
    real = capture_inbox.record_event

    def busy(*args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE")

    monkeypatch.setattr(capture_inbox, "record_event", busy)
    assert (
        capture_inbox.durable_record_event(
            core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None
        ).durability
        == "queued"
    )
    monkeypatch.setattr(capture_inbox, "record_event", real)
    second = dict(first, content="TEST 第二句", occurred_at="2026-09-05T12:00:04Z", recorded_at="2026-09-05T12:00:04Z")
    stored = capture_inbox.durable_record_event(
        core.storage, clock, ctx, second, scope_id="TEST-scope", host_scope=None
    )
    assert stored.durability == "persisted", "the second message takes the key while the first waits"
    return stored.event_refs[0]


def test_a_message_sent_while_the_turn_s_first_still_waits_is_stored(worker_app, monkeypatch):
    """Before 3.4.6 the second message was refused while the first waited; now both are stored, the first under a key
    of its own once its turn comes (review of 3.4.6)."""
    core, ctx, clock = worker_app
    _first_of_the_turn_waits(core, clock, ctx, monkeypatch, "TEST-turn-9")
    _a_pass(core, clock, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        keys = dict(conn.execute("SELECT content,source_event_key FROM source_events"))
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
    assert keys["TEST 第二句"] == "TEST-turn-9" and keys["TEST 第一句"].startswith("TEST-turn-9#rekey:")


def test_deleting_one_message_keeps_another_waiting_under_its_key(worker_app, monkeypatch):
    """A delete cancels a waiting capture that holds the deleted message, not every capture of its source group: the
    turn's first message, still waiting when the second, stored under the key, was deleted, was cancelled with it and
    lost, never deleted itself (review of 3.4.6).  A later version of the deleted message is still cancelled."""
    core, ctx, clock = worker_app
    deleted = _first_of_the_turn_waits(core, clock, ctx, monkeypatch, "TEST-turn-10")
    later = source_event(source_event_key="TEST-turn-10", source_revision=2, content="TEST 第二句（改）")
    capture_inbox.enqueue(core.storage, clock, ctx, later, scope_id="TEST-scope", host_scope=None)
    authorize(core, ctx, deleted)
    core.forget(ctx, request(deleted), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1, "the later version is cancelled"
    _a_pass(core, clock, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        kept = conn.execute("SELECT content,source_event_key FROM source_events WHERE read_blocked=0").fetchall()
    assert [content for content, _key in kept] == ["TEST 第一句"] and kept[0][1].startswith("TEST-turn-10#rekey:")


def test_the_same_words_sent_twice_into_one_turn_before_the_pass_are_kept_once(worker_app):
    """The same words under the same key while the first still waits are the same capture: a retried hook sends them
    so, with a later moment, and the first moment is kept."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-12", content="TEST 开始")
    assert (
        capture_inbox.durable_record_event(
            core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None
        ).durability
        == "persisted"
    )
    for moment in ("2026-09-05T12:00:03Z", "2026-09-05T12:00:08Z"):
        capture_inbox.durable_record_event(
            core.storage,
            clock,
            ctx,
            dict(first, content="TEST 继续", occurred_at=moment, recorded_at=moment),
            scope_id="TEST-scope",
            host_scope=None,
        )
    _a_pass(core, clock, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        kept = conn.execute("SELECT occurred_at FROM source_events WHERE content='TEST 继续'").fetchall()
    assert kept == [("2026-09-05T12:00:03Z",)]


def test_a_long_message_whose_key_was_taken_is_stored_under_a_new_group(worker_app):
    """Given a new key, each segment of a long message kept the first message's group, met it again and was never
    stored (review of 3.4.0rc10).  The segments now move into one new group."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-77", content="TEST 第一条很长的消息。" * 6000)
    assert (
        capture_inbox.durable_record_event(
            core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None
        ).durability
        == "persisted"
    )
    other = replace(ctx, session_id="TEST-session-2")
    collided = capture_inbox.durable_record_event(
        core.storage,
        clock,
        other,
        dict(first, content="TEST 第二条很长的消息。" * 6000),
        scope_id="TEST-scope",
        host_scope=None,
    )
    assert collided.disposition == "conflict"
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    )
    assert len(receipts) == 1 and receipts[0].durability == "persisted"
    with sqlite3.connect(core.storage.path) as conn:
        groups = conn.execute("SELECT source_group_key,count(*) FROM source_events GROUP BY 1 ORDER BY 1").fetchall()
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
    assert [count for _group, count in groups] == [2, 2] and groups[0][0] == "TEST-turn-77"
    assert groups[1][0].startswith("TEST-turn-77#rekey:")


def test_a_row_that_cannot_be_checked_again_is_put_off_and_the_rows_after_it_are_stored(worker_app, monkeypatch):
    """A row whose stored capture raised on revalidation stopped its whole page, on every pass.  Made final instead,
    a row this release merely could not read yet (a newer release's field, an installation being reinstalled) was
    never stored (reviews of 3.4.0rc10).  It is put off, and the replay that put it off takes it again in a minute."""
    core, ctx, clock = worker_app
    other = replace(ctx, session_id="TEST-session-2")
    for key in ("TEST-turn-50", "TEST-turn-51"):
        first = source_event(source_event_key=key, content=f"TEST first {key}")
        capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
        capture_inbox.durable_record_event(
            core.storage,
            clock,
            other,
            dict(first, content=f"TEST second {key}"),
            scope_id="TEST-scope",
            host_scope=None,
        )
    original = capture_inbox.prepare_capture

    def prepare(event, context):
        if "TEST-turn-50" in event["source_event_key"]:
            raise ContractError("INPUT_INVALID", "TEST-envelope")
        return original(event, context)

    monkeypatch.setattr(capture_inbox, "prepare_capture", prepare)
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    )
    assert sorted(receipt.disposition for receipt in receipts) == ["inserted", "queued"]
    with sqlite3.connect(core.storage.path) as conn:
        [(code,)] = conn.execute("SELECT last_error_code FROM capture_inbox").fetchall()
        assert (
            conn.execute("SELECT count(*) FROM source_events WHERE content='TEST second TEST-turn-51'").fetchone()[0]
            == 1
        )
    assert code.startswith(f"DEFERRED|{inbox_rules.__version__}|")
    assert code.endswith("|1|rekey|INPUT_INVALID:TEST-envelope"), "the field that failed is named"
    # Not taken again before its minute is up; still waiting as far as a record read is concerned.
    assert (
        capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
        )
        == ()
    )
    assert inbox_rules.waiting(code)
    # Once it reads, the collision is stored under a new key when its minute is up.
    monkeypatch.setattr(capture_inbox, "prepare_capture", original)
    clock.advance(seconds=61, iso="2026-09-06T12:01:01Z")
    assert [
        r.durability
        for r in capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
        )
    ] == ["persisted"]
    with sqlite3.connect(core.storage.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM source_events WHERE content='TEST second TEST-turn-50'").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_a_row_put_off_again_waits_longer_and_is_given_up_where_it_shows():
    """A row put off was tried again every hour for ever (review of 3.4.0rc10), and after a reinstall an hour was
    long to wait.  It is tried after a minute, doubling to an hour, and given up after a day's worth of tries."""
    from datetime import datetime, timezone

    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    code, waits = None, []
    for _attempt in range(inbox_rules.DEFER_ATTEMPTS):
        code = inbox_rules.deferral(code, ContractError("IDENTITY_UNBOUND", "TEST|host"), now, path="replay")
        waits.append((inbox_rules.deferred_until(code, now) - now).total_seconds())
    assert waits[:4] == [60, 120, 240, 480] and waits[-1] == 3600
    assert code.endswith("|24|replay|IDENTITY_UNBOUND:TEST/host"), "the field is named, its bar replaced"
    given_up = inbox_rules.deferral(code, RuntimeError("TEST"), now, path="replay")
    assert given_up == f"GAVE_UP|{inbox_rules.__version__}|25|replay|RuntimeError", "its failures, the first too"
    assert inbox_rules.given_up(given_up)
    assert not inbox_rules.replayable(given_up, now) and not inbox_rules.waiting(given_up)
    # Given up for every release, and the tries another release made count: a new release reset them, so a row
    # that failed under each was tried for ever (review of 3.4.0rc10).
    assert not inbox_rules.replayable("GAVE_UP|0.0.1|24|replay|RuntimeError", now)
    other = f"DEFERRED|0.0.1|2026-09-28T11:00:00Z|{inbox_rules.DEFER_ATTEMPTS}|replay|TypeError"
    assert inbox_rules.deferral(other, RuntimeError("TEST"), now, path="replay").startswith("GAVE_UP|")
    assert not inbox_rules.replayable(other.replace("11:00:00Z", "12:30:00Z"), now), "another release's wait holds"
    # A time without its zone, further off than any wait, or a code of another shape is due now rather than a crash
    # or a wait for ever.
    version = inbox_rules.__version__
    assert inbox_rules.replayable(f"DEFERRED|{version}|2026-09-28T12:30:00|1|replay|TEST", now)
    assert inbox_rules.replayable(f"DEFERRED|{version}|2026-12-01T00:00:00Z|1|replay|TEST", now)
    assert inbox_rules.replayable(f"DEFERRED|{version}|2026-09-28T12:30:00Z|1|TEST", now)
    assert not inbox_rules.replayable(f"DEFERRED|{version}|2026-09-28T12:30:00Z|1|replay|TEST", now)
    assert inbox_rules.deferred_path(f"DEFERRED|{version}|2026-09-28T12:30:00Z|1|rekey|TEST") == "rekey"


def test_a_delete_keeps_a_waiting_row_unless_it_holds_the_deleted_words(worker_app):
    """A delete cancels its partition's pending captures that hold a deleted message, so that a delayed one cannot
    undo it.  Every other row is kept: a row put off waits for hours (review of 3.4.0rc10), and a capture of another
    client waiting for the next pass was cancelled with the whole partition, words nothing had forgotten (rc13).
    Under the deleted message's key, a later version is cancelled and another message is kept, as storage takes them
    when they come after the delete (``storage.refuse_under_a_deleted_key``): the whole source group was cancelled,
    and Codex sends every message of a running turn under the turn's key (review of 3.4.6)."""
    core, ctx, clock = worker_app
    later = f"DEFERRED|{inbox_rules.__version__}|2026-09-06T13:00:00Z|1|replay|RuntimeError"
    given_up = f"GAVE_UP|{inbox_rules.__version__}|24|rekey|RuntimeError"
    long_kept, long_same = "TEST 暂缓的长消息。" * 8000, "TEST 同组的另一版长消息。" * 6000
    long_other = "TEST 同一个键下的另一条长消息。" * 6000
    rows = (
        ("TEST-put-off-keep", "TEST 暂缓的另一句话。", later, 1),
        ("TEST-put-off-same", "TEST 要删掉的话。", later, 1),
        ("TEST-plain-waiting", "TEST 普通等待的一句。", None, 1),
        ("TEST-plain-busy", "TEST 存储忙时等着的一句。", "STORAGE_UNAVAILABLE", 1),
        # Waiting for the next pass and holding the deleted words, under a key of its own or the deleted one's.
        ("TEST-plain-copy", "TEST 要删掉的话。", None, 1),
        ("TEST-stored-same", "TEST 同一条的下一版。", None, 2),
        # Another message under the deleted one's key.
        ("TEST-stored-same", "TEST 同一个键下的另一句。", None, 1),
        # A long message waits as segments: one of another group is kept, a later version of the deleted one is
        # not, and another message under its key is.
        ("TEST-long-kept", long_kept, given_up, 1),
        ("TEST-stored-long", long_same, later, 2),
        ("TEST-stored-long", long_other, later, 1),
    )
    for key, text, code, revision in rows:
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key=key, source_revision=revision, content=text),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
    source = capture(core, ctx, "TEST 要删掉的话。", key="TEST-stored-same")
    long_source_ = capture(core, ctx, "TEST 已存的长消息。" * 8000, key="TEST-stored-long")
    authorize(core, ctx, source, long_source_)
    core.forget(ctx, request(source, long_source_), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        events = [json.loads(row[0])["events"][0] for row in conn.execute("SELECT payload_json FROM capture_inbox")]
    left = sorted(
        (event["segment"]["group_key"] if "segment" in event else event["source_event_key"], event["source_revision"])
        for event in events
    )
    assert left == [
        ("TEST-long-kept", 1),
        ("TEST-plain-busy", 1),
        ("TEST-plain-waiting", 1),
        ("TEST-put-off-keep", 1),
        ("TEST-stored-long", 1),
        ("TEST-stored-same", 1),
    ]


def _inbox_rows(core):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0]


def _keys_like(core, prefix):
    with sqlite3.connect(core.storage.path) as conn:
        return sorted(
            conn.execute("SELECT content FROM source_events WHERE source_event_key LIKE ?", (prefix + "%",)).fetchall()
        )


def test_a_copy_of_a_deleted_message_under_its_key_leaves_the_inbox(worker_app):
    """A copy of a deleted message under that message's key is refused for good.  It stayed in the inbox with its
    code, and kept the doctor's ``capture_ingress_blocked`` and the patrol's line up until someone removed it by hand
    (rc13).  It leaves the inbox, and the hook and the pass are told it was cancelled.  The words spaced otherwise are
    the same words while the deleted text is kept, as a delete compares them (``inbox_rules.holds``)."""
    from scope_recall.runtime.worker_entry import _ingress_report

    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 要删掉的一句。", key="TEST-deleted-key")
    authorize(core, ctx, source)
    core.forget(ctx, request(source), remaining_seconds=5)
    # A hook's capture of the same words, and of them spaced otherwise.
    for content in ("TEST 要删掉的一句。", "TEST  要删掉的\n一句。"):
        receipt = capture_inbox.durable_record_event(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key="TEST-deleted-key", content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        assert (receipt.disposition, receipt.durability, receipt.error_code) == (
            "cancelled",
            "not_persisted",
            "ACCESS_DENIED",
        )
        assert capture_inbox.SOURCE_DELETED_GAP in receipt.gaps and _inbox_rows(core) == 0
    # One a pass replays: the hook's own commit did not run.
    capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        source_event(source_event_key="TEST-deleted-key", content="TEST 要删掉的一句。"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    receipts = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert [receipt.disposition for receipt in receipts] == ["cancelled"] and _inbox_rows(core) == 0
    assert _ingress_report(receipts)[0]["ingress_cancelled"] == 1
    assert _keys_like(core, "TEST-deleted-key") == [("TEST 要删掉的一句。",)], "the deleted row alone"


def test_another_message_under_a_deleted_message_s_key_is_stored_under_its_own(worker_app):
    """A restarted Hermes gateway numbers its turns from 1 again, and a delete removes its own command's key: the next
    message at that turn was refused, and with rc13's first version dropped without a trace (review of rc13).  It is
    a key collision, stored under a key of its own, before the delete is purged and after; once purged, the deleted
    message's digest still refuses a copy."""
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 要删掉的一句。", key="TEST-deleted-key")
    command = authorize(core, ctx, source)
    operation = core.forget(ctx, request(source), remaining_seconds=5)

    def store(key, content):
        return capture_inbox.durable_record_event(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key=key, content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )

    def resolve():
        return capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=5
        )

    turn = command.event["source_event_key"]
    for key, content in (
        (turn, "TEST 重启后同一回合的新消息。"),
        ("TEST-deleted-key", "TEST 同一个键上说的另一件事。"),
    ):
        assert store(key, content).disposition == "conflict"
    assert [receipt.durability for receipt in resolve()] == ["persisted", "persisted"] and _inbox_rows(core) == 0
    assert _keys_like(core, turn + inbox_rules.REKEY_MARKER) == [("TEST 重启后同一回合的新消息。",)]
    core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    assert store("TEST-deleted-key", "TEST 再换一件事。").disposition == "conflict"
    assert [receipt.durability for receipt in resolve()] == ["persisted"]
    assert _keys_like(core, "TEST-deleted-key" + inbox_rules.REKEY_MARKER) == [
        ("TEST 再换一件事。",),
        ("TEST 同一个键上说的另一件事。",),
    ]
    assert store("TEST-deleted-key", "TEST 要删掉的一句。").disposition == "cancelled", "its digest outlasts the purge"
    assert _inbox_rows(core) == 0


def test_a_host_check_that_raises_puts_off_that_row_only(worker_app):
    """Hermes' identity errors are RuntimeErrors: one raised for a single row stopped the whole page on every pass."""
    core, ctx, clock = worker_app
    for key in ("TEST-host-a", "TEST-host-b"):
        capture_inbox.enqueue(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key=key, content=f"TEST {key}"),
            scope_id="TEST-scope",
            host_scope={"TEST": key},
        )

    def authorize(host_scope):
        if host_scope["TEST"] == "TEST-host-a":
            raise RuntimeError("TEST no such entry")
        return ctx.allowed_scope_ids

    receipts = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=authorize)
    assert sorted(receipt.disposition for receipt in receipts) == ["inserted", "queued"]
    with sqlite3.connect(core.storage.path) as conn:
        [(code,)] = conn.execute("SELECT last_error_code FROM capture_inbox").fetchall()
    assert code.endswith("|1|replay|RuntimeError")


def test_a_row_the_rekey_path_put_off_is_taken_again_by_that_path_alone_and_its_tries_count(worker_app, monkeypatch):
    """A row put off while it was given a new key was due for the plain replay, which met the old collision and wrote
    it back as a bare conflict: its tries began again each round, and it was never given up (review of 3.4.0rc10).
    Only the rekey path takes it again, once its time is up, and it counts on."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-60", content="TEST first TEST-turn-60")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(
        core.storage,
        clock,
        other,
        dict(first, content="TEST second TEST-turn-60"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    original = capture_inbox.prepare_capture

    def prepare(event, context):
        if inbox_rules.REKEY_MARKER in event["source_event_key"]:
            raise ContractError("IDENTITY_UNBOUND", "TEST-host")
        return original(event, context)

    def code():
        with sqlite3.connect(core.storage.path) as conn:
            return conn.execute("SELECT last_error_code FROM capture_inbox").fetchone()[0]

    def replay():
        return capture_inbox.replay_inbox(core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids)

    def resolve():
        return capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
        )

    monkeypatch.setattr(capture_inbox, "prepare_capture", prepare)
    for attempt, moment in enumerate(("2026-09-06T12:00:00Z", "2026-09-06T12:02:00Z", "2026-09-06T12:05:00Z"), 1):
        clock.advance(seconds=1, iso=moment)
        assert replay() == ()
        assert [receipt.error_code for receipt in resolve()] == ["DEFERRED"]
        assert code().split("|")[3:] == [str(attempt), "rekey", "IDENTITY_UNBOUND:TEST-host"]
    clock.advance(seconds=1, iso="2026-09-06T13:00:00Z")
    assert replay() == (), "due, it is still the rekey path's"
    monkeypatch.setattr(capture_inbox, "prepare_capture", original)
    assert [receipt.durability for receipt in resolve()] == ["persisted"]
    with sqlite3.connect(core.storage.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM source_events WHERE content='TEST second TEST-turn-60'").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_retry_failures_returns_a_given_up_capture_to_the_replay(worker_app):
    """A row given up stayed in the inbox with nothing to take it once its cause was fixed (review of 3.4.0rc10).
    ``retry-failures`` counts such rows, and with ``--apply`` returns them to the replay, their tries anew."""
    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        source_event(source_event_key="TEST-given-up", content="TEST 放弃过的一句。"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    given_up = f"GAVE_UP|{inbox_rules.__version__}|24|replay|IDENTITY_UNBOUND:TEST-host"
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (given_up, token))
        conn.commit()

    def replay():
        return capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)

    assert replay() == ()
    preview = core.operations.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (preview["inbox_given_up"], preview["applied"]) == (1, False)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [(given_up,)]
    assert preview["inbox_by_kind"] == {"IDENTITY_UNBOUND:TEST-host": 1}
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["inbox_given_up"] == 1
    assert [receipt.durability for receipt in replay()] == ["persisted"]
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=True)["inbox_given_up"] == 0


def test_a_message_under_a_deleted_key_is_compared_whole_and_its_versions_refused(worker_app):
    """Compared part by part, a long deleted message sent again with its first character changed was stored under a
    new key, and its second part, word for word, was found again; a later version of a deleted message was stored; a
    copy spaced otherwise came back once the delete was purged; and a message of another length under a deleted
    message's key was dropped (review of rc13).  The whole message is compared, a version the deleted group never had
    is refused, the purge keeps the forms of the deleted words, and another message is a key collision."""
    core, ctx, clock = worker_app
    long_text = "".join(f"TEST 第{i}句要删的长话。" for i in range(6000))
    short = "TEST 要删的 一句 短话。"
    stored = [capture(core, ctx, long_text, key="TEST-long"), capture(core, ctx, short, key="TEST-short")]
    authorize(core, ctx, *stored)
    operation = core.forget(ctx, request(*stored), remaining_seconds=5)

    def store(key, content, **changes):
        return capture_inbox.durable_record_event(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key=key, content=content, **changes),
            scope_id="TEST-scope",
            host_scope=None,
        ).disposition

    # Before the purge: its first character changed, or eight characters put before it, the long message is a copy.
    assert store("TEST-long", "!" + long_text[1:]) == "cancelled"
    assert store("TEST-long", "[10:02] " + long_text) == "cancelled"
    # A later version is refused, as the deletion contract says.
    assert store("TEST-short", "TEST 改过的一句。", source_revision=2) == "cancelled"
    core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    from scope_recall.core.delete_storage import purged_group_key

    # A long message's group key is replaced once, where it had been hashed once for each of its parts.
    with sqlite3.connect(core.storage.path) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM source_events WHERE source_group_key=?", (purged_group_key("TEST-long"),)
            ).fetchone()[0]
            == 2
        )
    # After it: the same words spaced and cased otherwise, and a message with one of the deleted parts.
    assert store("TEST-short", "test 要删的一句短话。") == "cancelled"
    assert store("TEST-long", "!" + long_text[1:]) == "cancelled"
    # Another message of another length under either key is a key collision, stored under a key of its own.
    assert store("TEST-long", "TEST 同一个键上的一句短话。") == "conflict"
    assert store("TEST-short", "".join(f"TEST 第{i}句另一段长话。" for i in range(6000))) == "conflict"
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=10
    )
    assert [receipt.durability for receipt in receipts] == ["persisted", "persisted"] and _inbox_rows(core) == 0


def test_a_message_purged_before_rc13_still_refuses_what_comes_under_its_key(worker_app):
    """A purge before rc13 kept no digests of the deleted words, and hashed a long message's group key once for each of
    its parts.  Nothing kept tells a near copy under such a key from another message, so whatever comes under it is
    refused, as every release before rc13 refused it: a copy spaced otherwise is not stored as another message (review
    of rc13)."""
    from scope_recall.core.delete_storage import purged_group_key

    core, ctx, clock = worker_app
    long_text = "".join(f"TEST 第{i}句旧时删掉的长话。" for i in range(6000))
    stored = [
        capture(core, ctx, "TEST 旧时删掉的一句话。", key="TEST-old-short"),
        capture(core, ctx, long_text, key="TEST-old-long"),
    ]
    authorize(core, ctx, *stored)
    operation = core.forget(ctx, request(*stored), remaining_seconds=5)
    core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("""UPDATE source_events SET extra_json='{"evidence_refs":[]}'
                        WHERE source_event_key='removed-'||event_id""")
        conn.execute(
            "UPDATE source_events SET source_group_key=? WHERE source_group_key=?",
            (purged_group_key(purged_group_key("TEST-old-long")), purged_group_key("TEST-old-long")),
        )
        conn.commit()
    for key, content in (
        ("TEST-old-short", "TEST 旧时删掉的 一句话。"),
        ("TEST-old-short", "TEST 旧键上的另一句。"),
        ("TEST-old-long", "TEST 旧键上的一句短话。"),
    ):
        assert (
            capture_inbox.durable_record_event(
                core.storage,
                clock,
                ctx,
                source_event(source_event_key=key, content=content),
                scope_id="TEST-scope",
                host_scope=None,
            ).disposition
            == "cancelled"
        )


def test_a_deleted_message_with_no_text_is_not_taken_for_an_old_purge(worker_app):
    """A message with attachments alone has no text.  Deleted and not purged yet, it had no text and no kept forms, as
    a row purged before rc13, and every other message under its key was refused until the purge ran (review of rc13).
    Only a purged row counts as an old purge."""
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 带附件的一条。", key="TEST-attachments")
    authorize(core, ctx, source)
    core.forget(ctx, request(source), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE source_events SET content='' WHERE source_event_key='TEST-attachments'")
        conn.commit()
    assert (
        capture_inbox.durable_record_event(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key="TEST-attachments", content="TEST 同一个键上的另一件事。"),
            scope_id="TEST-scope",
            host_scope=None,
        ).disposition
        == "conflict"
    )


def test_a_purge_run_again_keeps_the_forms_the_first_one_kept_and_reads_a_version_once(worker_app, monkeypatch):
    """A restore purges its file again: written over from the empty text, the digests a first purge kept were lost,
    and a copy spaced otherwise came back.  And a long message's words were joined and read again for each part, so
    that its purge grew with the square of its parts under the writer lease (review of rc13)."""
    core, ctx, clock = worker_app
    short, long_text = "TEST 要删的 一句话。", "".join(f"TEST 第{i}句要删的长话。" for i in range(6000))
    stored = [capture(core, ctx, short, key="TEST-short"), capture(core, ctx, long_text, key="TEST-long")]
    authorize(core, ctx, *stored)
    operation = core.forget(ctx, request(*stored), remaining_seconds=5)
    counted = []
    original = inbox_rules.deleted_forms
    # The purge reads the rule through delete_storage, which takes it at import.
    monkeypatch.setattr(delete_storage, "deleted_forms", lambda text: counted.append(len(text)) or original(text))
    core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    assert len(counted) == 3, "the short message, the long one and the command: once each"
    with sqlite3.connect(core.storage.path) as conn:
        kept = sorted(conn.execute("SELECT extra_json FROM source_events WHERE source_event_key='removed-'||event_id"))
        layers = json.loads(
            conn.execute(
                "SELECT layers_json FROM deletion_operations WHERE operation_id=?", (operation["operation_id"],)
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE deletion_operations SET layers_json=? WHERE operation_id=?",
            (json.dumps({**layers, "sqlite_active": "pending"}), operation["operation_id"]),
        )
        conn.commit()
    core.operations.purge_sqlite(ctx, operation["operation_id"], remaining_seconds=10)
    with sqlite3.connect(core.storage.path) as conn:
        assert (
            sorted(conn.execute("SELECT extra_json FROM source_events WHERE source_event_key='removed-'||event_id"))
            == kept
        )
    assert (
        capture_inbox.durable_record_event(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key="TEST-short", content="TEST 要删的一句话 。"),
            scope_id="TEST-scope",
            host_scope=None,
        ).disposition
        == "cancelled"
    )


def test_retry_failures_returns_a_capture_an_earlier_release_refused_to_the_replay(worker_app):
    """A capture an earlier release refused as ACCESS_DENIED, most often one under a deleted message's key, stayed in
    the inbox for good with doctor's ``capture_ingress_blocked`` up, and nothing but a hand removed it (review of
    rc13).  ``retry-failures --apply`` returns it to the replay, which now stores another message under a key of its
    own and cancels a copy."""
    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        source_event(source_event_key="TEST-refused", content="TEST 早先被拒的一句。"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE capture_inbox SET last_error_code='ACCESS_DENIED' WHERE token=?", (token,))
        conn.commit()

    def replay():
        return capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)

    assert replay() == ()
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=True)["inbox_refused"] == 1
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [("ACCESS_DENIED",)]
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["inbox_refused"] == 1
    assert [receipt.durability for receipt in replay()] == ["persisted"] and _inbox_rows(core) == 0


def test_a_delete_keeps_a_row_that_took_the_deleted_message_s_key_for_other_words(worker_app):
    """A row being given a new key is another message that took a stored one's key: matched by that key, deleting the
    first message cancelled the second, whose words nobody deleted (review of rc10).  Its words still count."""
    core, ctx, clock = worker_app
    first = capture(core, ctx, "TEST 第一条，要删掉。", key="TEST-taken")
    rekey = f"DEFERRED|{inbox_rules.__version__}|2026-09-06T13:00:00Z|1|rekey|IDENTITY_UNBOUND:TEST-host"
    gave_up = f"GAVE_UP|{inbox_rules.__version__}|25|rekey|IDENTITY_UNBOUND:TEST-host"
    for session, content, code in (
        ("TEST-session-2", "TEST 第二条，另一句话。", rekey),
        ("TEST-session-3", "TEST 第一条，要删掉。", rekey),
        ("TEST-session-4", "TEST 第三条，放弃过的。", gave_up),
        # A collision waiting for its new key can wait a while too.
        ("TEST-session-5", "TEST 第四条，等新键的。", "VERSION_CONFLICT"),
    ):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(ctx, session_id=session),
            source_event(source_event_key="TEST-taken", content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
    authorize(core, ctx, first)
    core.forget(ctx, request(first), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        left = sorted(
            json.loads(row[0])["events"][0]["content"] for row in conn.execute("SELECT payload_json FROM capture_inbox")
        )
    assert left == sorted(["TEST 第二条，另一句话。", "TEST 第三条，放弃过的。", "TEST 第四条，等新键的。"])
    assert inbox_rules.deferred_path(gave_up) == "rekey" and inbox_rules.deferred_path("GAVE_UP|0.0.1|x") == "replay"


def test_a_delete_cancels_a_collision_that_holds_the_deleted_message(worker_app):
    """A key collision is kept across a delete unless it holds the deleted message; compared by digest alone, the
    same words with a line break more, or a long message with a character before it, were kept and stored after the
    delete, and no test guarded the collisions that had to go (review of rc10)."""
    core, ctx, clock = worker_app
    capture(core, ctx, "TEST 要删掉的这一句，里面有私事，早先的说法。", key="TEST-collided-short")
    short = capture(core, ctx, "TEST 要删掉的这一句，里面有私事。", key="TEST-collided-short", revision=2)
    long_text = "TEST 很长的要删掉的消息。" * 6000
    long_ = capture(core, ctx, long_text, key="TEST-collided-long")
    put_off = f"DEFERRED|{inbox_rules.__version__}|2026-09-06T13:00:00Z|1|replay|TEST"
    rows = (
        ("TEST-collided-short", "TEST 要删掉的这一句，里面有私事。", "VERSION_CONFLICT"),
        ("TEST-collided-short", "TEST 要删掉的这一句，里面有私事。\n", "VERSION_CONFLICT"),
        ("TEST-collided-short", "TEST 要删掉的这一句，\n里面有私事。", "VERSION_CONFLICT"),
        ("TEST-collided-short", "TEST要删掉的这一句，里面有私事。", "VERSION_CONFLICT"),
        # The version before, a line break inside: only its text says so, not its digest.
        ("TEST-collided-short", "TEST 要删掉的这一句，\n里面有私事，早先的说法。", "VERSION_CONFLICT"),
        # Put off under a key of its own: matched by its words, not by any key.
        ("TEST-elsewhere", "Y" + long_text, put_off),
        ("TEST-collided-short", "TEST 另外一句话。", "VERSION_CONFLICT"),
        ("TEST-collided-short", "TEST 引用了：要删掉的这一句", "VERSION_CONFLICT"),
        ("TEST-collided-long", long_text, "VERSION_CONFLICT"),
        ("TEST-collided-long", "X" + long_text, "VERSION_CONFLICT"),
        ("TEST-collided-long", "TEST 另一条很长的消息。" * 6000, "VERSION_CONFLICT"),
    )
    for index, (key, content, code) in enumerate(rows):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(ctx, session_id=f"TEST-session-c{index}"),
            source_event(source_event_key=key, content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
    authorize(core, ctx, short, long_)
    core.forget(ctx, request(short, long_), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        kept = {
            json.loads(payload)["events"][0]["content"][:20]
            for (payload,) in conn.execute("SELECT payload_json FROM capture_inbox")
        }
    assert kept == {"TEST 另外一句话。", "TEST 引用了：要删掉的这一句", ("TEST 另一条很长的消息。" * 2)[:20]}


def test_a_delete_of_a_short_message_keeps_waiting_rows_that_merely_contain_it(worker_app):
    """Deleting "好" or "ok" cancelled every waiting row that held those characters among other words, and comparing
    letters and digits alone made "C++" cancel "C#" and a 22-character sentence with its punctuation too short to be
    its own (reviews of rc10).  A distinct text (24 characters, whitespace aside) cancels any row holding it whole; a
    shorter one a row that is it with at most a tenth more, whitespace aside, or with four or more letters and digits
    the same ones with at most a tenth more, punctuation and case aside."""
    core, ctx, clock = worker_app
    deleted = {
        text: capture(core, ctx, text, key=f"TEST-deleted-{index}")
        for index, text in enumerate(
            (
                "好",
                "ok",
                "我要辞职了",
                "甲乙丙丁戊己庚辛壬癸",
                "一二三四五六七八九十一二三四五六七八九十一二三四",
                "子丑寅卯辰巳午未申酉戌亥子丑寅卯辰巳午未申酉戌",
                "他上个月在北京朝阳医院查出了肺结节，还在复查中。",
                "C++",
                "+1",
                "？？？",
                "\U0001f44d",
                "房间号是3721",
                "Let me know if you have any questions.",
            )
        )
    }
    rows = (
        ("TEST 这个方案挺好的，就这么办。", True),
        ("TEST I will look at the book tomorrow.", True),
        ("TEST 你好，请帮我看一下日志。", True),
        ("好", False),
        ("ok\n", False),
        ("我要辞职了。", False),
        ("我要辞职了！", False),
        ("[图片] 我要辞职了", True),
        # A tenth: one more character in eleven is the same text, two in twelve are not.
        ("甲乙丙丁戊己庚辛壬癸子", False),
        ("甲乙丙丁戊己庚辛壬癸子丑", True),
        # Twenty-four characters are distinct: held whole among other words, the text goes; twenty-three are not.
        ("前面的话很多很多很多。一二三四五六七八九十一二三四五六七八九十一二三四后面的话也很多很多。", False),
        ("前面的话很多很多很多。子丑寅卯辰巳午未申酉戌亥子丑寅卯辰巳午未申酉戌后面的话也很多很多。", True),
        ("好的，他上个月在北京朝阳医院查出了肺结节，还在复查中。我知道了，会保密的。", False),
        ("C#", True),
        ("-1", True),
        ("？？？\n", False),
        ("\U0001f44d\n", False),
        ("房间号是3712。", True),
        ("房间号是3721。", False),
        ("OK. Let me know if you have any questions! Also: the deploy key rotates on Friday.", True),
    )
    for index, (content, _kept) in enumerate(rows):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(ctx, session_id=f"TEST-session-s{index}"),
            source_event(source_event_key=f"TEST-waiting-{index}", content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code='VERSION_CONFLICT' WHERE token=?", (token,))
            conn.commit()
    authorize(core, ctx, *deleted.values())
    core.forget(ctx, request(*deleted.values()), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        kept = sorted(
            json.loads(payload)["events"][0]["content"]
            for (payload,) in conn.execute("SELECT payload_json FROM capture_inbox")
        )
    assert kept == sorted(content for content, keep in rows if keep)
    assert not inbox_rules.holds(
        json.dumps({"events": [{"content": ""}]}), frozenset(), frozenset(), frozenset({("", "")}), rekeyed=True
    ), "an empty text holds nothing"


def test_a_delete_cancels_what_holds_a_deleted_segment_or_cannot_be_read(worker_app):
    """A row whose first or second segment is a deleted one, the rest another's, holds no deleted text whole: only its
    segment says so.  A row that cannot be read is cancelled, as every row a delete cannot look into (review of rc10)."""
    core, ctx, clock = worker_app
    long_text = "TEST 很长的要删掉的消息。" * 6000
    long_ = capture(core, ctx, long_text, key="TEST-segment-deleted")
    rows = (
        (
            ctx,
            source_event(
                source_event_key="TEST-segment-row", content=long_text[:65536] + "TEST 完全不同的后续内容。" * 3000
            ),
        ),
        (ctx, source_event(source_event_key="TEST-second-segment-row", content="Z" * 65536 + long_text[65536:])),
        (ctx, source_event(source_event_key="TEST-unreadable", content="TEST 读不懂的一行。")),
        (ctx, source_event(source_event_key="TEST-unrelated", content="TEST 另外一件事。")),
    )
    for index, (context, event) in enumerate(rows):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(context, session_id=f"TEST-session-g{index}"),
            event,
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code='VERSION_CONFLICT' WHERE token=?", (token,))
            if event["source_event_key"] == "TEST-unreadable":
                # The table holds valid JSON only: a row of another shape is what cannot be read.
                conn.execute(
                    """UPDATE capture_inbox SET payload_json='{"events": [{"segment": 5}]}' WHERE token=?""", (token,)
                )
            conn.commit()
    authorize(core, ctx, long_)
    core.forget(ctx, request(long_), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        kept = [
            json.loads(payload)["events"][0]["content"]
            for (payload,) in conn.execute("SELECT payload_json FROM capture_inbox")
        ]
    assert kept == ["TEST 另外一件事。"]


def test_a_delete_puts_each_deleted_text_together_once_and_only_when_it_needs_it(worker_app, monkeypatch):
    """Each version of a deleted message was put together for every one of its segments, and for an empty inbox too:
    a delete of four long messages took seconds under the writer lease (review of rc10)."""
    core, ctx, clock = worker_app
    calls = []
    real = inbox_rules.without_whitespace
    monkeypatch.setattr(inbox_rules, "without_whitespace", lambda text: calls.append(1) or real(text))
    first = capture(core, ctx, "TEST 很长的消息。" * 9000, key="TEST-once-1")
    authorize(core, ctx, first)
    core.forget(ctx, request(first), remaining_seconds=5)
    assert calls == [], "nothing waits: nothing is put together"
    second = capture(core, ctx, "TEST 另一条很长的消息。" * 9000, key="TEST-once-2")
    for index in range(2):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(ctx, session_id=f"TEST-session-o{index}"),
            source_event(source_event_key="TEST-once-2", content=f"TEST 第{index}条。"),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code='VERSION_CONFLICT' WHERE token=?", (token,))
            conn.commit()
    authorize(core, ctx, second)
    core.forget(ctx, request(second), remaining_seconds=5)
    # The deleted message and the request that named it (deleted with it), once each, and each waiting row's own text.
    assert len(calls) == 4


def test_a_passing_failure_keeps_a_row_on_its_path(worker_app, monkeypatch):
    """A store error that passes (the store busy) was written over a collision's code, which took it off the rekey
    path and out of what a delete keeps (review of rc10)."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-70", content="TEST first TEST-turn-70")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(
        core.storage,
        clock,
        other,
        dict(first, content="TEST second TEST-turn-70"),
        scope_id="TEST-scope",
        host_scope=None,
    )

    for passing in ("STORAGE_UNAVAILABLE", "DEADLINE_EXCEEDED"):

        def busy(*args, passing=passing, **kwargs):
            raise ContractError(passing, "connection_cleanup")

        monkeypatch.setattr(capture_inbox, "record_event", busy)
        receipts = capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
        )
        assert [receipt.error_code for receipt in receipts] == [passing]
        assert capture_inbox.INGRESS_PENDING_GAP in receipts[0].gaps
        with sqlite3.connect(core.storage.path) as conn:
            assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [("VERSION_CONFLICT",)]


def test_a_commit_left_for_the_next_pass_says_so_and_a_refused_one_does_not(worker_app, monkeypatch):
    """Only a busy store was tested: a commit whose time ran out said nothing either, and a refusal must not say it
    is pending (review of rc10)."""
    from scope_recall.core.capture import CaptureReceipt

    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        source_event(source_event_key="TEST-commit-outcome", content="TEST 提交的结果。"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    late = CaptureReceipt("unavailable", (), "unknown", "unknown", "unknown", error_code="DEADLINE_EXCEEDED")
    monkeypatch.setattr(capture_inbox, "record_event", lambda *args, **kwargs: late)
    [receipt] = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert receipt.error_code == "DEADLINE_EXCEEDED" and capture_inbox.INGRESS_PENDING_GAP in receipt.gaps

    def refused(*args, **kwargs):
        raise ContractError("INPUT_INVALID", "TEST-content")

    monkeypatch.setattr(capture_inbox, "record_event", refused)
    [receipt] = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert receipt.error_code == "INPUT_INVALID" and capture_inbox.INGRESS_PENDING_GAP not in receipt.gaps
    with sqlite3.connect(core.storage.path) as conn:
        assert (
            conn.execute("SELECT last_error_code FROM capture_inbox WHERE token=?", (token,)).fetchone()[0]
            == "INPUT_INVALID"
        )


def test_a_suppress_leaves_the_inbox_alone(worker_app):
    """A suppress cancelled the partition's inbox as a delete does, and then every waiting row that held its words:
    a quote with news in it, the message's next version, a row it could not read (reviews of rc10).  The contract
    keeps them: what comes of the same message, or restates a suppressed claim, is suppressed as it is stored."""
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 别再主动提这件私事，说过很多次了。", key="TEST-suppressed")
    for index, (key, content, code) in enumerate(
        (
            ("TEST-unrelated-waiting", "TEST 无关的等待中的一句。", None),
            ("TEST-suppressed", "TEST 别再主动提这件私事，说过很多次了。", "VERSION_CONFLICT"),
            ("TEST-quoting", "TEST 别再主动提这件私事，说过很多次了。另外，明天的会改到下午三点。", None),
            ("TEST-suppressed", "TEST 同一条消息的下一版。", None),
        )
    ):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(ctx, session_id=f"TEST-session-p{index}"),
            source_event(source_event_key=key, content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
    authorize(core, ctx, source, mode="suppress")
    core.forget(ctx, request(source, mode="suppress"), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 4


def test_a_collided_copy_of_a_suppressed_message_is_stored_suppressed(worker_app):
    """A message sent again under a key another message held is stored under a new key, a group of its own that the
    first message's suppression did not reach: a suppressed message came back to automatic recall that way (rc13).
    A copy of a suppressed message is stored suppressed; other words under the same key are not."""
    core, ctx, clock = worker_app
    text, plain = "TEST 别再主动提这件私事，说过很多次了。", "TEST 一句没有被压下的话。"
    capture(core, ctx, plain, key="TEST-plain")
    source = capture(core, ctx, text, key="TEST-suppressed")
    authorize(core, ctx, source, mode="suppress")
    core.forget(ctx, request(source, mode="suppress"), remaining_seconds=5)
    for index, content in enumerate((text, "TEST 同一个键上的另一句话。", plain)):
        receipt = capture_inbox.durable_record_event(
            core.storage,
            clock,
            replace(ctx, session_id=f"TEST-session-c{index}"),
            source_event(source_event_key="TEST-suppressed", content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        assert receipt.disposition == "conflict"
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=5
    )
    assert [receipt.durability for receipt in receipts] == ["persisted"] * 3
    with sqlite3.connect(core.storage.path) as conn:
        stored = dict(
            conn.execute(
                "SELECT content,suppressed FROM source_events WHERE source_event_key LIKE ?",
                (f"TEST-suppressed{inbox_rules.REKEY_MARKER}%",),
            ).fetchall()
        )
    # A copy of words that were never suppressed is not suppressed either.
    assert stored == {text: 1, "TEST 同一个键上的另一句话。": 0, plain: 0}
    # The same words said again under a key of their own are a new message, stored as before (3.4.0rc10).
    capture(core, replace(ctx, session_id="TEST-session-later"), text, key="TEST-said-again")
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute(
            "SELECT suppressed FROM source_events WHERE source_event_key='TEST-said-again'"
        ).fetchall() == [(0,)]


def test_a_collided_copy_is_compared_as_a_delete_compares_and_its_group_suppressed_whole(worker_app):
    """Compared by each part's exact digest, a collided copy spaced otherwise came back to automatic recall, and so
    did the last part of a long copy that differed in one character, while its first part was suppressed (review of
    rc13).  The words of the message whose key it took are compared as a delete compares them, and a source group is
    suppressed whole.  A copy of another role, or in another project, is not the suppressed message's."""
    core, ctx, clock = worker_app
    # Two parts, whose words do not repeat: each part is compared on its own.
    short, long_text = "TEST 这件私事别再主动提了，谢谢。", "".join(f"TEST 第{i}句私事。" for i in range(8000))

    def suppress(text, key):
        source = capture(core, ctx, text, key=key)
        authorize(core, ctx, source, mode="suppress")
        core.forget(ctx, request(source, mode="suppress"), remaining_seconds=5)

    suppress(short, "TEST-short")
    suppress(long_text, "TEST-long")
    for index, (key, content, changes) in enumerate(
        (
            ("TEST-short", "TEST  这件私事别再主动提了，\n谢谢。", {}),
            # Its last part differs by a word, and then its first by a letter: the other part is the copy either way,
            # and the one that differs is no copy by itself (a changed full stop alone would be one).
            ("TEST-long", long_text[:-2] + "情。", {}),
            ("TEST-long", "!" + long_text[1:], {}),
            ("TEST-short", short, {"actor_origin": "tool_observation"}),
            ("TEST-short", short, {"project_id": "TEST-elsewhere"}),
        )
    ):
        context = replace(ctx, session_id=f"TEST-session-copy{index}", **changes)
        role = "tool" if context.actor_origin == "tool_observation" else "user"
        receipt = capture_inbox.durable_record_event(
            core.storage,
            clock,
            context,
            source_event(source_event_key=key, content=content, role=role, origin=context.actor_origin),
            scope_id="TEST-scope",
            host_scope=None,
        )
        assert receipt.disposition == "conflict", key
    for context in (ctx, replace(ctx, project_id="TEST-elsewhere")):
        capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, context, authorize=lambda _: ctx.allowed_scope_ids, remaining_seconds=10
        )
    assert _inbox_rows(core) == 0
    with sqlite3.connect(core.storage.path) as conn:
        rows = conn.execute(f"""SELECT source_group_key,role,project_id,suppressed FROM source_events
            WHERE source_group_key LIKE '%{inbox_rules.REKEY_MARKER}%'""").fetchall()
    stored = sorted(
        (group.split(inbox_rules.REKEY_MARKER)[0], role, project, suppressed)
        for group, role, project, suppressed in rows
    )
    here = ctx.project_id
    assert stored == sorted(
        [
            *[("TEST-long", "user", here, 1)] * 4,
            ("TEST-short", "tool", here, 0),
            ("TEST-short", "user", here, 1),
            ("TEST-short", "user", "TEST-elsewhere", 0),
        ]
    ), stored


def test_retry_failures_returns_only_the_rows_its_replay_takes(tmp_path):
    """Returned by what a config could see, rows of other partitions went back to a replay that never takes them;
    and a row put off is not retry-failures' to return (review of rc10)."""
    from scope_recall.contracts import TrustedContext

    core, cfg, _path = fixture(tmp_path)
    own = cfg.context()
    given_up = f"GAVE_UP|{inbox_rules.__version__}|25|replay|IDENTITY_UNBOUND:TEST-host"
    put_off = f"DEFERRED|{inbox_rules.__version__}|2026-09-06T13:00:00Z|3|replay|TypeError"
    partitions = {
        "own": (own, given_up),
        "put off": (own, put_off),
        "unset": (TrustedContext(cfg.binding, "TEST-session", frozenset({"TEST-a"}), "human_direct"), given_up),
        "other project": (replace(own, project_id="TEST-other"), given_up),
        # One an earlier release refused, returned as well, from this partition alone (rc13).
        "refused": (own, "ACCESS_DENIED"),
        "refused elsewhere": (replace(own, project_id="TEST-other"), "ACCESS_DENIED"),
    }
    tokens = {}
    for label, (context, code) in partitions.items():
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            core.clock,
            context,
            source_event(source_event_key=f"TEST-{label}", content=f"TEST {label} 的一句。"),
            scope_id="TEST-a",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
        tokens[label] = token
    preview = core.operations.retry_failed_work(own, limit=64, dry_run=True)
    assert (preview["inbox_given_up"], preview["inbox_by_kind"]) == (1, {"IDENTITY_UNBOUND:TEST-host": 1})
    assert preview["inbox_refused"] == 1
    core.operations.retry_failed_work(own, limit=64, dry_run=False)
    with sqlite3.connect(core.storage.path) as conn:
        codes = dict(conn.execute("SELECT token,last_error_code FROM capture_inbox").fetchall())
    assert {label: codes[token] for label, token in tokens.items()} == {
        "own": None,
        "put off": put_off,
        "unset": given_up,
        "other project": given_up,
        "refused": None,
        "refused elsewhere": "ACCESS_DENIED",
    }


def test_a_stored_context_this_release_cannot_read_is_named(worker_app):
    """A field a newer release wrote into a stored context left the row's code ending in a bare TypeError, which named
    nothing to look at (review of rc10)."""
    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        source_event(source_event_key="TEST-newer-context", content="TEST 新版本写的上下文。"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    with sqlite3.connect(core.storage.path) as conn:
        body = json.loads(conn.execute("SELECT payload_json FROM capture_inbox WHERE token=?", (token,)).fetchone()[0])
        body["context"]["TEST_newer_field"] = "x"
        conn.execute(
            "UPDATE capture_inbox SET payload_json=? WHERE token=?", (json.dumps(body, ensure_ascii=False), token)
        )
        conn.commit()
    receipts = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert [receipt.error_code for receipt in receipts] == ["DEFERRED"]
    with sqlite3.connect(core.storage.path) as conn:
        [(code,)] = conn.execute("SELECT last_error_code FROM capture_inbox").fetchall()
    assert code.endswith("|1|replay|INPUT_INVALID:ingress_context")


def test_a_stored_context_is_named_whatever_part_fails_and_its_own_contract_errors_kept(worker_app):
    """Only a newer field was tested: a context with its scopes missing, or not an object at all, and a contract error
    raised inside, which keeps its own code, went untested (review of rc10)."""
    core, ctx, clock = worker_app

    def put_off_as(change):
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("DELETE FROM capture_inbox")
            conn.commit()
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key="TEST-context-part", content="TEST 上下文的一部分。"),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            body = json.loads(
                conn.execute("SELECT payload_json FROM capture_inbox WHERE token=?", (token,)).fetchone()[0]
            )
            change(body)
            conn.execute(
                "UPDATE capture_inbox SET payload_json=? WHERE token=?", (json.dumps(body, ensure_ascii=False), token)
            )
            conn.commit()
        capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
        with sqlite3.connect(core.storage.path) as conn:
            return conn.execute("SELECT last_error_code FROM capture_inbox").fetchone()[0].rsplit("|", 1)[-1]

    assert put_off_as(lambda body: body["context"].pop("allowed_scope_ids")) == "INPUT_INVALID:ingress_context"
    assert put_off_as(lambda body: body.update(context=["TEST"])) == "INPUT_INVALID:ingress_context"
    assert (
        put_off_as(lambda body: body["context"].update(actor_origin="TEST-not-an-origin"))
        == "IDENTITY_UNBOUND:actor_origin"
    )


def test_a_busy_store_at_the_commit_or_at_a_deferral_is_said_as_pending(tmp_path, monkeypatch):
    """A pass whose commit met a busy writer said nothing, and a deferral the store did not take was reported as put
    off or given up, and reported again at the next pass (review of rc10)."""
    from scope_recall.core.capture import CaptureReceipt
    from scope_recall.core.writer_lease import TruthWriterBusyError
    from scope_recall.runtime.instance import RuntimeInstance
    from scope_recall.runtime.worker_entry import _ingress_report

    core, cfg, _path = fixture(tmp_path)
    context = cfg.context()
    token, _prepared = capture_inbox.enqueue(
        core.storage,
        core.clock,
        context,
        source_event(source_event_key="TEST-busy-commit", content="TEST 提交时忙。"),
        scope_id="TEST-a",
        host_scope=None,
    )
    unavailable = CaptureReceipt("unavailable", (), "unknown", "unknown", "unknown", error_code="STORAGE_UNAVAILABLE")
    monkeypatch.setattr(capture_inbox, "record_event", lambda *args, **kwargs: unavailable)
    fake = SimpleNamespace(
        config=cfg, _ingress_authorizer=lambda _: context.allowed_scope_ids, core=core, ingress_receipts=()
    )
    assert RuntimeInstance._replay_ingress(fake, 8.0) == (capture_inbox.INGRESS_PENDING_GAP,)
    last = f"DEFERRED|{inbox_rules.__version__}|2026-01-01T00:00:00Z|{inbox_rules.DEFER_ATTEMPTS}|replay|TEST"
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (last, token))
        conn.commit()

    def refused(*args, **kwargs):
        raise ContractError("IDENTITY_UNBOUND", "TEST-host")

    def busy(*args, **kwargs):
        raise TruthWriterBusyError()

    monkeypatch.setattr(capture_inbox, "_revalidated", refused)
    monkeypatch.setattr(core.storage, "write", busy)
    assert RuntimeInstance._replay_ingress(fake, 8.0) == (capture_inbox.INGRESS_PENDING_GAP,)
    counts, _gaps = _ingress_report(fake.ingress_receipts)
    assert (counts["ingress_given_up"], counts["ingress_deferred"]) == (0, 0), "the store never took the give-up"
    monkeypatch.undo()
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [(last,)]


def test_a_pass_says_pending_when_its_replay_stopped_or_raised_a_store_error(tmp_path, monkeypatch):
    """Neither a pass reading the gap from its replay's receipts nor one catching a SQLite error from the first replay
    was tested (review of rc10)."""
    import sqlite3 as sqlite

    from scope_recall.core.writer_lease import TruthWriterBusyError
    from scope_recall.runtime.instance import RuntimeInstance

    core, cfg, _path = fixture(tmp_path)
    context = cfg.context()
    capture_inbox.enqueue(
        core.storage,
        core.clock,
        context,
        source_event(source_event_key="TEST-stopped", content="TEST 停在这里。"),
        scope_id="TEST-a",
        host_scope=None,
    )

    def busy(*args, **kwargs):
        raise TruthWriterBusyError()

    monkeypatch.setattr(capture_inbox, "_revalidated", busy)
    fake = SimpleNamespace(
        config=cfg, _ingress_authorizer=lambda _: context.allowed_scope_ids, core=core, ingress_receipts=()
    )
    assert RuntimeInstance._replay_ingress(fake, 8.0) == (capture_inbox.INGRESS_PENDING_GAP,)
    assert [receipt.error_code for receipt in fake.ingress_receipts] == ["STORAGE_UNAVAILABLE"]
    ran = []

    def locked(*args, **kwargs):
        raise sqlite.OperationalError("database is locked")

    monkeypatch.setattr(capture_inbox, "replay_inbox", locked)
    monkeypatch.setattr(capture_inbox, "resolve_conflicted_ingress", lambda *args, **kwargs: ran.append(1) or ())
    assert RuntimeInstance._replay_ingress(fake, 8.0) == (capture_inbox.INGRESS_PENDING_GAP,) and ran == [1]


def test_a_given_up_row_of_the_rekey_path_goes_back_to_it(worker_app):
    """Returned as never tried, a row the rekey path gave up met the old collision on the plain replay, and one put
    off there was matched by a delete through the key it had taken (review of rc10)."""
    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(
        core.storage,
        clock,
        ctx,
        source_event(source_event_key="TEST-rekey-given-up", content="TEST 换键时放弃的。"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            "UPDATE capture_inbox SET last_error_code=? WHERE token=?",
            (f"GAVE_UP|{inbox_rules.__version__}|25|rekey|IDENTITY_UNBOUND:TEST-host", token),
        )
        conn.commit()
    assert core.operations.retry_failed_work(ctx, limit=64, dry_run=False)["inbox_given_up"] == 1
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [("VERSION_CONFLICT",)]


def test_a_busy_store_halfway_through_a_replay_keeps_what_it_did(worker_app, monkeypatch):
    """Met on the second row, a busy store raised out of the replay: the first row's deferral went unsaid, and the
    replay of collided keys after it did not run (review of rc10)."""
    from scope_recall.core.writer_lease import TruthWriterBusyError

    core, ctx, clock = worker_app
    tokens = []
    for index in range(3):
        clock.advance(seconds=1, iso=f"2026-09-06T12:00:0{index}Z")
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            ctx,
            source_event(source_event_key=f"TEST-busy-{index}", content=f"TEST 第{index}条。"),
            scope_id="TEST-scope",
            host_scope=None,
        )
        tokens.append(token)

    def revalidated(storage, context, row, authorize, deadline, *, rekey):
        if row["token"] == tokens[0]:
            raise ContractError("IDENTITY_UNBOUND", "TEST-host")
        raise TruthWriterBusyError()

    monkeypatch.setattr(capture_inbox, "_revalidated", revalidated)
    receipts = capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
    assert [receipt.error_code for receipt in receipts] == ["DEFERRED", "STORAGE_UNAVAILABLE"]
    assert capture_inbox.INGRESS_PENDING_GAP in receipts[1].gaps


def test_a_pass_runs_both_replays_whatever_the_first_met(tmp_path, monkeypatch):
    """A pass whose first replay raised skipped the replay of collided keys (review of rc10)."""
    from scope_recall.core.capture import CaptureReceipt
    from scope_recall.runtime.instance import RuntimeInstance

    core, cfg, _path = fixture(tmp_path)
    context = cfg.context()
    stored = CaptureReceipt("inserted", (), "persisted", "indexed", "pending")

    def raising(*args, **kwargs):
        raise RuntimeError("TEST busy")

    monkeypatch.setattr(capture_inbox, "replay_inbox", raising)
    monkeypatch.setattr(capture_inbox, "resolve_conflicted_ingress", lambda *args, **kwargs: (stored,))
    fake = SimpleNamespace(
        config=cfg, _ingress_authorizer=lambda _: context.allowed_scope_ids, core=core, ingress_receipts=()
    )
    assert RuntimeInstance._replay_ingress(fake, 8.0) == (capture_inbox.INGRESS_PENDING_GAP,)
    assert fake.ingress_receipts == (stored,)


def test_a_pass_says_and_keeps_what_its_replays_put_off_and_gave_up(tmp_path):
    """Only the counting was tested: a pass that left the counts or the gaps out of its report, or a status file that
    dropped them, passed; and doctor did not show them (review of rc10)."""
    import time

    from scope_recall.core.capture import CaptureReceipt
    from scope_recall.maintenance import doctor
    from scope_recall.runtime.worker_entry import _drain_once, persist_worker_status

    core, cfg, _path = fixture(tmp_path)
    receipt = SimpleNamespace(
        items=(),
        idle=False,
        retried=0,
        processed=0,
        completed=0,
        failed=0,
        skipped=0,
        stale=0,
        obsolete=0,
        deferred=0,
        recovered=0,
        unavailable_work_types=(),
    )
    ingress = tuple(
        CaptureReceipt("queued", (), "queued", "pending", "pending", error_code=code)
        for code in ("GAVE_UP", "DEFERRED", "DEFERRED")
    )
    queue_state = SimpleNamespace(terminal_failed_work=0, failed_work=0, pending_work=0, oldest_pending_at=None)
    instance = SimpleNamespace(
        auxiliary=SimpleNamespace(ledger_path=None, capability_gaps=()),
        drain=lambda **kwargs: receipt,
        background_gaps=(),
        provider_holds={},
        ingress_receipts=ingress,
        status=lambda **kwargs: queue_state,
    )
    gaps = {"capture_gap:durable_ingress_deferred", "capture_gap:durable_ingress_given_up"}
    payload = _drain_once(cfg, instance, time.monotonic() + 10)
    assert (payload["ingress_deferred"], payload["ingress_given_up"]) == (2, 1) and gaps <= set(
        payload["capability_gaps"]
    )
    persist_worker_status(cfg, payload, started_at="2026-09-12T00:00:00Z", exit_code=0)
    status = json.loads((cfg.binding.data_directory / "runtime-worker-status.json").read_text(encoding="utf-8"))
    assert (status["ingress_deferred"], status["ingress_given_up"]) == (2, 1) and gaps <= set(status["capability_gaps"])
    assert {"ingress_deferred", "ingress_given_up"} <= doctor._WORKER_STATUS_KEYS


def test_a_pass_counts_the_inbox_rows_it_put_off_and_gave_up():
    """A pass said how many rows it put off but not how many it gave up, and kept neither in its status file."""
    from scope_recall.core.capture import CaptureReceipt
    from scope_recall.runtime.worker_entry import _ingress_report

    def receipt(disposition, durability, code=None):
        return CaptureReceipt(disposition, (), durability, "pending", "pending", error_code=code)

    counts, gaps = _ingress_report(
        (
            receipt("queued", "queued", "DEFERRED"),
            receipt("queued", "queued", "GAVE_UP"),
            receipt("queued", "queued", "GAVE_UP"),
            receipt("inserted", "persisted"),
            receipt("cancelled", "not_persisted", "ACCESS_DENIED"),
        )
    )
    assert counts == {"ingress_deferred": 1, "ingress_given_up": 2, "ingress_replayed": 1, "ingress_cancelled": 1}
    assert gaps == ["capture_gap:durable_ingress_deferred", "capture_gap:durable_ingress_given_up"]
    assert _ingress_report(()) == (
        {"ingress_deferred": 0, "ingress_given_up": 0, "ingress_replayed": 0, "ingress_cancelled": 0},
        [],
    )


def test_a_named_message_that_was_deleted_still_counts_as_said(worker_app):
    """Once a delete is purged, a named message's rows no longer carry its key, and a Stop's read of the session
    record stored the words again under a key of the record's: deleted words came back (review of 3.4.0rc10)."""
    core, ctx, _clock = worker_app
    source = capture(core, ctx, "TEST 要删掉的一句话。", key="TEST-named-deleted")
    authorize(core, ctx, source)
    deleted = core.forget(ctx, request(source), remaining_seconds=5)
    core.operations.purge_sqlite(ctx, deleted["operation_id"], remaining_seconds=10)
    said = core.said_in_session(
        ctx, "TEST-scope", [("user", "TEST 要删掉的一句话。", source.event["occurred_at"], "TEST-named-deleted")]
    )
    assert tuple(said) == (True,)


def test_a_named_message_stored_in_another_session_is_held_across_sessions(worker_app):
    """Hermes carries a steer into the session a compression continues.  Asked across sessions, the message stored in
    the session before is held, and so are one deleted there and one still waiting in the inbox there; asked for this
    session alone, the stored and the waiting ones are not."""
    core, ctx, clock = worker_app
    kept = capture(core, ctx, "TEST 带进下一个会话的话。", key="TEST-named-carried")
    gone = capture(core, ctx, "TEST 删掉后又带进来的话。", key="TEST-named-carried-deleted")
    authorize(core, ctx, gone)
    deleted = core.forget(ctx, request(gone), remaining_seconds=5)
    core.operations.purge_sqlite(ctx, deleted["operation_id"], remaining_seconds=10)
    queued = source_event(source_event_key="TEST-named-carried-waiting", content="TEST 还在收件箱里的话。")
    token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, queued, scope_id="TEST-scope", host_scope=None)
    assert token is not None
    later = replace(ctx, session_id="TEST-session-2")
    items = [
        ("user", "TEST 带进下一个会话的话。", kept.event["occurred_at"], "TEST-named-carried"),
        ("user", "TEST 删掉后又带进来的话。", gone.event["occurred_at"], "TEST-named-carried-deleted"),
        (queued["role"], queued["content"], queued["occurred_at"], "TEST-named-carried-waiting"),
    ]
    assert tuple(core.said_in_session(later, "TEST-scope", items)) == (False, True, False)
    assert tuple(core.said_in_session(later, "TEST-scope", items, across_sessions=True)) == (True, True, True)


def test_a_long_key_that_was_taken_is_cut_to_fit_its_new_key(worker_app):
    """A host key of 490 characters or more went past the 512-character limit once the marker and the fingerprint
    were added, and was refused on every pass."""
    core, ctx, clock = worker_app
    key = "TEST-" + "k" * 500
    first = source_event(source_event_key=key, content="TEST first long-keyed message")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(
        core.storage,
        clock,
        other,
        dict(first, content="TEST second long-keyed message"),
        scope_id="TEST-scope",
        host_scope=None,
    )
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    )
    assert [receipt.durability for receipt in receipts] == ["persisted"]
    with sqlite3.connect(core.storage.path) as conn:
        rekeyed = conn.execute(
            "SELECT source_event_key FROM source_events WHERE content='TEST second long-keyed message'"
        ).fetchone()[0]
    assert len(rekeyed) == 512 and "#rekey:" in rekeyed and rekeyed.startswith("TEST-kkk")


def test_a_long_message_waiting_in_the_inbox_counts_as_said(worker_app):
    """A message over 65,536 characters waits in the inbox as segments under keys of their own: looked up by the
    host's key it was not found, and a session-record read stored it a second time."""
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-long-waiting", content="TEST 很长的等待中的消息。" * 6000)
    token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    assert token is not None
    said = core.said_in_session(
        ctx, "TEST-scope", [(event["role"], event["content"], event["occurred_at"], "TEST-long-waiting")]
    )
    assert tuple(said) == (True,)


def test_a_capture_that_conflicts_again_under_its_new_key_stays_final(worker_app, monkeypatch):
    """Written back as a bare conflict, such a row was given the same new key and refused on every pass, and it
    woke the worker every 30 s (review of 3.4.0rc10)."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-43", content="TEST first message")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(
        core.storage, clock, other, dict(first, content="TEST second message"), scope_id="TEST-scope", host_scope=None
    )
    # A new key that collides as well.
    monkeypatch.setattr(capture_inbox, "_rekeyed_event", lambda event, capture="": dict(event))
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    )
    assert [receipt.disposition for receipt in receipts] == ["conflict"]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [("VERSION_CONFLICT:rekeyed",)]
    assert (
        capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
        )
        == ()
    )


def test_the_parts_of_a_delete_s_comparison_each_hold(worker_app):
    """Each part of the comparison was left to the others in the tests, so a floor, a tenth or the case could change
    unnoticed (review of rc10): here each decides a row alone."""
    core, ctx, clock = worker_app
    deleted = {
        text: capture(core, ctx, text, key=f"TEST-part-{index}")
        for index, text in enumerate(
            ("1.2", "1.2.3", "a1b2", "Deploy Tonight Please", "| --- | --- |", "|---|---|---|---|---|")
        )
    }
    rows = (
        ("12", True),  # two letters and digits: a near copy only whitespace aside
        ("123", True),  # three: likewise
        ("a1b2.", False),  # four: the same letters and digits, punctuation aside
        ("deploy tonight please!", False),  # case aside
        ("deploy tonight pleasex", False),  # the letters' tenth: one more in twenty
        ("|---|---|", False),  # symbols only: whitespace aside, removed and not folded
        ("|---|---|---|---|---|-", False),  # the whitespace tenth: one more in twenty-two
        ("|---|---|---|---|---|------", True),
    )  # six more is over a tenth
    for index, (content, _kept) in enumerate(rows):
        token, _prepared = capture_inbox.enqueue(
            core.storage,
            clock,
            replace(ctx, session_id=f"TEST-session-p{index}"),
            source_event(source_event_key=f"TEST-part-row-{index}", content=content),
            scope_id="TEST-scope",
            host_scope=None,
        )
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code='VERSION_CONFLICT' WHERE token=?", (token,))
            conn.commit()
    authorize(core, ctx, *deleted.values())
    core.forget(ctx, request(*deleted.values()), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        kept = sorted(
            json.loads(payload)["events"][0]["content"]
            for (payload,) in conn.execute("SELECT payload_json FROM capture_inbox")
        )
    assert kept == sorted(content for content, keep in rows if keep)
