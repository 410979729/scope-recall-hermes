"""Release-critical offline failure/restart checks, without model/network calls."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core import capture_inbox
from scope_recall.core.episodes import source_watermark
from scope_recall.core.worker import _decode_consolidation_result
from scope_recall.runtime.resume_entry import resume_once, control_path
from scope_recall.maintenance.autostart import plan
from test_v11_worker import worker_app as worker_app, app as app, capture, draft, consolidation_payload, FakeConsolidation
from test_v11_deletion import authorize, request
from test_sprint_consolidation_chunks import long_source, row
from test_finite_supervisor import fixture, queue, NOW
from v11_support import downgrade_store
from v11_support import source_event


def test_capture_commit_failure_survives_fresh_process_and_dedupes(worker_app, monkeypatch, tmp_path):
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-durable", content="TEST-project 配色 蓝色。")
    real = capture_inbox.record_event
    def broken(*args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE")
    monkeypatch.setattr(capture_inbox, "record_event", broken)
    receipt = capture_inbox.durable_record_event(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    assert receipt.durability == "queued"
    with core.storage.read(ctx) as tx:
        assert tx.status().sources == 0
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1
    monkeypatch.setattr(capture_inbox, "record_event", real)
    # A new OS process gets only binding/context metadata; no in-memory event.
    metadata = dict(agent_id=ctx.binding.agent_id,installation_id=ctx.binding.installation_id,
        data_directory=str(ctx.binding.data_directory),scope_ids=sorted(ctx.binding.scope_ids),
        project_id=ctx.project_id,branch_id=ctx.branch_id,session_id=ctx.session_id)
    path = tmp_path/"replay.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    code = '''import json,sys
from pathlib import Path
from scope_recall.contracts import InstanceBinding,TrustedContext
from scope_recall.core import CoreConfig,MemoryCore
from scope_recall.core.capture_inbox import replay_inbox
m=json.loads(Path(sys.argv[1]).read_text()); b=InstanceBinding(m['agent_id'],m['installation_id'],Path(m['data_directory']),frozenset(m['scope_ids']),True)
c=TrustedContext(b,m['session_id'],b.scope_ids,'host_generated',project_id=m['project_id'],branch_id=m['branch_id'])
core=MemoryCore(CoreConfig(b));r=replay_inbox(core.storage,core.clock,c,authorize=lambda _: b.scope_ids,remaining_seconds=5)
assert len(r)==1 and r[0].durability=='persisted'
print('fresh-process-replay-ok')'''
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "fresh-process-replay-ok" in result.stdout
    duplicate = capture_inbox.durable_record_event(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    assert duplicate.disposition == "duplicate"
    with core.storage.read(ctx) as tx:
        assert tx.status().sources == 1
        assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_revocation_and_deletion_cancel_pending_ingress(worker_app):
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-revoked",content="TEST-project 配色 蓝色。")
    capture_inbox.enqueue(core.storage,clock,ctx,event,scope_id="TEST-scope",host_scope=None)
    receipt = capture_inbox.replay_inbox(core.storage,clock,ctx,authorize=lambda _: frozenset())[0]
    assert receipt.disposition == "cancelled" and core.status(ctx).sources == 0
    source = capture(core,ctx,"TEST 已有资料。")
    capture_inbox.enqueue(core.storage,clock,ctx,event,scope_id="TEST-scope",host_scope=None)
    authorize(core,ctx,source)
    core.forget(ctx,request(source),remaining_seconds=5)
    assert capture_inbox.replay_inbox(core.storage,clock,ctx,authorize=lambda _: ctx.allowed_scope_ids) == ()


def test_ingress_rejects_secrets_conflicts_and_other_partitions(worker_app):
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-collision",content="TEST original")
    capture_inbox.enqueue(core.storage,clock,ctx,event,scope_id="TEST-scope",host_scope=None)
    with pytest.raises(ContractError,match="VERSION_CONFLICT"):
        capture_inbox.enqueue(core.storage,clock,ctx,dict(event,content="TEST changed"),scope_id="TEST-scope",host_scope=None)
    assert capture_inbox.replay_inbox(core.storage,clock,replace(ctx,project_id="TEST-foreign"),authorize=lambda _:ctx.allowed_scope_ids) == ()
    token, prepared = capture_inbox.enqueue(core.storage,clock,ctx,dict(event,source_event_key="TEST-secret",content="api_key=sk-"+"abcd"*12),scope_id="TEST-scope",host_scope=None)
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
    source = long_source(core,ctx,goal+"\n"+"这是一段归档资料；"*1800+"\n"+progress)
    def build(sources, episode_ref=None):
        page = sources[0]
        refs = [f"{page.ref}@{page.revision}"]
        seed = getattr(page,"consolidation_seed",())
        resumes = []
        if goal in page.event["content"] or seed:
            resumes = [dict(episode_ref=episode_ref,goal=dict(text=goal,evidence_refs=refs),decisions=[],
                verified_progress=[dict(text=progress,evidence_refs=refs)] if progress in page.event["content"] else [],
                open_items=[],blockers=[],next_step=None,next_step_basis="unknown",artifact_refs=[],
                source_watermark=source_watermark(refs),evidence_refs=refs)]
        return consolidation_payload(page,resume_proposals=resumes)
    for _ in range(40):
        receipt = core.drain_worker(ctx,consolidation=FakeConsolidation(build),max_items=1,remaining_seconds=5)
        assert receipt.failed == receipt.retried == 0, receipt
        with sqlite3.connect(core.storage.path) as db:
            resumes = db.execute("SELECT resume_json FROM episode_versions WHERE resume_json IS NOT NULL").fetchall()
        if row(core,source)[0] == "done":
            break
        assert resumes == []
        core = MemoryCore(CoreConfig(ctx.binding),clock=clock)
    assert row(core,source)[1] == len(source.event["content"])
    assert resumes and progress in resumes[-1][0]
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("SELECT disposition FROM consolidation_outcomes").fetchone()[0] == "complete"
        assert db.execute("SELECT count(*) FROM consolidation_fragments").fetchone()[0] == 0


def test_decoder_repairs_unique_serialized_quote_but_never_fuzzy_support(worker_app):
    core,ctx,_ = worker_app
    raw = json.dumps({"note":r"TEST-project 路径 C:\work\report.txt"},ensure_ascii=False)
    source = capture(core,ctx,raw)
    quote = r"TEST-project 路径 C:\work\report.txt"
    p=draft(source,value=r"C:\work\report.txt",predicate="路径",evidence_spans=[dict(source_ref=source.ref,source_revision=1,quote=quote)],procedure={})
    value=_decode_consolidation_result(json.dumps(consolidation_payload(source,claims=[p])),(source,))
    assert "procedure" not in value["claim_proposals"][0]
    assert value["claim_proposals"][0]["evidence_spans"][0]["quote"] in raw
    p["evidence_spans"][0]["quote"]="TEST invented quote"
    value=_decode_consolidation_result(json.dumps(consolidation_payload(source,claims=[p])),(source,))
    assert value["claim_proposals"][0]["evidence_spans"][0]["quote"] == "TEST invented quote"


def test_external_wake_due_future_pause_and_task_plan(tmp_path):
    core,cfg,oldpath=fixture(tmp_path)
    path=cfg.binding.data_directory/"runtime.json"
    path.write_bytes(oldpath.read_bytes())
    prepared=plan(path,Path(sys.executable),user_id="TEST-user")
    assert "LogonTrigger" in prepared["xml"] and "PT5M" in prepared["xml"] and "LeastPrivilege" in prepared["xml"]
    control={k:v for k,v in prepared.items() if k!="xml"}
    control_path(cfg).write_text(json.dumps(control))
    launched=[]
    def launch(*args,**kwargs):
        launched.append((args,kwargs))
        return SimpleNamespace(pid=99)
    assert not resume_once(path,launcher=launch,now=NOW)["launched"]
    queue(core,cfg,due=NOW+timedelta(minutes=1))
    assert not resume_once(path,launcher=launch,now=NOW)["launched"]
    assert resume_once(path,launcher=launch,now=NOW+timedelta(minutes=2))["launched"]
    control["enabled"]=False
    control_path(cfg).write_text(json.dumps(control))
    assert resume_once(path,launcher=launch,now=NOW+timedelta(minutes=3))["status"] == "paused"
    assert len(launched)==1


def test_external_wake_still_launches_after_ten_thousand_items_in_a_day(tmp_path):
    core,cfg,oldpath=fixture(tmp_path)
    path=cfg.binding.data_directory/"runtime.json"
    path.write_bytes(oldpath.read_bytes())
    prepared=plan(path,Path(sys.executable),user_id="TEST-user")
    control_path(cfg).write_text(json.dumps({k:v for k,v in prepared.items() if k!="xml"}))
    (cfg.binding.data_directory/"runtime-worker-day.json").write_text(json.dumps(
        dict(installation_id=cfg.binding.installation_id,day=NOW.isoformat()[:10],used=10_001)))
    queue(core,cfg,due=NOW)
    launched=[]
    def launch(*args,**kwargs):
        launched.append((args,kwargs))
        return SimpleNamespace(pid=99)
    assert resume_once(path,launcher=launch,now=NOW)["launched"]
    assert len(launched)==1


def test_restore_cancels_stale_inbox_and_fences_replay(worker_app,tmp_path):
    from test_v11_deletion import InstallationMaintenance,export_deletion_ledger,begin_restore,ledger_digest,replay_deletion_ledger,sqlite_backup
    core,ctx,clock=worker_app
    event=source_event(source_event_key='TEST-before-restore',content='TEST queued before restore')
    capture_inbox.enqueue(core.storage,clock,ctx,event,scope_id='TEST-scope',host_scope=None)
    backup=tmp_path/'before.sqlite3'
    sqlite_backup(core.storage.path,backup)
    authority=InstallationMaintenance(ctx)
    ledger=export_deletion_ledger(core.storage,authority)
    begin_restore(core.storage,authority,expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    replay_deletion_ledger(core.storage,authority,ledger)
    assert capture_inbox.replay_inbox(core.storage,clock,ctx,authorize=lambda _:ctx.allowed_scope_ids)==()
    assert core.status(ctx).sources==0


def test_1106_failure_upgrade_preserves_history_and_requeues_exactly_once(worker_app):
    core,ctx,_=worker_app
    source=capture(core,ctx,'TEST upgrade failure evidence')
    with sqlite3.connect(core.storage.path) as db:
        db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='DERIVATION_INVALID' WHERE work_type='consolidate'")
    downgrade_store(core.storage.path, 1106)
    core.initialize()
    assert row(core,source)==('pending',0,0)
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute('SELECT stage,error_code FROM work_error_details').fetchone()==('upgrade_1106','DERIVATION_INVALID')
        db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='DERIVATION_INVALID' WHERE work_type='consolidate'")
    core.initialize()
    assert row(core,source)==('failed',0,3)


def test_backup_and_rollback_cli_preview_is_readonly_and_protects_inbox(worker_app,tmp_path,capsys):
    from scope_recall.maintenance.cli import main
    core,ctx,clock=worker_app
    snapshot=tmp_path/'verified.sqlite3'
    assert main(['backup','--database',str(core.storage.path),'--output',str(snapshot)])==0
    manifest=json.loads(snapshot.with_suffix('.sqlite3.json').read_text())
    assert manifest['quick_check']=='ok'
    capture_inbox.enqueue(core.storage,clock,ctx,source_event(content='TEST pending ingress'),scope_id='TEST-scope',host_scope=None)
    before=core.storage.path.read_bytes()
    assert main(['rollback','--current-db',str(core.storage.path),'--snapshot',str(snapshot)])==0
    assert core.storage.path.read_bytes()==before and not (ctx.binding.data_directory/'restore-required.json').exists()
    assert main(['rollback','--current-db',str(core.storage.path),'--snapshot',str(snapshot),'--apply'])==0
    assert (ctx.binding.data_directory/'restore-required.json').is_file()
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute('SELECT count(*) FROM capture_inbox').fetchone()[0]==1


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
    assert capture_inbox.durable_record_event(
        core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None
    ).durability == "persisted"

    # A different session reuses the same key for different content: the inbox
    # token differs, so this enqueues, and the collision only surfaces at commit.
    other = replace(ctx, session_id="TEST-session-2")
    collided = capture_inbox.durable_record_event(
        core.storage, clock, other, dict(first, content="TEST second message"),
        scope_id="TEST-scope", host_scope=None,
    )
    assert collided.disposition == "conflict" and collided.error_code == "VERSION_CONFLICT"

    with sqlite3.connect(core.storage.path) as conn:
        blocked = conn.execute(
            "SELECT count(*) FROM capture_inbox WHERE last_error_code='VERSION_CONFLICT'"
        ).fetchone()[0]
    assert blocked == 1, "the payload is held, not discarded"

    # Replay cannot help: the identity collides by construction, so the row is
    # outside its filter entirely.
    assert capture_inbox.replay_inbox(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids
    ) == ()

    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    )
    assert len(receipts) == 1 and receipts[0].durability == "persisted"

    with sqlite3.connect(core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
        stored = conn.execute(
            "SELECT source_event_key,content FROM source_events ORDER BY rowid"
        ).fetchall()
    contents = [row["content"] for row in stored]
    assert "TEST first message" in contents and "TEST second message" in contents, \
        "both messages survive; neither hides the other"
    keys = [row["source_event_key"] for row in stored]
    assert "TEST-turn-42" in keys, "the original keeps its identity"
    rekeyed = [key for key in keys if key.startswith("TEST-turn-42#rekey:")]
    assert len(rekeyed) == 1, "the collision stays visible in the stored key"

    # Idempotent: nothing is left to repair, and a second pass adds nothing.
    assert capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5
    ) == ()


def test_a_long_message_whose_key_was_taken_is_stored_under_a_new_group(worker_app):
    """Given a new key, each segment of a long message kept the first message's group, met it again and was never
    stored (review of 3.4.0rc10).  The segments now move into one new group."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-77", content="TEST 第一条很长的消息。" * 6000)
    assert capture_inbox.durable_record_event(
        core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None).durability == "persisted"
    other = replace(ctx, session_id="TEST-session-2")
    collided = capture_inbox.durable_record_event(
        core.storage, clock, other, dict(first, content="TEST 第二条很长的消息。" * 6000),
        scope_id="TEST-scope", host_scope=None)
    assert collided.disposition == "conflict"
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)
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
        capture_inbox.durable_record_event(core.storage, clock, other, dict(first, content=f"TEST second {key}"),
                                           scope_id="TEST-scope", host_scope=None)
    original = capture_inbox.prepare_capture

    def prepare(event, context):
        if "TEST-turn-50" in event["source_event_key"]:
            raise ContractError("INPUT_INVALID", "TEST-envelope")
        return original(event, context)

    monkeypatch.setattr(capture_inbox, "prepare_capture", prepare)
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)
    assert sorted(receipt.disposition for receipt in receipts) == ["inserted", "queued"]
    with sqlite3.connect(core.storage.path) as conn:
        [(code,)] = conn.execute("SELECT last_error_code FROM capture_inbox").fetchall()
        assert conn.execute("SELECT count(*) FROM source_events WHERE content='TEST second TEST-turn-51'").fetchone()[0] == 1
    assert code.startswith(f"DEFERRED|{capture_inbox.__version__}|")
    assert code.endswith("|1|rekey|INPUT_INVALID:TEST-envelope"), "the field that failed is named"
    # Not taken again before its minute is up; still waiting as far as a record read is concerned.
    assert capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5) == ()
    assert capture_inbox.waiting(code)
    # Once it reads, the collision is stored under a new key when its minute is up.
    monkeypatch.setattr(capture_inbox, "prepare_capture", original)
    clock.advance(seconds=61, iso="2026-09-06T12:01:01Z")
    assert [r.durability for r in capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)] == ["persisted"]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM source_events WHERE content='TEST second TEST-turn-50'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_a_row_put_off_again_waits_longer_and_is_given_up_where_it_shows():
    """A row put off was tried again every hour for ever (review of 3.4.0rc10), and after a reinstall an hour was
    long to wait.  It is tried after a minute, doubling to an hour, and given up after a day's worth of tries."""
    from datetime import datetime, timezone

    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    code, waits = None, []
    for _attempt in range(capture_inbox.DEFER_ATTEMPTS):
        code = capture_inbox._deferral(code, ContractError("IDENTITY_UNBOUND", "TEST|host"), now, path="replay")
        waits.append((capture_inbox.deferred_until(code, now) - now).total_seconds())
    assert waits[:4] == [60, 120, 240, 480] and waits[-1] == 3600
    assert code.endswith("|24|replay|IDENTITY_UNBOUND:TEST/host"), "the field is named, its bar replaced"
    given_up = capture_inbox._deferral(code, RuntimeError("TEST"), now, path="replay")
    assert given_up == f"GAVE_UP|{capture_inbox.__version__}|25|replay|RuntimeError", "its failures, the first too"
    assert capture_inbox.given_up(given_up) and capture_inbox.put_off(given_up)
    assert not capture_inbox.replayable(given_up, now) and not capture_inbox.waiting(given_up)
    # Given up for every release, and the tries another release made count: a new release reset them, so a row
    # that failed under each was tried for ever (review of 3.4.0rc10).
    assert not capture_inbox.replayable("GAVE_UP|0.0.1|24|replay|RuntimeError", now)
    other = f"DEFERRED|0.0.1|2026-09-28T11:00:00Z|{capture_inbox.DEFER_ATTEMPTS}|replay|TypeError"
    assert capture_inbox._deferral(other, RuntimeError("TEST"), now, path="replay").startswith("GAVE_UP|")
    assert not capture_inbox.replayable(other.replace("11:00:00Z", "12:30:00Z"), now), "another release's wait holds"
    # A time without its zone, further off than any wait, or a code of another shape is due now rather than a crash
    # or a wait for ever.
    version = capture_inbox.__version__
    assert capture_inbox.replayable(f"DEFERRED|{version}|2026-09-28T12:30:00|1|replay|TEST", now)
    assert capture_inbox.replayable(f"DEFERRED|{version}|2026-12-01T00:00:00Z|1|replay|TEST", now)
    assert capture_inbox.replayable(f"DEFERRED|{version}|2026-09-28T12:30:00Z|1|TEST", now)
    assert not capture_inbox.replayable(f"DEFERRED|{version}|2026-09-28T12:30:00Z|1|replay|TEST", now)
    assert capture_inbox.deferred_path(f"DEFERRED|{version}|2026-09-28T12:30:00Z|1|rekey|TEST") == "rekey"


def test_a_delete_keeps_a_put_off_row_unless_it_holds_the_deleted_words(worker_app):
    """A delete cancels its partition's pending captures, so that a delayed one cannot undo it.  A row put off waits
    for hours, and cancelling it lost words nothing had forgotten (review of 3.4.0rc10)."""
    core, ctx, clock = worker_app
    later = f"DEFERRED|{capture_inbox.__version__}|2026-09-06T13:00:00Z|1|replay|RuntimeError"
    given_up = f"GAVE_UP|{capture_inbox.__version__}|24|rekey|RuntimeError"
    long_kept, long_same = "TEST 暂缓的长消息。" * 8000, "TEST 同组的另一版长消息。" * 6000
    rows = (("TEST-put-off-keep", "TEST 暂缓的另一句话。", later), ("TEST-put-off-same", "TEST 要删掉的话。", later),
            ("TEST-plain-waiting", "TEST 普通等待的一句。", None),
            # A long message waits as segments: one of another group is kept, one of the deleted group is not.
            ("TEST-long-kept", long_kept, given_up), ("TEST-stored-long", long_same, later))
    for key, text, code in rows:
        token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, source_event(source_event_key=key, content=text),
                                                 scope_id="TEST-scope", host_scope=None)
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
    source = capture(core, ctx, "TEST 要删掉的话。", key="TEST-stored-same")
    long_source_ = capture(core, ctx, "TEST 已存的长消息。" * 8000, key="TEST-stored-long")
    authorize(core, ctx, source, long_source_)
    core.forget(ctx, request(source, long_source_), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        events = [json.loads(row[0])["events"][0] for row in conn.execute("SELECT payload_json FROM capture_inbox")]
    left = sorted(event["segment"]["group_key"] if "segment" in event else event["source_event_key"] for event in events)
    assert left == ["TEST-long-kept", "TEST-put-off-keep"]


def test_a_host_check_that_raises_puts_off_that_row_only(worker_app):
    """Hermes' identity errors are RuntimeErrors: one raised for a single row stopped the whole page on every pass."""
    core, ctx, clock = worker_app
    for key in ("TEST-host-a", "TEST-host-b"):
        capture_inbox.enqueue(core.storage, clock, ctx, source_event(source_event_key=key, content=f"TEST {key}"),
                              scope_id="TEST-scope", host_scope={"TEST": key})

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
    capture_inbox.durable_record_event(core.storage, clock, other, dict(first, content="TEST second TEST-turn-60"),
                                       scope_id="TEST-scope", host_scope=None)
    original = capture_inbox.prepare_capture

    def prepare(event, context):
        if capture_inbox.REKEY_MARKER in event["source_event_key"]:
            raise ContractError("IDENTITY_UNBOUND", "TEST-host")
        return original(event, context)

    def code():
        with sqlite3.connect(core.storage.path) as conn:
            return conn.execute("SELECT last_error_code FROM capture_inbox").fetchone()[0]

    def replay():
        return capture_inbox.replay_inbox(core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids)

    def resolve():
        return capture_inbox.resolve_conflicted_ingress(
            core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)

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
        assert conn.execute("SELECT count(*) FROM source_events WHERE content='TEST second TEST-turn-60'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0


def test_retry_failures_returns_a_given_up_capture_to_the_replay(worker_app):
    """A row given up stayed in the inbox with nothing to take it once its cause was fixed (review of 3.4.0rc10).
    ``retry-failures`` counts such rows, and with ``--apply`` returns them to the replay, their tries anew."""
    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(
        core.storage, clock, ctx, source_event(source_event_key="TEST-given-up", content="TEST 放弃过的一句。"),
        scope_id="TEST-scope", host_scope=None)
    given_up = f"GAVE_UP|{capture_inbox.__version__}|24|replay|IDENTITY_UNBOUND:TEST-host"
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (given_up, token))
        conn.commit()

    def replay():
        return capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)

    assert replay() == ()
    preview = core.retry_failed_work(ctx, limit=64, dry_run=True)
    assert (preview["inbox_given_up"], preview["applied"]) == (1, False)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [(given_up,)]
    assert preview["inbox_by_kind"] == {"IDENTITY_UNBOUND:TEST-host": 1}
    assert core.retry_failed_work(ctx, limit=64, dry_run=False)["inbox_given_up"] == 1
    assert [receipt.durability for receipt in replay()] == ["persisted"]
    assert core.retry_failed_work(ctx, limit=64, dry_run=True)["inbox_given_up"] == 0


def test_a_delete_keeps_a_row_that_took_the_deleted_message_s_key_for_other_words(worker_app):
    """A row being given a new key is another message that took a stored one's key: matched by that key, deleting the
    first message cancelled the second, whose words nobody deleted (review of rc10).  Its words still count."""
    core, ctx, clock = worker_app
    first = capture(core, ctx, "TEST 第一条，要删掉。", key="TEST-taken")
    rekey = f"DEFERRED|{capture_inbox.__version__}|2026-09-06T13:00:00Z|1|rekey|IDENTITY_UNBOUND:TEST-host"
    gave_up = f"GAVE_UP|{capture_inbox.__version__}|25|rekey|IDENTITY_UNBOUND:TEST-host"
    for session, content, code in (("TEST-session-2", "TEST 第二条，另一句话。", rekey),
                                   ("TEST-session-3", "TEST 第一条，要删掉。", rekey),
                                   ("TEST-session-4", "TEST 第三条，放弃过的。", gave_up),
                                   # A collision waiting for its new key wakes nothing, and can wait as long.
                                   ("TEST-session-5", "TEST 第四条，等新键的。", "VERSION_CONFLICT")):
        token, _prepared = capture_inbox.enqueue(core.storage, clock, replace(ctx, session_id=session),
                                                 source_event(source_event_key="TEST-taken", content=content),
                                                 scope_id="TEST-scope", host_scope=None)
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
    authorize(core, ctx, first)
    core.forget(ctx, request(first), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        left = sorted(json.loads(row[0])["events"][0]["content"]
                      for row in conn.execute("SELECT payload_json FROM capture_inbox"))
    assert left == sorted(["TEST 第二条，另一句话。", "TEST 第三条，放弃过的。", "TEST 第四条，等新键的。"])
    assert capture_inbox.deferred_path(gave_up) == "rekey" and capture_inbox.deferred_path("GAVE_UP|0.0.1|x") == "replay"


def test_a_delete_cancels_a_collision_that_holds_the_deleted_message(worker_app):
    """A key collision is kept across a delete unless it holds the deleted message; compared by digest alone, the
    same words with a line break more, or a long message with a character before it, were kept and stored after the
    delete, and no test guarded the collisions that had to go (review of rc10)."""
    core, ctx, clock = worker_app
    short = capture(core, ctx, "TEST 要删掉的这一句。", key="TEST-collided-short")
    long_text = "TEST 很长的要删掉的消息。" * 6000
    long_ = capture(core, ctx, long_text, key="TEST-collided-long")
    rows = (("TEST-collided-short", "TEST 要删掉的这一句。", "exact"),
            ("TEST-collided-short", "TEST 要删掉的这一句。\n", "a line break more"),
            ("TEST-collided-short", "TEST 另外一句话。", "other words"),
            ("TEST-collided-short", "TEST 引用了：要删掉的这一句", "a part quoted"),
            ("TEST-collided-long", long_text, "long exact"),
            ("TEST-collided-long", "X" + long_text, "long, a character before"),
            ("TEST-collided-long", "TEST 另一条很长的消息。" * 6000, "long, other words"))
    for index, (key, content, _label) in enumerate(rows):
        token, _prepared = capture_inbox.enqueue(core.storage, clock, replace(ctx, session_id=f"TEST-session-c{index}"),
                                                 source_event(source_event_key=key, content=content),
                                                 scope_id="TEST-scope", host_scope=None)
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code='VERSION_CONFLICT' WHERE token=?", (token,))
            conn.commit()
    authorize(core, ctx, short, long_)
    core.forget(ctx, request(short, long_), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        kept = {json.loads(payload)["events"][0]["content"][:20] for (payload,) in
                conn.execute("SELECT payload_json FROM capture_inbox")}
    assert kept == {"TEST 另外一句话。", "TEST 引用了：要删掉的这一句", ("TEST 另一条很长的消息。" * 2)[:20]}


def test_a_passing_failure_keeps_a_row_on_its_path(worker_app, monkeypatch):
    """A store error that passes (the store busy) was written over a collision's code, which took it off the rekey
    path and out of what a delete keeps (review of rc10)."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-70", content="TEST first TEST-turn-70")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(core.storage, clock, other, dict(first, content="TEST second TEST-turn-70"),
                                       scope_id="TEST-scope", host_scope=None)

    def busy(*args, **kwargs):
        raise ContractError("STORAGE_UNAVAILABLE", "connection_cleanup")

    monkeypatch.setattr(capture_inbox, "record_event", busy)
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)
    assert [receipt.error_code for receipt in receipts] == ["STORAGE_UNAVAILABLE"]
    assert capture_inbox.INGRESS_PENDING_GAP in receipts[0].gaps
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [("VERSION_CONFLICT",)]


def test_a_commit_left_for_the_next_pass_says_so_and_a_refused_one_does_not(worker_app, monkeypatch):
    """Only a busy store was tested: a commit whose time ran out said nothing either, and a refusal must not say it
    is pending (review of rc10)."""
    from scope_recall.core.capture import CaptureReceipt

    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, source_event(
        source_event_key="TEST-commit-outcome", content="TEST 提交的结果。"), scope_id="TEST-scope", host_scope=None)
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
        assert conn.execute("SELECT last_error_code FROM capture_inbox WHERE token=?", (token,)).fetchone()[0] == \
            "INPUT_INVALID"


def test_a_suppress_leaves_the_inbox_alone(worker_app):
    """The partition's inbox was cancelled for a suppress as well as a delete, though a suppressed group already
    hides what comes of it later (review of rc10)."""
    core, ctx, clock = worker_app
    source = capture(core, ctx, "TEST 别再主动提这件事。", key="TEST-suppressed")
    capture_inbox.enqueue(core.storage, clock, ctx, source_event(source_event_key="TEST-unrelated-waiting",
                                                                 content="TEST 无关的等待中的一句。"),
                          scope_id="TEST-scope", host_scope=None)
    authorize(core, ctx, source, mode="suppress")
    core.forget(ctx, request(source, mode="suppress"), remaining_seconds=5)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1


def test_retry_failures_returns_only_the_rows_its_replay_takes(tmp_path):
    """Returned by what a config could see, rows of other partitions went back to a replay that never takes them;
    and a row put off is not retry-failures' to return (review of rc10)."""
    from scope_recall.contracts import TrustedContext

    core, cfg, _path = fixture(tmp_path)
    own = cfg.context()
    given_up = f"GAVE_UP|{capture_inbox.__version__}|25|replay|IDENTITY_UNBOUND:TEST-host"
    put_off = f"DEFERRED|{capture_inbox.__version__}|2026-09-06T13:00:00Z|3|replay|TypeError"
    partitions = {"own": (own, given_up), "put off": (own, put_off),
                  "unset": (TrustedContext(cfg.binding, "TEST-session", frozenset({"TEST-a"}), "human_direct"), given_up),
                  "other project": (replace(own, project_id="TEST-other"), given_up)}
    tokens = {}
    for label, (context, code) in partitions.items():
        token, _prepared = capture_inbox.enqueue(core.storage, core.clock, context, source_event(
            source_event_key=f"TEST-{label}", content=f"TEST {label} 的一句。"), scope_id="TEST-a", host_scope=None)
        with sqlite3.connect(core.storage.path) as conn:
            conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?", (code, token))
            conn.commit()
        tokens[label] = token
    preview = core.retry_failed_work(own, limit=64, dry_run=True)
    assert (preview["inbox_given_up"], preview["inbox_by_kind"]) == (1, {"IDENTITY_UNBOUND:TEST-host": 1})
    core.retry_failed_work(own, limit=64, dry_run=False)
    with sqlite3.connect(core.storage.path) as conn:
        codes = dict(conn.execute("SELECT token,last_error_code FROM capture_inbox").fetchall())
    assert {label: codes[token] for label, token in tokens.items()} == {
        "own": None, "put off": put_off, "unset": given_up, "other project": given_up}


def test_a_stored_context_this_release_cannot_read_is_named(worker_app):
    """A field a newer release wrote into a stored context left the row's code ending in a bare TypeError, which named
    nothing to look at (review of rc10)."""
    core, ctx, clock = worker_app
    token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, source_event(
        source_event_key="TEST-newer-context", content="TEST 新版本写的上下文。"), scope_id="TEST-scope", host_scope=None)
    with sqlite3.connect(core.storage.path) as conn:
        body = json.loads(conn.execute("SELECT payload_json FROM capture_inbox WHERE token=?", (token,)).fetchone()[0])
        body["context"]["TEST_newer_field"] = "x"
        conn.execute("UPDATE capture_inbox SET payload_json=? WHERE token=?", (json.dumps(body, ensure_ascii=False), token))
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
        token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, source_event(
            source_event_key="TEST-context-part", content="TEST 上下文的一部分。"), scope_id="TEST-scope", host_scope=None)
        with sqlite3.connect(core.storage.path) as conn:
            body = json.loads(conn.execute("SELECT payload_json FROM capture_inbox WHERE token=?", (token,)).fetchone()[0])
            change(body)
            conn.execute("UPDATE capture_inbox SET payload_json=? WHERE token=?",
                         (json.dumps(body, ensure_ascii=False), token))
            conn.commit()
        capture_inbox.replay_inbox(core.storage, clock, ctx, authorize=lambda _: ctx.allowed_scope_ids)
        with sqlite3.connect(core.storage.path) as conn:
            return conn.execute("SELECT last_error_code FROM capture_inbox").fetchone()[0].rsplit("|", 1)[-1]

    assert put_off_as(lambda body: body["context"].pop("allowed_scope_ids")) == "INPUT_INVALID:ingress_context"
    assert put_off_as(lambda body: body.update(context=["TEST"])) == "INPUT_INVALID:ingress_context"
    assert put_off_as(lambda body: body["context"].update(actor_origin="TEST-not-an-origin")) == \
        "IDENTITY_UNBOUND:actor_origin"


def test_a_busy_store_at_the_commit_or_at_a_deferral_is_said_as_pending(tmp_path, monkeypatch):
    """A pass whose commit met a busy writer said nothing, and a deferral the store did not take was reported as put
    off or given up, and reported again at the next pass (review of rc10)."""
    from scope_recall.core.capture import CaptureReceipt
    from scope_recall.core.writer_lease import TruthWriterBusyError
    from scope_recall.runtime.instance import RuntimeInstance
    from scope_recall.runtime.worker_entry import _ingress_report

    core, cfg, _path = fixture(tmp_path)
    context = cfg.context()
    token, _prepared = capture_inbox.enqueue(core.storage, core.clock, context, source_event(
        source_event_key="TEST-busy-commit", content="TEST 提交时忙。"), scope_id="TEST-a", host_scope=None)
    unavailable = CaptureReceipt("unavailable", (), "unknown", "unknown", "unknown", error_code="STORAGE_UNAVAILABLE")
    monkeypatch.setattr(capture_inbox, "record_event", lambda *args, **kwargs: unavailable)
    fake = SimpleNamespace(config=cfg, _ingress_authorizer=lambda _: context.allowed_scope_ids, core=core,
                           ingress_receipts=())
    assert RuntimeInstance._replay_ingress(fake, 8.0) == (capture_inbox.INGRESS_PENDING_GAP,)
    last = f"DEFERRED|{capture_inbox.__version__}|2026-01-01T00:00:00Z|{capture_inbox.DEFER_ATTEMPTS}|replay|TEST"
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
    capture_inbox.enqueue(core.storage, core.clock, context, source_event(
        source_event_key="TEST-stopped", content="TEST 停在这里。"), scope_id="TEST-a", host_scope=None)

    def busy(*args, **kwargs):
        raise TruthWriterBusyError()

    monkeypatch.setattr(capture_inbox, "_revalidated", busy)
    fake = SimpleNamespace(config=cfg, _ingress_authorizer=lambda _: context.allowed_scope_ids, core=core,
                           ingress_receipts=())
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
    token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, source_event(
        source_event_key="TEST-rekey-given-up", content="TEST 换键时放弃的。"), scope_id="TEST-scope", host_scope=None)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE capture_inbox SET last_error_code=? WHERE token=?",
                     (f"GAVE_UP|{capture_inbox.__version__}|25|rekey|IDENTITY_UNBOUND:TEST-host", token))
        conn.commit()
    assert core.retry_failed_work(ctx, limit=64, dry_run=False)["inbox_given_up"] == 1
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
        token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, source_event(
            source_event_key=f"TEST-busy-{index}", content=f"TEST 第{index}条。"), scope_id="TEST-scope", host_scope=None)
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
    fake = SimpleNamespace(config=cfg, _ingress_authorizer=lambda _: context.allowed_scope_ids, core=core,
                           ingress_receipts=())
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
    receipt = SimpleNamespace(items=(), idle=False, retried=0, processed=0, completed=0, failed=0, skipped=0, stale=0,
                              obsolete=0, deferred=0, recovered=0, unavailable_work_types=())
    ingress = tuple(CaptureReceipt("queued", (), "queued", "pending", "pending", error_code=code)
                    for code in ("GAVE_UP", "DEFERRED", "DEFERRED"))
    queue_state = SimpleNamespace(work_error_counts=(), failed_work=0, pending_work=0, oldest_pending_at=None)
    instance = SimpleNamespace(auxiliary=SimpleNamespace(ledger_path=None, capability_gaps=()),
                               drain=lambda **kwargs: receipt, background_gaps=(), provider_holds={},
                               ingress_receipts=ingress, status=lambda **kwargs: queue_state)
    gaps = {"capture_gap:durable_ingress_deferred", "capture_gap:durable_ingress_given_up"}
    payload = _drain_once(cfg, instance, time.monotonic() + 10)
    assert (payload["ingress_deferred"], payload["ingress_given_up"]) == (2, 1) and gaps <= set(payload["capability_gaps"])
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

    counts, gaps = _ingress_report((receipt("queued", "queued", "DEFERRED"), receipt("queued", "queued", "GAVE_UP"),
                                    receipt("queued", "queued", "GAVE_UP"), receipt("inserted", "persisted"),
                                    receipt("cancelled", "not_persisted", "ACCESS_DENIED")))
    assert counts == {"ingress_deferred": 1, "ingress_given_up": 2, "ingress_replayed": 1, "ingress_cancelled": 1}
    assert gaps == ["capture_gap:durable_ingress_deferred", "capture_gap:durable_ingress_given_up"]
    assert _ingress_report(()) == ({"ingress_deferred": 0, "ingress_given_up": 0, "ingress_replayed": 0,
                                    "ingress_cancelled": 0}, [])


def test_a_named_message_that_was_deleted_still_counts_as_said(worker_app):
    """Once a delete is purged, a named message's rows no longer carry its key, and a Stop's read of the session
    record stored the words again under a key of the record's: deleted words came back (review of 3.4.0rc10)."""
    core, ctx, _clock = worker_app
    source = capture(core, ctx, "TEST 要删掉的一句话。", key="TEST-named-deleted")
    authorize(core, ctx, source)
    deleted = core.forget(ctx, request(source), remaining_seconds=5)
    core.purge_sqlite(ctx, deleted["operation_id"], remaining_seconds=10)
    said = core.said_in_session(ctx, "TEST-scope", [("user", "TEST 要删掉的一句话。", source.event["occurred_at"],
                                                     "TEST-named-deleted")])
    assert tuple(said) == (True,)


def test_a_long_key_that_was_taken_is_cut_to_fit_its_new_key(worker_app):
    """A host key of 490 characters or more went past the 512-character limit once the marker and the fingerprint
    were added, and was refused on every pass."""
    core, ctx, clock = worker_app
    key = "TEST-" + "k" * 500
    first = source_event(source_event_key=key, content="TEST first long-keyed message")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(core.storage, clock, other, dict(first, content="TEST second long-keyed message"),
                                       scope_id="TEST-scope", host_scope=None)
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)
    assert [receipt.durability for receipt in receipts] == ["persisted"]
    with sqlite3.connect(core.storage.path) as conn:
        rekeyed = conn.execute("SELECT source_event_key FROM source_events WHERE content='TEST second long-keyed message'"
                               ).fetchone()[0]
    assert len(rekeyed) == 512 and "#rekey:" in rekeyed and rekeyed.startswith("TEST-kkk")


def test_a_long_message_waiting_in_the_inbox_counts_as_said(worker_app):
    """A message over 65,536 characters waits in the inbox as segments under keys of their own: looked up by the
    host's key it was not found, and a session-record read stored it a second time."""
    core, ctx, clock = worker_app
    event = source_event(source_event_key="TEST-long-waiting", content="TEST 很长的等待中的消息。" * 6000)
    token, _prepared = capture_inbox.enqueue(core.storage, clock, ctx, event, scope_id="TEST-scope", host_scope=None)
    assert token is not None
    said = core.said_in_session(ctx, "TEST-scope",
                                [(event["role"], event["content"], event["occurred_at"], "TEST-long-waiting")])
    assert tuple(said) == (True,)


def test_a_capture_that_conflicts_again_under_its_new_key_stays_final(worker_app, monkeypatch):
    """Written back as a bare conflict, such a row was given the same new key and refused on every pass, and it
    woke the worker every 30 s (review of 3.4.0rc10)."""
    core, ctx, clock = worker_app
    first = source_event(source_event_key="TEST-turn-43", content="TEST first message")
    capture_inbox.durable_record_event(core.storage, clock, ctx, first, scope_id="TEST-scope", host_scope=None)
    other = replace(ctx, session_id="TEST-session-2")
    capture_inbox.durable_record_event(core.storage, clock, other, dict(first, content="TEST second message"),
                                       scope_id="TEST-scope", host_scope=None)
    # A new key that collides as well.
    monkeypatch.setattr(capture_inbox, "_rekeyed_event", lambda event, capture="": dict(event))
    receipts = capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5)
    assert [receipt.disposition for receipt in receipts] == ["conflict"]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT last_error_code FROM capture_inbox").fetchall() == [("VERSION_CONFLICT:rekeyed",)]
    assert capture_inbox.resolve_conflicted_ingress(
        core.storage, clock, other, authorize=lambda _: other.allowed_scope_ids, remaining_seconds=5) == ()
