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
        # A real 1106 database cannot contain the later candidate tables.
        for table in ('candidate_source_triggers','candidate_evaluations','candidate_trigger_terms',
                      'candidate_evidence','candidate_lifecycle','candidate_scan_cursors',
                      'capture_inbox','work_error_details','consolidation_fragments','consolidation_outcomes'):
            db.execute(f'DROP TABLE {table}')
        db.execute("UPDATE work_items SET state='failed',attempt=3,last_error_code='DERIVATION_INVALID' WHERE work_type='consolidate'")
        db.execute('UPDATE instance_meta SET schema_version=1106')
        db.execute('PRAGMA user_version=1106')
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
