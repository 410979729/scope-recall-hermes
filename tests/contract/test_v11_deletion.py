"""P06 online/read/restore fencing in isolated SQLite; no host or vector claims."""
from dataclasses import replace
import json
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.visibility import ObjectRef
from scope_recall.core.restore import InstallationMaintenance,begin_restore,export_deletion_ledger,ledger_digest,replay_deletion_ledger
from test_v11_claims import app,capture,initial,accept,draft,counts


def request(*items,mode="delete"):
    return dict(protocol_version="1.1",target_refs=[item.ref for item in items],mode=mode,
                expected_revisions={item.ref:item.revision for item in items})


def authorize(core,ctx,*items,mode="delete"):
    return capture(core,ctx,("删除 " if mode=="delete" else "不要主动提 ")+" ".join(item.ref for item in items),when="2026-09-06T12:00:00Z")


def test_C08_delete_blocks_every_existing_content_exit_before_physical_purge(app):
    core,ctx = app
    item,source = initial(core,ctx)
    echo = capture(core,ctx,"TEST-project 配色 蓝色。",origin="assistant_visible",evidence_refs=[f"{source.ref}@1"])
    authorization = authorize(core,ctx,source)
    epoch = core.status(ctx).memory_epoch
    cached = core.release_objects(ctx,(ObjectRef("claim",item.ref,1),),expected_epoch=epoch)
    assert cached[0].payload["value_text"] == "蓝色"
    result = core.forget(ctx,request(source),remaining_seconds=10)
    assert result["read_blocked"] and not result["active_content_removed"] and not result["declared_scope_complete"]
    assert result["memory_epoch"] > epoch
    assert all(core.source(ctx,obj.ref,1) is None for obj in (source,echo,authorization))
    assert core.current_claim(ctx,item.ref) is None and core.claim_history(ctx,item.ref) == ()
    assert not core.search_sources(ctx,"蓝色",history=True)
    for automatic in (False,True):
        with pytest.raises(ContractError,match="VERSION_CONFLICT"):
            core.release_objects(ctx,(ObjectRef("claim",item.ref,1),),expected_epoch=epoch,automatic=automatic,history=True)
    with pytest.raises(ContractError,match="SOURCE_MISSING"):
        core.release_objects(ctx,(ObjectRef("event",source.ref,1),),expected_epoch=result["memory_epoch"],automatic=False,history=True)


def test_C09_late_proposal_and_derived_capture_cannot_resurrect_deleted_source(app):
    core,ctx = app
    item,source = initial(core,ctx)
    proposal = draft(source)
    authorize(core,ctx,source)
    core.forget(ctx,request(source),remaining_seconds=10)
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError,match="SOURCE_MISSING"):
        accept(core,ctx,proposal)
    with pytest.raises(ContractError,match="deleted_derivation"):
        capture(core,ctx,"TEST echo 蓝色",origin="memory_reinjection",evidence_refs=[f"{source.ref}@1"])
    assert core.storage.path.read_bytes() == before


def test_delete_claim_also_fences_original_source_representation(app):
    core,ctx = app
    item,source = initial(core,ctx)
    authorize(core,ctx,item)
    core.forget(ctx,request(item),remaining_seconds=10)
    assert core.source(ctx,source.ref,1) is None and not core.search_sources(ctx,"蓝色",history=True)


def test_C30_suppress_blocks_auto_preserves_explicit_read_and_no_implicit_unsuppress(app):
    core,ctx = app
    item,source = initial(core,ctx)
    authorize(core,ctx,item,mode="suppress")
    result = core.forget(ctx,request(item,mode="suppress"),remaining_seconds=10)
    assert not result["read_blocked"] and result["suppressed"]
    assert core.current_claim(ctx,item.ref).payload["value_text"] == "蓝色"
    assert core.source(ctx,source.ref,1).event["content"] == "TEST-project 配色 蓝色。"
    assert not core.search_sources(ctx,"蓝色",automatic=True)
    assert core.search_sources(ctx,"蓝色",history=True)
    with pytest.raises(ContractError,match="SOURCE_MISSING"):
        core.release_objects(ctx,(ObjectRef("claim",item.ref,1),),expected_epoch=result["memory_epoch"])
    assert core.release_objects(ctx,(ObjectRef("claim",item.ref,1),),expected_epoch=result["memory_epoch"],automatic=False)
    assert core.current_claim(ctx,item.ref).suppressed


def test_new_derivation_of_suppressed_source_inherits_auto_policy(app):
    core,ctx = app
    source = capture(core,ctx,"TEST-project 配色 蓝色。")
    authorize(core,ctx,source,mode="suppress")
    core.forget(ctx,request(source,mode="suppress"),remaining_seconds=10)
    item = accept(core,ctx,draft(source)).items[0]
    assert core.current_claim(ctx,item.ref).suppressed
    echo = capture(core,ctx,"TEST-project 配色 蓝色。",origin="host_generated",evidence_refs=[f"{source.ref}@1"])
    assert echo.suppressed
    assert not core.search_sources(ctx,"蓝色",automatic=True)


def test_delete_idempotence_is_zero_mutation_even_after_authorization_source_removed(app):
    core,ctx = app
    item,source = initial(core,ctx)
    authorize(core,ctx,source)
    one = core.forget(ctx,request(source),remaining_seconds=10)
    before = core.storage.path.read_bytes()
    two = core.forget(ctx,request(source),remaining_seconds=10)
    assert one==two and core.storage.path.read_bytes()==before


def test_unknown_and_unauthorized_targets_use_same_error_and_do_not_disclose_existence(app):
    core,ctx = app
    item,source = initial(core,ctx)
    other = replace(ctx,project_id="TEST-other")
    for ref in (source.ref,"event-"+"f"*64):
        with pytest.raises(ContractError) as error:
            core.forget(other,dict(protocol_version="1.1",mode="delete",target_refs=[ref],expected_revisions={ref:1}))
        assert (error.value.code,error.value.field)==("ACCESS_DENIED","target_unavailable")
    assert core.source(ctx,source.ref,1)


def test_expected_revision_and_explicit_batch_authorization_are_required(app):
    core,ctx = app
    item,source = initial(core,ctx)
    second = capture(core,ctx,"TEST other source")
    authorize(core,ctx,source)
    with pytest.raises(ContractError,match="explicit_batch_required"):
        core.forget(ctx,request(source,second))
    bad = request(source); bad["expected_revisions"]={}
    with pytest.raises(ContractError,match="explicit_target_versions_required"):
        core.forget(ctx,bad)
    bad["expected_revisions"]={source.ref:2}
    with pytest.raises(ContractError,match="VERSION_CONFLICT"):
        core.forget(ctx,bad)
    authorize(core,ctx,source,second)
    assert core.forget(ctx,request(source,second),remaining_seconds=10)["read_blocked"]


@pytest.mark.parametrize("text",["不要删除 ","是否删除 ","删除了吗？ "])
def test_negative_or_unsettled_delete_does_not_change_any_target(app,text):
    core,ctx = app
    item,source = initial(core,ctx)
    capture(core,ctx,text+source.ref)
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError,match="forget_not_authorized"):
        core.forget(ctx,request(source))
    assert core.storage.path.read_bytes()==before


def test_C11_epoch_change_rejects_old_claim_release_without_vector_body_fallback(app):
    core,ctx = app
    item,source = initial(core,ctx,value="H100",kind="fact")
    epoch = core.status(ctx).memory_epoch
    capture(core,ctx,"刚才写错了，TEST-project 用H200。",when="2026-09-03T12:00:00Z")
    # The correction supersedes revision 1: the release refuses it by its own revision, whatever the epoch did.
    with pytest.raises(ContractError,match="VERSION_CONFLICT"):
        core.release_objects(ctx,(ObjectRef("claim",item.ref,1),),expected_epoch=epoch)
    assert core.current_claim(ctx,item.ref).payload["value_text"]=="H200"


def test_C20_authority_failure_prevents_release(app,monkeypatch):
    core,ctx = app
    item,source = initial(core,ctx)
    epoch = core.status(ctx).memory_epoch
    def unavailable(*args,**kwargs): raise sqlite3.OperationalError("TEST unavailable")
    monkeypatch.setattr(core.storage,"_open",unavailable)
    with pytest.raises(sqlite3.OperationalError):
        core.release_objects(ctx,(ObjectRef("event",source.ref,1),),expected_epoch=epoch)


def test_purge_removes_source_claim_quote_and_lexical_payloads_and_reports_remaining_layers(app):
    core,ctx = app
    item,source = initial(core,ctx,value="TEST_ERASE_PAYLOAD_937591")
    authorize(core,ctx,source)
    deleted = core.forget(ctx,request(source),remaining_seconds=10)
    result = core.purge_sqlite(ctx,deleted["operation_id"],remaining_seconds=10)
    assert not result["active_content_removed"] and result["layers"]["sqlite_active"]=="removed"
    assert result["layers"]["sqlite_history"]=="maintenance_pending" and result["layers"]["vector_history"]=="inventory_pending"
    assert not result["declared_scope_complete"]
    with sqlite3.connect(core.storage.path) as conn:
        for table,column in (("source_events","content"),("source_events","extra_json"),("claim_versions","payload_json"),("evidence_links","quote"),("claims","subject"),("deletion_operations","layers_json")):
            assert not any("TEST_ERASE_PAYLOAD_937591" in r[0] for r in conn.execute(f"SELECT {column} FROM {table}"))
        assert not conn.execute("SELECT 1 FROM lexical_postings WHERE source_id IN (SELECT source_id FROM source_events WHERE event_id=?)",(source.ref,)).fetchone()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    before = core.storage.path.read_bytes()
    assert core.purge_sqlite(ctx,deleted["operation_id"],remaining_seconds=10)==result
    assert core.storage.path.read_bytes()==before


def sqlite_backup(source,target):
    with sqlite3.connect(source) as reader,sqlite3.connect(target) as writer:
        reader.backup(writer)


def test_C10_restored_backup_stays_closed_until_latest_ledger_replayed(app,tmp_path):
    core,ctx = app
    item,source = initial(core,ctx,value="TEST_DELETE_BEFORE_RESTORE")
    backup = tmp_path/"TEST-backup.sqlite3"
    sqlite_backup(core.storage.path,backup)
    authorize(core,ctx,source)
    deleted = core.forget(ctx,request(source),remaining_seconds=10)
    ledger = export_deletion_ledger(core.storage,InstallationMaintenance(ctx))
    assert "TEST_DELETE_BEFORE_RESTORE" not in json.dumps(ledger)
    begin_restore(core.storage,InstallationMaintenance(ctx),expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    for read in (lambda:core.source(ctx,source.ref,1),lambda:core.current_claim(ctx,item.ref),core.initialize):
        with pytest.raises(ContractError,match="RESTORE_UNVERIFIED"):
            read()
    incomplete = dict(ledger,operations=[])
    with pytest.raises(ContractError,match="ledger_incomplete"):
        replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),incomplete)
    assert (ctx.binding.data_directory/"restore-required.json").is_file()
    assert replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),ledger)["status"]=="deletion_ledger_replayed"
    assert core.source(ctx,source.ref,1) is None and core.current_claim(ctx,item.ref) is None
    assert not core.search_sources(ctx,"TEST_DELETE_BEFORE_RESTORE",history=True)


def test_restore_replays_withdrawal_without_inventing_missing_human_source(app,tmp_path):
    core,ctx = app
    item,source = initial(core,ctx)
    backup = tmp_path/"TEST-withdraw-backup.sqlite3"
    sqlite_backup(core.storage.path,backup)
    capture(core,ctx,"撤回 TEST-project 配色蓝色。",when="2026-09-03T12:00:00Z")
    ledger = export_deletion_ledger(core.storage,InstallationMaintenance(ctx))
    begin_restore(core.storage,InstallationMaintenance(ctx),expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),ledger)
    assert core.current_claim(ctx,item.ref) is None
    restored = core.claim_history(ctx,item.ref)[-1]
    assert restored.state=="retracted" and restored.reason=="restored_governance_ledger" and restored.basis=="unknown"
    assert core.current_claim(ctx,item.ref,as_of="2026-09-02T00:00:00Z").payload["value_text"]=="蓝色"


def test_deleted_absent_snapshot_member_cannot_be_reimported_later(app,tmp_path):
    core,ctx = app
    backup = tmp_path/"TEST-empty-backup.sqlite3"
    sqlite_backup(core.storage.path,backup)
    item,source = initial(core,ctx)
    authorize(core,ctx,source)
    core.forget(ctx,request(source),remaining_seconds=10)
    ledger = export_deletion_ledger(core.storage,InstallationMaintenance(ctx))
    begin_restore(core.storage,InstallationMaintenance(ctx),expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),ledger)
    with pytest.raises(ContractError,match="source_unavailable"):
        core.record_event(ctx,source.event,scope_id="TEST-scope",remaining_seconds=10)


def test_intention_dependency_and_duplicate_quote_edges_are_erased_together(app):
    from test_v11_claims import intention
    core,ctx = app
    source = capture(core,ctx,"TEST-project 验收前提醒检查散热。")
    item = accept(core,ctx,intention(source)).items[0]
    authorize(core,ctx,source)
    operation = core.forget(ctx,request(source),remaining_seconds=10)
    result = core.purge_sqlite(ctx,operation["operation_id"],remaining_seconds=10)
    assert not result["active_content_removed"] and core.current_claim(ctx,item.ref) is None
    with sqlite3.connect(core.storage.path) as conn:
        assert not any(r[0] for r in conn.execute("SELECT quote FROM evidence_links WHERE source_ref=?",(source.ref,)))


def test_missing_future_segment_is_still_denied_after_group_key_payload_purge(app):
    core,ctx = app
    part = capture(core,ctx,"TEST incomplete first half",key="TEST-part1",segment=dict(group_key="TEST-full-source",index=0,total=2,truncated=False))
    authorize(core,ctx,part)
    deleted = core.forget(ctx,request(part),remaining_seconds=10)
    core.purge_sqlite(ctx,deleted["operation_id"],remaining_seconds=10)
    with pytest.raises(ContractError,match="source_unavailable"):
        capture(core,ctx,"TEST late deleted tail",key="TEST-part2",segment=dict(group_key="TEST-full-source",index=1,total=2,truncated=False))


def test_fresh_explicit_mention_does_not_permanently_remove_same_scoped_suppression(app):
    core,ctx = app
    item,source = initial(core,ctx)
    authorize(core,ctx,item,mode="suppress")
    core.forget(ctx,request(item,mode="suppress"),remaining_seconds=10)
    fresh = capture(core,ctx,"我现在明确讨论TEST-project 配色 蓝色。",when="2026-09-06T12:00:00Z")
    assert fresh.suppressed
    assert core.source(ctx,fresh.ref,1)
    assert not core.search_sources(ctx,"蓝色",automatic=True)


def test_failure_after_all_blocks_rolls_back_epoch_operations_and_visibility(app,monkeypatch):
    from scope_recall.core.delete_storage import Deletions
    core,ctx = app
    item,source = initial(core,ctx)
    authorize(core,ctx,source)
    before = core.storage.path.read_bytes()
    apply = Deletions.apply_blocks
    def fail(self,*args,**kwargs):
        apply(self,*args,**kwargs)
        raise RuntimeError("TEST block failure")
    monkeypatch.setattr(Deletions,"apply_blocks",fail)
    with pytest.raises(RuntimeError,match="TEST block failure"):
        core.forget(ctx,request(source),remaining_seconds=10)
    assert core.storage.path.read_bytes()==before and core.current_claim(ctx,item.ref)


def test_cancelled_intention_does_not_return_to_pending_after_restore(app,tmp_path):
    from test_v11_claims import intention
    core,ctx = app
    source = capture(core,ctx,"TEST-project 验收前提醒检查散热。")
    item = accept(core,ctx,intention(source)).items[0]
    backup = tmp_path/"TEST-intention-backup.sqlite3"
    sqlite_backup(core.storage.path,backup)
    cancellation = capture(core,ctx,"TEST-project 散热检查约定取消了。",when="2026-09-03T12:00:00Z")
    accept(core,ctx,intention(cancellation,"cancelled"))
    ledger = export_deletion_ledger(core.storage,InstallationMaintenance(ctx))
    begin_restore(core.storage,InstallationMaintenance(ctx),expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),ledger)
    restored = core.current_claim(ctx,item.ref)
    assert restored.payload["intention"]["state"]=="cancelled"
    with pytest.raises(ContractError,match="SOURCE_MISSING"):
        core.release_objects(ctx,(ObjectRef("claim",item.ref,restored.revision),),expected_epoch=core.status(ctx).memory_epoch)


def test_withdrawn_object_missing_from_backup_cannot_be_late_reconstructed(app,tmp_path):
    core,ctx = app
    backup = tmp_path/"TEST-pre-claim-backup.sqlite3"
    sqlite_backup(core.storage.path,backup)
    item,source = initial(core,ctx)
    capture(core,ctx,"撤回 TEST-project 配色蓝色。",when="2026-09-03T12:00:00Z")
    ledger = export_deletion_ledger(core.storage,InstallationMaintenance(ctx))
    begin_restore(core.storage,InstallationMaintenance(ctx),expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),ledger)
    core.record_event(ctx,source.event,scope_id="TEST-scope",remaining_seconds=10)
    with pytest.raises(ContractError,match="claim_unavailable"):
        accept(core,ctx,draft(source))


def test_restore_epoch_never_reuses_any_pre_restore_packet_epoch(app,tmp_path):
    core,ctx = app
    item,source = initial(core,ctx)
    backup = tmp_path/"TEST-epoch-backup.sqlite3"
    sqlite_backup(core.storage.path,backup)
    for index in range(8):
        capture(core,ctx,f"TEST unrelated event {index}")
    last_epoch = core.status(ctx).memory_epoch
    # Deleted after a reader held last_epoch; the older file still has it, the replayed ledger does not.
    authorize(core,ctx,item)
    core.forget(ctx,request(item),remaining_seconds=10)
    ledger = export_deletion_ledger(core.storage,InstallationMaintenance(ctx))
    begin_restore(core.storage,InstallationMaintenance(ctx),expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    replay_deletion_ledger(core.storage,InstallationMaintenance(ctx),ledger)
    assert core.status(ctx).memory_epoch > last_epoch
    with pytest.raises(ContractError,match="memory_epoch"):
        core.release_objects(ctx,(ObjectRef("claim",item.ref,1),),expected_epoch=last_epoch)


def test_ordinary_context_with_all_scopes_has_no_installation_maintenance_authority(app):
    core,ctx = app
    initial(core,ctx)
    for context in (ctx,replace(ctx,project_id="TEST-other"),replace(ctx,actor_origin="host_generated")):
        for call in (lambda:export_deletion_ledger(core.storage,context),
                     lambda:begin_restore(core.storage,context,expected_ledger_sha256="a"*64),
                     lambda:replay_deletion_ledger(core.storage,context,{})):
            with pytest.raises(ContractError,match="restore_authority"):
                call()
    assert not (ctx.binding.data_directory/"restore-required.json").exists()


def test_purge_failure_keeps_content_blocked_and_can_retry(app,monkeypatch):
    from test_v11_storage import inject,InjectedFailure,storage_module
    core,ctx = app
    item,source = initial(core,ctx)
    authorize(core,ctx,source)
    op = core.forget(ctx,request(source),remaining_seconds=10)
    before = core.storage.path.read_bytes()
    actual,_ = inject(monkeypatch,operation="UPDATE claim_versions SET payload_json")
    with pytest.raises(InjectedFailure):
        core.purge_sqlite(ctx,op["operation_id"],remaining_seconds=10)
    monkeypatch.setattr(storage_module,"connect_truth_database",actual)
    assert core.storage.path.read_bytes()==before
    assert core.source(ctx,source.ref,1) is None and core.current_claim(ctx,item.ref) is None
    assert not core.purge_sqlite(ctx,op["operation_id"],remaining_seconds=10)["active_content_removed"]


def test_question_word_without_punctuation_cannot_authorize_delete(app):
    core,ctx = app
    item,source = initial(core,ctx)
    capture(core,ctx,"可以删除 "+source.ref+" 吗")
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError,match="forget_not_authorized"):
        core.forget(ctx,request(source))
    assert core.storage.path.read_bytes()==before
    assert core.source(ctx,source.ref,source.revision) is not None


def test_restore_failed_commit_keeps_marker_closed_and_replay_retries(app,tmp_path,monkeypatch):
    from test_v11_storage import inject,InjectedFailure,storage_module
    core,ctx = app
    item,source = initial(core,ctx)
    backup = tmp_path/"TEST-restore-failure.sqlite3"
    sqlite_backup(core.storage.path,backup)
    authorize(core,ctx,source)
    core.forget(ctx,request(source),remaining_seconds=10)
    authority = InstallationMaintenance(ctx)
    ledger = export_deletion_ledger(core.storage,authority)
    begin_restore(core.storage,authority,expected_ledger_sha256=ledger_digest(ledger))
    sqlite_backup(backup,core.storage.path)
    actual,_ = inject(monkeypatch,operation="COMMIT")
    with pytest.raises(InjectedFailure):
        replay_deletion_ledger(core.storage,authority,ledger)
    monkeypatch.setattr(storage_module,"connect_truth_database",actual)
    with pytest.raises(ContractError,match="RESTORE_UNVERIFIED"):
        core.source(ctx,source.ref,1)
    replay_deletion_ledger(core.storage,authority,ledger)
    assert core.source(ctx,source.ref,1) is None and core.current_claim(ctx,item.ref) is None


def test_a_capture_between_a_read_and_its_release_withdraws_nothing(app):
    """Another entry's capture moves the epoch; only a deletion in the reader's scopes refuses the release."""
    core,ctx = app
    item,source = initial(core,ctx)
    epoch = core.status(ctx).memory_epoch
    capture(core,ctx,"TEST 另一个入口刚记下的一句无关的话。")
    assert core.status(ctx).memory_epoch > epoch
    assert not core.memory_retracted_since(ctx,epoch)
    released = core.release_objects(ctx,(ObjectRef("claim",item.ref,item.revision),),expected_epoch=epoch)
    assert [one.revision for one in released] == [item.revision]
    authorize(core,ctx,item)
    core.forget(ctx,request(item),remaining_seconds=10)
    assert core.memory_retracted_since(ctx,epoch)
    with pytest.raises(ContractError,match="memory_epoch"):
        core.release_objects(ctx,(ObjectRef("event",source.ref,source.revision),),expected_epoch=epoch)
