"""Host lifecycle and bounded observation cache regressions."""
from dataclasses import replace
import json
import sqlite3
from unittest.mock import Mock

from scope_recall.adapters.hermes.boundary import SourceObservationLedger, pre_llm_source_event
from scope_recall.adapters.hermes.outcomes import TurnOutcomeTracker
from scope_recall.adapters.hermes import ScopeRecallHermesAdapter
from scope_recall.adapters.hermes import hooks


def test_evicted_long_event_replay_retains_original_occurrence_and_identity(adapter):
    provider, clock=adapter
    text='TEST first witnessed long document '+('a'*65537)
    provider.observe_pre_llm(session_id='TEST-session-1',turn_id='TEST-long',user_message=text)
    core=provider._core
    with sqlite3.connect(core.storage.path) as db:
        before=db.execute('SELECT event_id,occurred_at,recorded_at,content FROM source_events ORDER BY event_id').fetchall()
    assert len(before)==2
    for n in range(1024):
        provider._ledger.confirm((f'TEST-evict-{n}',1))
    clock.now='2026-09-07T12:00:00Z'
    provider.observe_pre_llm(session_id='TEST-session-1',turn_id='TEST-long',user_message=text)
    with sqlite3.connect(core.storage.path) as db:
        after=db.execute('SELECT event_id,occurred_at,recorded_at,content FROM source_events ORDER BY event_id').fetchall()
    assert before==after
    assert not provider.diagnostics.capture_failures
    assert len(provider._ledger._confirmed)==1024


def test_read_only_session_end_never_submits_background_write(adapter,monkeypatch):
    provider,_=adapter
    provider._identity=replace(provider._identity,read_only=True)
    submit=Mock(); monkeypatch.setattr(provider._worker,'submit',submit)
    provider.on_session_end([])
    submit.assert_not_called()


def test_old_session_retry_preserves_original_scope_session_and_provenance(adapter,monkeypatch):
    provider,clock=adapter
    core=provider._core; original=core.record_host_event
    monkeypatch.setattr(core,'record_host_event',Mock(side_effect=RuntimeError('TEST transient storage')))
    provider.observe_pre_llm(session_id='TEST-session-1',turn_id='TEST-retry',user_message='TEST original user input')
    assert len(provider._retry_captures)==1
    old_scope=provider._identity.local_scope_id
    provider.on_session_switch('TEST-new-session')
    clock.now='2026-09-07T12:00:00Z'
    monkeypatch.setattr(core,'record_host_event',original)
    provider.on_pre_compress([])
    with sqlite3.connect(core.storage.path) as db:
        rows=db.execute('SELECT session_id,scope_id,extra_json,occurred_at,recorded_at FROM source_events').fetchall()
    assert len(rows)==1 and rows[0][:2]==('TEST-session-1',old_scope)
    assert '"platform":"cli"' in rows[0][2]
    assert rows[0][3:]==('2026-09-06T12:00:00Z','2026-09-06T12:00:00Z')
    assert not provider._retry_captures and not provider._current_source_refs
    # Cached initialize authority remains unchanged, but the current manifest
    # revokes this principal. A retry must consult the fresh authorization.
    monkeypatch.setattr(core,'record_host_event',Mock(side_effect=RuntimeError('TEST transient storage')))
    provider.observe_pre_llm(session_id='TEST-new-session',turn_id='TEST-revoked',user_message='TEST revoked capture')
    assert len(provider._retry_captures)==1
    manifest_path=provider._identity.hermes_home/'scope-recall/installation.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['owner_principals']=[dict(platform='cli',user_id='TEST-replacement-owner')]
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
    assert old_scope in provider._identity.runtime_audience.allowed_scope_ids
    record=Mock(wraps=original)
    monkeypatch.setattr(core,'record_host_event',record)
    provider.on_pre_compress([])
    record.assert_not_called()
    assert not provider._retry_captures
    assert 'capture_gap:retry_authorization_revoked' in provider.diagnostics.pending_outcome_gaps
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0]==1


def test_failed_retry_buffer_is_counted_once_with_pending_ledger_at_shutdown(adapter,monkeypatch):
    provider,_=adapter
    monkeypatch.setattr(provider._core,'record_host_event',Mock(side_effect=RuntimeError('TEST transient storage')))
    provider.observe_pre_llm(session_id='TEST-session-1',turn_id='TEST-pending',user_message='TEST pending capture')
    assert not provider._ledger.pending_identities()
    assert len(provider._retry_captures)==1
    assert len(provider.diagnostics.pending_capture_identities)==1
    assert 'capability_gap:durable_capture_ingress_unavailable' in provider.diagnostics.pending_outcome_gaps
    context=provider._identity.trusted_context()
    for turn in ('TEST-pending','TEST-ledger-only'):
        pre_llm_source_event(provider._ledger,context,session_id='TEST-session-1',turn_id=turn,
                             user_message='TEST pending capture',recorded_at='2026-09-06T12:00:00Z')
    assert len(provider.diagnostics.pending_capture_identities)==2
    provider.shutdown()
    assert provider.diagnostics.shutdown_state['pending_captures']==2
    assert provider.diagnostics.shutdown_state['pending_capture_status']=='unpersisted'
    assert provider.diagnostics.shutdown_state['pending_capture_durability']=='memory_only'


def test_outcome_history_is_bounded():
    outcomes=TurnOutcomeTracker()
    for n in range(2000):
        outcomes.open_turn('TEST',str(n)); outcomes.mark_failure('TEST',str(n),reason='TEST')
    assert len(outcomes._turns)<=256 and len(outcomes._gaps)<=128
    assert len(outcomes.pending_gaps())<=384


def test_durable_ingress_preserves_original_host_authority_after_restart(adapter,monkeypatch):
    from scope_recall.core import capture_inbox
    from scope_recall.contracts import ContractError
    provider,clock=adapter
    original=capture_inbox.record_event
    monkeypatch.setattr(capture_inbox,'record_event',Mock(side_effect=ContractError('STORAGE_UNAVAILABLE')))
    provider.observe_pre_llm(session_id='TEST-session-1',turn_id='TEST-durable',user_message='TEST durable original user input')
    assert not provider._retry_captures
    assert provider.diagnostics.durable_pending_captures==1
    provider.on_session_switch('TEST-new-session')
    clock.now='2026-09-07T12:00:00Z'
    monkeypatch.setattr(capture_inbox,'record_event',original)
    provider.on_pre_compress([])
    assert provider.diagnostics.durable_pending_captures==0
    with sqlite3.connect(provider._core.storage.path) as db:
        row=db.execute('SELECT session_id,origin,extra_json,recorded_at FROM source_events').fetchone()
    assert row[0:2]==('TEST-session-1','human_direct')
    assert '"platform":"cli"' in row[2] and row[3]=='2026-09-06T12:00:00Z'


def test_same_binding_ambiguous_read_authority_does_not_dispatch(adapter,initialize_kwargs):
    provider,_=adapter
    duplicate=ScopeRecallHermesAdapter(core=provider._core)
    duplicate.initialize('TEST-session-1',**initialize_kwargs)
    duplicate._identity=replace(duplicate._identity,read_only=True)
    try:
        assert hooks._active_adapter({'session_id':'TEST-session-1'}) is None
    finally:
        duplicate.shutdown()


def test_basic_session_end_calls_existing_bounded_core_worker(adapter,monkeypatch):
    provider,_=adapter
    drain=Mock(); monkeypatch.setattr(provider._core,'drain_worker',drain)
    monkeypatch.setattr(provider._worker,'submit',lambda fn,**kw:fn())
    provider.on_session_end([])
    drain.assert_called_once()
    assert drain.call_args.kwargs=={'max_items':8,'remaining_seconds':1.0}
