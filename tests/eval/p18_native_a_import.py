"""Read-only verification of native A public raw-source import receipts."""
import hashlib
import json
from pathlib import Path
import tomllib
import sqlite3
import time


def _read(ref):
    path = Path(ref['path'])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref['sha256']:
        raise ValueError('native_A_bound_artifact_changed')
    return data


def project_records(records):
    items, provenance = [], []
    for record in records:
        role, text = record['speaker_role'], record['text']
        if role in {'user', 'assistant'}:
            item = {'type':'message', 'role':role, 'content':[{
                'type':'input_text' if role == 'user' else 'output_text', 'text':text}]}
        elif role == 'tool':
            item = {'type':'function_call_output', 'output':text}
        else:
            raise ValueError('native_A_unmapped_original_role')
        items.append(item)
        provenance.append({'event_id':record['event_id'], 'sequence':record['sequence'],
            'original_role':role, 'original_origin':record['source_type'],
            'original_occurred_at':record['occurred_at'], 'transport_origin':'synthetic_imported_history',
            'host_direct_capture_proven':False, 'text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()})
    return {'items':items, 'provenance':provenance}


def verify_native_import(row, binding, manifest_sha):
    """Prove the original import only; native generation is a separate observation."""
    refs = binding['native_source_import']
    if refs['manifest_sha256'] != manifest_sha:
        raise ValueError('native_A_raw_manifest_mismatch')
    expected = project_records(row['source_records'])
    if json.loads(_read(refs['source_input'])) != expected:
        raise ValueError('native_A_original_roles_or_source_changed')
    receipt = json.loads(_read(refs['receipt']))
    sid = receipt.get('source_thread_id')
    if (receipt.get('unit_id') != row['unit_id'] or receipt.get('status') != 'IMPORTED_SOURCE_ONLY'
            or not sid or sid != refs['source_session_id'] or receipt.get('turn_start_requests') != 0
            or receipt.get('source_items_matched') != len(expected['items'])
            or receipt.get('source_origin') != 'synthetic_imported_history'):
        raise ValueError('native_A_source_receipt_invalid')
    home = Path(binding['roots']['home_path']).resolve()
    if Path(receipt['initialize']['codexHome']).resolve() != home:
        raise ValueError('native_A_actual_home_mismatch')
    config_ref = binding['native_memory_config']
    if Path(config_ref['path']).resolve() != home/'config.toml':
        raise ValueError('native_A_actual_config_path_mismatch')
    config = tomllib.loads(_read(config_ref).decode('utf-8'))
    if (config.get('features', {}).get('memories') is not True
            or any(config.get('memories', {}).get(key) is not True
                   for key in ('generate_memories', 'use_memories'))):
        raise ValueError('native_A_actual_memory_disabled')
    rollout_ref = receipt['rollout']
    if not Path(rollout_ref['path']).resolve().is_relative_to(home/'sessions'):
        raise ValueError('native_A_rollout_outside_actual_home')
    rollout = [json.loads(line) for line in _read(rollout_ref).decode('utf-8').splitlines() if line.strip()]
    meta = [entry['payload'] for entry in rollout if entry.get('type') == 'session_meta']
    if len(meta) != 1 or meta[0].get('id') != sid:
        raise ValueError('native_A_rollout_source_session_mismatch')
    position = 0
    for entry in rollout:
        if entry.get('type') != 'response_item' or position == len(expected['items']):
            continue
        if all(entry['payload'].get(k) == v for k, v in expected['items'][position].items()):
            position += 1
    if position != len(expected['items']):
        raise ValueError('native_A_rollout_raw_source_missing')
    requests = json.loads(_read(refs['rpc_requests']))
    if any(request['method'] in {'turn/start', 'thread/resume'} for request in requests):
        raise ValueError('native_A_source_unexpected_turn_or_resume')
    injections = [r['params'] for r in requests if r['method'] == 'thread/inject_items']
    if injections != [{'threadId':sid, 'items':expected['items']}]:
        raise ValueError('native_A_source_injection_mismatch')
    return {'status':'IMPORTED_NATIVE_RAW_HISTORY', 'manifest_sha256':manifest_sha,
        'import_session_id':sid, 'source_capture_refs':[], 'original_roles_preserved':True,
        'host_L1_capture_proven':False, 'main_model_calls':0, 'native_generation_verified':False,
        'background_usage':'unknown_reserved', 'source_receipt':refs['receipt'], 'rollout':rollout_ref}


def native_source_state(binding, source_session_id):
    """Read actual native state; no SQL writes or fabricated generation."""
    home = Path(binding['roots']['home_path']).resolve()
    with sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT id,memory_mode,updated_at_ms,updated_at,has_user_event FROM threads WHERE id=?',
                         (source_session_id,)).fetchone()
        if row is None or row['memory_mode'] != 'enabled':
            raise ValueError('native_A_source_thread_missing_or_disabled')
        source = dict(row)
    with sqlite3.connect((home/'memories_1.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        outputs = [dict(row) for row in db.execute(
            'SELECT thread_id,source_updated_at,generated_at,selected_for_phase2,length(raw_memory) AS raw_memory_chars,'
            'length(rollout_summary) AS rollout_summary_chars FROM stage1_outputs WHERE thread_id=?', (source_session_id,))]
        jobs = [dict(row) for row in db.execute('SELECT kind,job_key,status,last_error FROM jobs')]
    files = []
    memory_root = home/'memories'
    if memory_root.is_dir():
        for path in sorted(memory_root.rglob('*')):
            if path.is_file():
                data = path.read_bytes()
                files.append({'path':str(path), 'sha256':hashlib.sha256(data).hexdigest(), 'bytes':len(data)})
    return {'source':source, 'stage1_outputs':outputs, 'jobs':jobs, 'native_memory_files':files,
            'observed_ns':time.time_ns(), 'native_generation_verified':bool(outputs),
            'verified_generation_stage':'stage1' if outputs else None,
            'full_native_consolidation_verified':False}


def assert_native_source_idle(binding, state):
    config = tomllib.loads(_read(binding['native_memory_config']).decode('utf-8'))
    idle = config['memories'].get('min_rollout_idle_hours', 6)*3600
    updated = state['source']['updated_at_ms']
    seconds = updated/1000 if updated is not None else state['source']['updated_at']
    if time.time() < seconds + idle:
        raise ValueError('native_A_source_not_yet_idle')
