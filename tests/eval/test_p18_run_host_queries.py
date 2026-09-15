"""Public offline executor tests; host doubles are never formal evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from p18_formal_evidence import _operation_map, EvidenceSchemaError
from p18_run_host_queries import (
    HostQueryError, _execute_codex_condition, _import_worker_retryable, import_condition_history,
    assert_host_query_identity_alignment, host_query_operation_map, query_units,
)
from p18_score_report import IDENTITY_MAP_SHA256, authoritative_query_pair_indices


def test_worker_native_temp_created_only_inside_owned_state(tmp_path):
    from p18_run_host_queries import _prepare_codex_worker_temp
    root=tmp_path/'TEST-binding';state=root/'state'
    binding={'roots':{'binding_root':str(root),'state_path':str(state)}}
    env={'TEMP':str(state/'Temp'),'TMP':str(state/'Tmp')}
    assert not state.exists()
    result=_prepare_codex_worker_temp(binding,env)
    assert all(Path(p).is_dir() for p in result.values())
    with pytest.raises(HostQueryError,match='temp_outside_owned_state'):
        _prepare_codex_worker_temp(binding,{**env,'TMP':str(tmp_path/'outside')})
    assert not (tmp_path/'outside').exists()


def _rows():
    return [{"unit_id":f"codex_windows_desktop-A-query-{pair:02d}-c{condition}","kind":"host_query",
             "ordinal":(pair-1)*2+condition,"source_sequence":"imported_history_then_new_session_query",
             "source_records":[{"event_id":f"PUBLIC-{pair}-{condition}","sequence":1,"occurred_at":"2026-01-01T00:00:00Z",
                                "source_type":"human_direct","speaker_role":"user","text":"PUBLIC natural source"}],
             "model_input":{"history":[{"source_type":"human_direct","speaker_role":"user","text":"PUBLIC natural source","occurred_at":"2026-01-01T00:00:00Z"}],
                            "query":{"text":"PUBLIC follow-up"}}}
            for pair in range(1,41) for condition in (1,2)]


def _sealed_identity_rows(*, pair_indices, host="hermes_a2a", arm="C"):
    rows = []
    for ordinal, pair_index in enumerate(pair_indices, start=1):
        for condition in (1, 2):
            event_id = f"R{pair_index:03d}{'A' if condition == 1 else 'B'}-E1"
            rows.append({
                "unit_id": f"{host}-{arm}-query-{ordinal:02d}-c{condition}",
                "kind": "host_query",
                "ordinal": (ordinal - 1) * 2 + condition,
                "source_sequence": "imported_history_then_new_session_query",
                "source_records": [{
                    "event_id": event_id, "sequence": 1, "occurred_at": "2026-01-01T00:00:00Z",
                    "source_type": "human_direct", "speaker_role": "user", "text": "PUBLIC natural source",
                }],
                "model_input": {
                    "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "PUBLIC natural source", "occurred_at": "2026-01-01T00:00:00Z"}],
                    "query": {"text": "PUBLIC follow-up"},
                },
            })
    return rows


def test_sealed_query_rows_reject_dataset_hash_identity_mismatch():
    misaligned = _sealed_identity_rows(pair_indices=range(1, 41))
    with pytest.raises(HostQueryError, match="query_identity_mismatch"):
        query_units(misaligned)
    aligned = _sealed_identity_rows(pair_indices=authoritative_query_pair_indices())
    assert len(query_units(aligned)) == 80
    assert_host_query_identity_alignment(aligned)


def test_mixed_sealed_source_pair_ids_are_rejected_before_dispatch():
    from p18_sealed_run_plan import _project_model_input

    rows = _sealed_identity_rows(pair_indices=authoritative_query_pair_indices())
    rows[0]["source_records"][0]["event_id"] = "R001A-E1"
    second = dict(rows[0]["source_records"][0], event_id="R002A-E2", sequence=2)
    rows[0]["source_records"].append(second)
    rows[0]["model_input"] = _project_model_input({"history": rows[0]["source_records"], "query": rows[0]["model_input"]["query"]})
    with pytest.raises(HostQueryError, match="identity"):
        query_units(rows)


def test_offline_public_query_units_remain_compatible_without_r_bindings():
    rows = _rows()
    assert len(query_units(rows)) == 80
    assert all(event["event_id"].startswith("PUBLIC-") for row in rows for event in row["source_records"])


def test_full_pair_denominator_has_no_main_model_calls_for_raw_import():
    rows = _rows()
    operations = host_query_operation_map(rows)
    assert len(_operation_map(operations)) == 80
    assert sum(op["unit"]["kind"]=="query" for op in operations.values()) == 80
    with pytest.raises(HostQueryError,match="exact_40_pairs"):
        query_units(rows[:-1])
    with pytest.raises(HostQueryError,match="duplicate"):
        query_units(rows+[rows[0]])
    next(iter(operations.values()))["unit"]["query_id"] = None
    with pytest.raises(EvidenceSchemaError,match="unit_invalid"):
        _operation_map(operations)


def test_legacy_fixture_label_requires_explicit_cli_method_mapping():
    rows = _rows()
    for row in rows:
        row["unit_id"] = row["unit_id"].replace("codex_windows_desktop", "hermes_a2a")
    legacy = host_query_operation_map(rows)
    revised = host_query_operation_map(rows, execution_method="hermes_cli_local_input_v1")
    assert set(legacy) == set(revised)
    assert all(op["host_id"] == "hermes_a2a" for op in legacy.values())
    assert all(op["host_id"] == "hermes_cli_local_input_v1" for op in _operation_map(revised).values())
    assert len(revised) == 80


def test_CLI_import_uses_actual_local_audience_and_preserves_imported_origin(tmp_path):
    from p18_host_arm_binding import _hermes_installation
    from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance
    from scope_recall.adapters.hermes.identity import bind_hermes_identity
    home = tmp_path/"TEST-cli-home"
    home.mkdir()
    installed = _hermes_installation(home, "C", "TEST-cli", cli=True)
    runtime_path = tmp_path/"runtime.json"
    payload = {"binding":installed["payload"],"session_id":"TEST-runtime","allowed_scope_ids":installed["payload"]["scope_ids"]}
    runtime_path.write_text(json.dumps(payload))
    row = _rows()[0]
    binding = {"host_id":"hermes_cli_local_input_v1","arm_id":"C","roots":{
        "runtime_config_path":str(runtime_path),"home_path":str(home)}}
    receipt = import_condition_history(row,binding)
    identity = bind_hermes_identity("TEST-reader",hermes_home=home,platform="cli",agent_identity="default",agent_workspace="hermes")
    runtime = build_runtime_instance(RuntimeInstanceConfig.from_mapping(payload))
    try:
        ref,revision = receipt["source_capture_refs"][0].rsplit("@",1)
        source = runtime.core.source(identity.trusted_context(),ref,int(revision))
        assert source.event["origin"] == "imported"
        assert source.event["source_original_origin"] == "human_direct"
        assert source.scope_id == identity.runtime_audience.capture_scope_id
        assert not receipt["host_L1_capture_proven"] and receipt["main_model_calls"] == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("arm,limit,complete", [("A",2200,True),("A",50,False),("B",2200,True)])
def test_real_public_baseline_import_uses_same_cli_home_and_source_envelopes(tmp_path, arm, limit, complete):
    from p18_hermes_baselines import FROZEN_HERMES_SOURCE, LEGACY_B_ARCHIVE
    from p18_run_host_queries import import_baseline_condition
    if not FROZEN_HERMES_SOURCE.is_dir() or not LEGACY_B_ARCHIVE.is_dir():
        pytest.skip("frozen public baseline source not installed")
    root = tmp_path/"TEST-baseline-import"
    root.mkdir()
    home = root/"home"
    home.mkdir()
    child_temp = root/"temp"
    child_temp.mkdir()
    config = home/"config.yaml"
    config.write_text(json.dumps({"memory":{"memory_char_limit":limit,"user_char_limit":1375}}))
    binding = {"host_id":"hermes_cli_local_input_v1","arm_id":arm,
        "roots":{"home_path":str(home),"config_path":str(config),"database_path":str(home/"scope-recall/memory.sqlite3")},
        "fixed_host":{"runtime_python_path":sys.executable,"source":{"path":str(FROZEN_HERMES_SOURCE)}},
        "loader":{"plugin_directory":{"path":str(LEGACY_B_ARCHIVE)} if arm == "B" else None}}
    class PublicOfflineChild:
        def run_process(self, argv, output, *, label, timeout_seconds):
            stdout,stderr=output/(label+".stdout"),output/(label+".stderr")
            env={"PATH":os.environ.get("PATH",""),"TEMP":str(child_temp),"TMP":str(child_temp),"PYTHONUTF8":"1"}
            # Simulate a credential inherited by the owned formal launcher.
            # The raw-import child must remove it before legacy initialization.
            env["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"] = "TEST-not-a-real-key"
            with stdout.open("wb") as out,stderr.open("wb") as err:
                with subprocess.Popen(argv,stdout=out,stderr=err,cwd=root,env=env,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)) as child:
                    child.wait(timeout=timeout_seconds)
                    assert child.returncode == 0, stderr.read_text(errors="replace")
                    return {"pid":child.pid,"returncode":child.returncode,"error":None,"stdout":stdout,"stderr":stderr}
    row=_rows()[0]
    for index,(origin,role) in enumerate((("assistant_visible","assistant"),("external_document","document")),2):
        row["source_records"].append({"event_id":f"PUBLIC-other-{index}","sequence":index,
            "occurred_at":"2026-01-01T00:00:00Z","source_type":origin,"speaker_role":role,"text":f"PUBLIC original {role} source"})
    receipt=import_baseline_condition(row,binding,process_owner=PublicOfflineChild(),output=root)
    assert receipt["status"]==("IMPORTED_BASELINE_HISTORY" if complete else "IMPORTED_WITH_GAPS"), (root/"baseline-import.stderr").read_text(errors="replace")
    assert receipt["stored_successes"]==(3 if complete else 0) and receipt["main_model_calls"]==0
    observed=json.loads(Path(receipt["result_artifact"]["path"]).read_bytes())
    inputs=json.loads(Path(receipt["input_artifact"]["path"]).read_bytes())
    envelope=inputs["envelopes"][0]
    assert envelope["text"]==row["source_records"][0]["text"] and envelope["speaker_role"]=="user"
    assert envelope["import_origin"]=="imported" and not receipt["host_L1_capture_proven"]
    assert [item["speaker_role"] for item in inputs["envelopes"]] == ["user","assistant","document"]
    assert observed["network_disabled"]
    if arm == "B":
        assert observed["embedding_deferred_to_host"] is True
    if arm=="A":
        assert observed["native_memory_char_limit"]==limit
        assert [json.loads(value) for value in observed["accepted_entries"]] == (inputs["envelopes"] if complete else [])
    else:
        import sqlite3
        with sqlite3.connect(binding["roots"]["database_path"]) as db:
            assert db.execute("SELECT name FROM sqlite_master WHERE name='memories'").fetchone()
            assert db.execute("SELECT name FROM sqlite_master WHERE name='source_events'").fetchone() is None
        db.close()
        from p18_run_host_queries import _BASELINE_IMPORT_SCRIPT
        prefix = _BASELINE_IMPORT_SCRIPT.split("entries =",1)[0]
        readback = prefix + '''
archive = pathlib.Path(payload["baseline_plugin"])
spec = importlib.util.spec_from_file_location("scope_recall", archive/"__init__.py", submodule_search_locations=[str(archive)])
module = importlib.util.module_from_spec(spec)
sys.modules["scope_recall"] = module
spec.loader.exec_module(module)
provider = importlib.import_module("scope_recall.provider").ScopeRecallMemoryProvider()
try:
    provider.initialize("TEST-real-new-session", hermes_home=payload["home"], platform="cli",
                        agent_context="primary", agent_identity="default", agent_workspace="hermes")
    recalled = provider.prefetch("PUBLIC natural source",session_id="TEST-real-new-session")
    assert "PUBLIC natural source" in recalled, repr(recalled)
finally:
    provider.shutdown(timeout=5)
'''
        PublicOfflineChild().run_process([sys.executable,"-I","-B","-c",readback,receipt["input_artifact"]["path"]],
            root,label="PUBLIC-fresh-readback",timeout_seconds=30)


@pytest.mark.parametrize("field",["gold","expected","required_facts"])
def test_model_projection_cannot_carry_control_fields(field):
    rows = _rows()
    rows[0]["model_input"][field] = "PUBLIC forbidden control"
    with pytest.raises(HostQueryError,match="natural_input"):
        query_units(rows)


def test_raw_import_preserves_all_original_roles_and_actual_Codex_scope(tmp_path):
    from scope_recall.adapters.codex.config import install_codex_scope_recall
    from scope_recall.adapters.codex.identity import resolve_runtime_audience, trusted_context
    workspace=tmp_path/"TEST-workspace"
    workspace.mkdir()
    installation,core=install_codex_scope_recall(tmp_path/"TEST-instance",project_root=workspace)
    runtime_path=tmp_path/"runtime.json"
    runtime_path.write_text(json.dumps({"binding":{"agent_id":installation.agent_id,"installation_id":installation.installation_id,
        "data_directory":str(installation.data_directory),"scope_ids":list(installation.scope_ids),"test_mode":True},
        "session_id":"TEST-public-runtime","allowed_scope_ids":list(installation.scope_ids)}))
    row=_rows()[0]
    row["source_records"]=[]
    for index,(origin,role) in enumerate((("human_direct","user"),("assistant_visible","assistant"),("tool_observation","tool")),1):
        row["source_records"].append({"event_id":f"PUBLIC-event-{index}","sequence":index,"occurred_at":"2026-01-01T00:00:00Z",
                                     "source_type":origin,"speaker_role":role,"text":f"PUBLIC raw {role}"})
    binding={"arm_id":"C","roots":{"runtime_config_path":str(runtime_path),"installation_config_path":str(installation.config_path)},
             "launch_contract":{"working_directory":str(workspace)}}
    receipt=import_condition_history(row,binding)
    audience=resolve_runtime_audience(installation,str(workspace))
    context=trusted_context(installation,audience,session_id="TEST-reader")
    assert receipt["main_model_calls"]==0 and receipt["host_L1_capture_proven"] is False
    assert receipt["import_receipt"]["inserted"]==3
    for ref,event in zip(receipt["source_capture_refs"],row["source_records"],strict=True):
        object_id,revision=ref.rsplit("@",1)
        source=core.source(context,object_id,int(revision))
        assert source.scope_id==audience.capture_scope_id
        assert source.event["origin"]=="imported"
        assert source.event["source_original_origin"]==event["source_type"]
        assert source.event["role"]==event["speaker_role"]


@pytest.mark.parametrize("arm,worker_exit", [("A",0),("C",0),("C",1)])
def test_concrete_codex_executor_imports_then_uses_new_observed_thread(tmp_path,monkeypatch,arm,worker_exit):
    import p18_run_host_queries as entry
    row=_rows()[0]
    root=tmp_path/"TEST-arm"
    workspace=root/"workspace"
    workspace.mkdir(parents=True)
    env=root/"environment.json"
    env.write_text("{}")
    binding={"arm_id":arm,"roots":{"binding_root":str(root),"environment_path":str(env),"database_path":str(root/"memory.sqlite3")},
             "launch_contract":{"working_directory":str(workspace)},"fixed_host":{"executable_path":"PUBLIC.exe"}}
    calls=[]
    class Bridge:
        def __init__(self,**kwargs):
            self.output=kwargs["output_root"]
            assert isinstance(kwargs["source_refs_provider"],entry.JourneySourceRefsProvider)
        def resume(self):
            calls.append("resume")
        def new_session(self,alias):
            calls.append("new:"+alias)
            return {"session_id":"actual-"+alias}
        def execute_turn(self,**kwargs):
            calls.append((kwargs["session_alias"],kwargs["model_input"]["query"]))
            artifact=self.output/(kwargs["operation_id"]+".json")
            artifact.write_text(json.dumps({"operation_id":kwargs["operation_id"],"status":"COMPLETED",
                                           "ids":{"session_id":kwargs["session_id"]}}))
            return {"session_id":kwargs["session_id"],"formal_evidence_path":str(artifact),"source_refs":["public-source@1"]}
        def close(self):
            calls.append("close")
    monkeypatch.setattr(entry,"CodexSubmissionBudget",lambda *_:SimpleNamespace())
    monkeypatch.setattr(entry,"CodexAppServerTransport",lambda *_,**__:SimpleNamespace())
    monkeypatch.setattr(entry,"CodexAppServerSessionControl",lambda *_:SimpleNamespace())
    monkeypatch.setattr(entry,"CodexJourneyHostBridge",Bridge)
    monkeypatch.setattr("p18_codex_appserver_transport.codex_binding_environment",lambda _: {})
    def importer(*_):
        calls.append("import")
        return {"status":"IMPORTED_RAW_HISTORY","source_capture_refs":["public-source@1"],"import_session_id":"actual-import"}
    monkeypatch.setattr(entry,"import_condition_history",importer)
    def worker(*_):
        calls.append("worker")
        return {"returncode":worker_exit,"error":None,"host_lifecycle_proven":False,"purpose":"imported_history_L2_preparation"}
    monkeypatch.setattr(entry,"_run_codex_import_worker",worker)
    monkeypatch.setattr(entry,"_wait_automatic_work",lambda *_,**__: {"status":"QUIESCENT","states":{"done":1}})
    readiness=SimpleNamespace(details={"ledger_path":str(tmp_path/"ledger.sqlite3")})
    receipt=_execute_codex_condition(row,binding,readiness,tmp_path/"formal.json",root/"execution")
    if worker_exit:
        assert receipt["status"] == "FAILED" and receipt["error"] == "import_worker_incomplete"
        assert calls == ["resume","import","worker","close"]
        return
    assert receipt["status"]=="EXECUTED" and receipt["semantic_pass"] is False
    assert receipt["import"]["import_session_id"] != receipt["query_session_id"]
    assert calls == ["resume","import"] + (["worker"] if arm=="C" else []) + ["new:query",("query","PUBLIC follow-up"),"close"]
    if arm=="C":
        assert receipt["import_worker"]["host_lifecycle_proven"] is False
        assert receipt["automatic_work"]["normal_worker_invoked_by_harness"] is True
    assert receipt["source_capture_refs"] == ["public-source@1"]
    assert receipt["query_evidence"]["sha256"]


def test_D_archive_is_same_raw_history_not_extra_answers(tmp_path):
    import p18_run_host_queries as entry
    row=_rows()[0]
    archive=tmp_path/"public-raw.jsonl"
    archive.write_text(json.dumps({"history":row["source_records"]}),encoding="utf-8")
    ref={"path":str(archive),"sha256":entry._sha(archive.read_bytes())}
    binding={"arm_id":"D","loader":{"archive":ref}}
    result=import_condition_history(row,binding)
    assert result["status"]=="IMPORTED_RAW_ARCHIVE" and result["main_model_calls"]==0
    assert result["host_L1_capture_proven"] is False
    archive.write_text(json.dumps({"history":row["source_records"]+[row["source_records"][0]]}),encoding="utf-8")
    ref["sha256"]=entry._sha(archive.read_bytes())
    with pytest.raises(HostQueryError,match="same_condition_raw"):
        import_condition_history(row,binding)


def _formal_preflight_bundle(tmp_path, *, rows, stamp=None):
    import p18_run_host_queries as entry
    root = tmp_path / "TEST-formal"
    root.mkdir(exist_ok=True)
    def artifact(name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": name, "sha256": entry._sha(content)}
    inputs = artifact("units.jsonl", "\n".join(json.dumps(row) for row in rows).encode())
    executable = root / "PUBLIC-never-executed.exe"
    executable.write_bytes(b"PUBLIC not an executable")
    method = artifact("method.json", json.dumps({"method": {"executable_sha256": entry._sha(executable.read_bytes()), "executable_version": "PUBLIC"}}).encode())
    bindings = {}
    for index, row in enumerate(rows):
        arm_root = root / f"arm-{index}"
        workspace = arm_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        binding = {"host_id": "codex_windows_desktop", "arm_id": "A",
                   "roots": {"binding_root": str(arm_root), "home_path": str(arm_root / "home"), "database_path": str(arm_root / "db.sqlite3")},
                   "launch_contract": {"working_directory": str(workspace)},
                   "fixed_host": {"executable_path": str(executable), "version": "PUBLIC"}}
        bindings[row["unit_id"]] = artifact(f"binding-{index}.json", json.dumps(binding).encode())
    binding_ref = artifact("bindings.json", json.dumps(bindings).encode())
    config = {"host_query_inputs": inputs, "host_query_bindings": binding_ref}
    if stamp is not None:
        config["identity_map_sha256"] = stamp
    config_path = root / "formal.json"
    config_path.write_text(json.dumps(config))
    return entry, root, rows, inputs, method, bindings, artifact, config_path


def test_batch_preflight_checks_all_frozen_bindings_without_dispatch(tmp_path, monkeypatch):
    rows = _sealed_identity_rows(pair_indices=authoritative_query_pair_indices(), host="codex_windows_desktop", arm="A")
    entry, root, rows, inputs, method, bindings, artifact, config_path = _formal_preflight_bundle(
        tmp_path, rows=rows, stamp=IDENTITY_MAP_SHA256
    )
    ready = SimpleNamespace(formal_execution_allowed=True, details={"operations": host_query_operation_map(rows), "method_path": str(root / method["path"])})
    monkeypatch.setattr(entry, "verify_formal_run_config", lambda _: ready)
    monkeypatch.setattr(entry, "_execute_codex_condition", lambda *_: pytest.fail("preflight dispatched a host"))
    result = entry.run_host_queries(formal_config_path=config_path, output_root=root / "TEST-output")
    assert result["query_conditions"] == 80 and result["independent_query_pairs"] == 40
    assert result["status"] == "PREFLIGHT_ONLY" and not (root / "TEST-output").exists()
    bindings[rows[1]["unit_id"]] = bindings[rows[0]["unit_id"]]
    binding_ref = artifact("bindings.json", json.dumps(bindings).encode())
    config_path.write_text(json.dumps({
        "host_query_inputs": inputs, "host_query_bindings": binding_ref, "identity_map_sha256": IDENTITY_MAP_SHA256,
    }))
    with pytest.raises(HostQueryError, match="storage_reuse"):
        entry.run_host_queries(formal_config_path=config_path, output_root=root / "TEST-output")


def test_formal_preflight_rejects_missing_stamp_without_r_before_dispatch(tmp_path, monkeypatch):
    dispatched = []
    entry, root, _rows_used, _inputs, method, _bindings, _artifact, config_path = _formal_preflight_bundle(
        tmp_path, rows=_rows(), stamp=None
    )
    ready = SimpleNamespace(formal_execution_allowed=True, details={"operations": host_query_operation_map(_rows()), "method_path": str(root / method["path"])})
    monkeypatch.setattr(entry, "verify_formal_run_config", lambda _: ready)
    monkeypatch.setattr(entry, "_execute_codex_condition", lambda *args, **kwargs: dispatched.append((args, kwargs)) or pytest.fail("unbound formal preflight dispatched"))
    with pytest.raises(HostQueryError, match="identity_map_sha256_required"):
        entry.run_host_queries(formal_config_path=config_path, output_root=root / "TEST-output")
    assert dispatched == []
    assert not (root / "TEST-output").exists()


def test_import_worker_timeout_is_retryable_hard_crash_is_not():
    unfinished = {"status": "TIMEOUT", "states": {"done": 5, "leased": 1}}
    assert _import_worker_retryable({"returncode": 124, "error": None}, unfinished) is True
    assert _import_worker_retryable({"returncode": None, "error": "timeout"}, unfinished) is True
    assert _import_worker_retryable({"returncode": 0, "error": None}, unfinished) is True
    assert _import_worker_retryable({"returncode": 1, "error": None}, unfinished) is False
    assert _import_worker_retryable({"returncode": 124, "error": None}, {"status": "QUIESCENT"}) is False
