"""Execute frozen paired host queries through owned native sessions.

Reads the blind planner's existing host_query rows at execution time. No gold
reader or scorer is present. Each condition needs its own frozen host binding;
the existing p18_host_arm_binding preparer supplies actual launch files.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from p18_codex_appserver_transport import ARM_HOOK_POLICIES, CodexAppServerTransport, CodexTransportConfig
from p18_codex_budget import CodexSubmissionBudget
from p18_formal_evidence import verify_formal_run_config
from p18_formal_runner import CodexLedgerBudgetAdapter
from p18_journey_host_bridge import CodexAppServerSessionControl, CodexJourneyHostBridge, JourneySourceRefsProvider
from p18_run_journey import _candidate_import_matches, attachment_path_input
from p18_core_execution import _record_manifest
from p18_history_loader import history_event_dtos, load_raw_history


class HostQueryError(ValueError):
    pass


_UNIT = re.compile(r"(codex_windows_desktop|codex_windows_appserver_native_hooks_v2|hermes_a2a|hermes_cli_local_input_v1)-([ABCD])-query-(\d{2})-c([12])\Z")
_SEALED_PAIR_EVENT = re.compile(r"^R(\d{3})[AB](?:-|\Z)")
_METHOD = "codex_windows_appserver_native_hooks_v2"
_ROLES = {"human_direct":"user", "assistant_visible":"assistant", "tool_observation":"tool", "external_document":"document"}


_BASELINE_IMPORT_SCRIPT = r'''
import hashlib, importlib, importlib.util, json, os, pathlib, socket, sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
def blocked(*args, **kwargs):
    raise RuntimeError("TEST raw import forbids network")
socket.create_connection = blocked
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
# This child imports raw history only. Keep the hosted embedder unavailable
# here so the legacy provider does not retry the intentionally blocked socket.
# The actual host retains its credential and normal vector configuration.
os.environ.pop("SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY", None)
os.environ.pop("SCOPE_RECALL_TEST_B_EMBEDDING_TOKEN", None)
os.environ["HERMES_HOME"] = payload["home"]
sys.path.insert(0, payload["hermes_source"])
# Preserve the literal source first. The frozen B renderer truncates each
# item; sorting metadata ahead of the text would consume its whole allowance
# on evaluator bookkeeping instead of the identical offered source.
entries = [json.dumps({"text":item["text"], **{key:value for key,value in item.items() if key != "text"}},
                      ensure_ascii=False) for item in payload["envelopes"]]
result = {"arm_id":payload["arm_id"], "input_sha256":hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest(),
          "records_offered":len(entries), "model_calls":0, "network_disabled":True, "writes":[]}
if payload["arm_id"] == "B":
    result["embedding_deferred_to_host"] = True
if payload["arm_id"] == "A":
    from tools.memory_tool_store import MemoryStore
    store = MemoryStore(memory_char_limit=payload["memory_char_limit"], user_char_limit=payload["user_char_limit"])
    store.load_from_disk()
    if store.memory_entries or store.user_entries:
        raise RuntimeError("native baseline import requires empty memory")
    result["writes"] = [store.add("memory", entry) for entry in entries]
    fresh = MemoryStore(memory_char_limit=payload["memory_char_limit"], user_char_limit=payload["user_char_limit"])
    fresh.load_from_disk()
    result.update(api="MemoryStore.load_from_disk/add", accepted_entries=fresh.memory_entries,
                  stored_successes=sum(bool(item.get("success")) for item in result["writes"]),
                  native_memory_char_limit=payload["memory_char_limit"], flushed=True)
else:
    archive = pathlib.Path(payload["baseline_plugin"])
    spec = importlib.util.spec_from_file_location("scope_recall", archive/"__init__.py", submodule_search_locations=[str(archive)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["scope_recall"] = module
    spec.loader.exec_module(module)
    provider = importlib.import_module("scope_recall.provider").ScopeRecallMemoryProvider()
    # Raw import must not create a sticky local-hash fallback generation.
    # Temporarily defer vector initialization through the public config file,
    # restoring its exact bytes before the separately metered drain starts.
    vector_config = pathlib.Path(payload["home"])/"scope-recall/config.json"
    vector_config.parent.mkdir(parents=True, exist_ok=True)
    previous_config = vector_config.read_bytes() if vector_config.exists() else None
    offline_config = json.loads(previous_config) if previous_config is not None else {}
    offline_config.setdefault("vector", {})["enabled"] = False
    vector_config.write_text(json.dumps(offline_config), encoding="utf-8")
    try:
        # Match frozen classic CLI initialize kwargs. In particular omit
        # user_id so 578b derives the same home-local opaque principal itself.
        provider.initialize(payload["session_id"], hermes_home=payload["home"], platform="cli",
                            agent_context="primary", agent_identity="default", agent_workspace="hermes")
        for envelope, entry in zip(payload["envelopes"], entries):
            result["writes"].append(provider.store_now(content=entry, source=envelope["source_type"],
                target="project", session_id=payload["session_id"], semantic_merge=False,
                metadata={"dataset_id":envelope["dataset_id"], "source_event_key":envelope["source_event_key"],
                          "speaker_role":envelope["speaker_role"], "occurred_at":envelope["occurred_at"],
                          "transport_origin":"synthetic_agent_relay", "import_origin":"imported"}))
        result.update(api="578b initialize/store_now/flush/shutdown", flushed=bool(provider.flush(timeout=5.0)),
                      stored_successes=sum(bool(item[1]) for item in result["writes"]))
    finally:
        try:
            provider.shutdown(timeout=5.0)
        finally:
            if previous_config is None:
                vector_config.unlink(missing_ok=True)
            else:
                vector_config.write_bytes(previous_config)
pathlib.Path(payload["result_path"]).write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
'''


def import_baseline_condition(row, binding, *, process_owner, output):
    """Actual A/B public write APIs, same literal inputs and host-local scope."""
    arm = binding["arm_id"]
    if binding["host_id"] != "hermes_cli_local_input_v1" or arm not in {"A","B"}:
        raise HostQueryError("native_CLI_baseline_import_required")
    manifest = _record_manifest(row)
    history_event_dtos(manifest)
    home = Path(binding["roots"]["home_path"]).resolve()
    if arm == "B" and Path(binding["roots"]["database_path"]).exists():
        raise HostQueryError("legacy_baseline_import_database_must_be_new")
    config = _json(binding["roots"]["config_path"])
    envelopes = [{"dataset_id":manifest["dataset_id"], "source_event_key":event["event_id"],
        "source_type":event["source_type"], "speaker_role":event["speaker_role"],
        "occurred_at":event["occurred_at"], "text":event["text"],
        "transport_origin":"synthetic_agent_relay", "import_origin":"imported"}
        for event in row["source_records"]]
    output = Path(output).resolve()
    result_path = output/"baseline-import-result.json"
    input_path = output/"baseline-import-input.json"
    payload = {"arm_id":arm,"home":str(home),"hermes_source":binding["fixed_host"]["source"]["path"],
        "session_id":"TEST-import-"+row["unit_id"],"envelopes":envelopes,"result_path":str(result_path),
        "baseline_plugin":(binding.get("loader",{}).get("plugin_directory") or {}).get("path"),
        "memory_char_limit":config.get("memory",{}).get("memory_char_limit",2200),
        "user_char_limit":config.get("memory",{}).get("user_char_limit",1375)}
    with input_path.open("xb") as handle:
        handle.write(json.dumps(payload,ensure_ascii=False).encode())
    process = process_owner.run_process([binding["fixed_host"]["runtime_python_path"],"-I","-B","-c",_BASELINE_IMPORT_SCRIPT,str(input_path)],
        output,label="baseline-import",timeout_seconds=30)
    if process["returncode"] != 0 or process["error"] or not result_path.is_file():
        raise HostQueryError("actual_baseline_public_import_failed")
    observed = _json(result_path)
    if observed.get("input_sha256") != _sha(input_path.read_bytes()) or observed.get("records_offered") != len(envelopes):
        raise HostQueryError("baseline_import_receipt_not_bound")
    complete = observed.get("stored_successes") == len(envelopes) and observed.get("flushed") is True
    return {"status":"IMPORTED_BASELINE_HISTORY" if complete else "IMPORTED_WITH_GAPS",
        "api":observed["api"],"manifest_sha256":manifest["manifest_sha256"],"records_offered":len(envelopes),
        "stored_successes":observed["stored_successes"],"source_capture_refs":[],"host_L1_capture_proven":False,
        "original_roles_preserved":True,"transport_origin":"synthetic_agent_relay","main_model_calls":0,
        "input_artifact":{"path":str(input_path),"sha256":_sha(input_path.read_bytes())},
        "result_artifact":{"path":str(result_path),"sha256":_sha(result_path.read_bytes())},
        "process":{key:process[key] for key in ("pid","returncode","error")}}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(path):
    return json.loads(Path(path).read_bytes())


def _artifact(ref, root):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise HostQueryError("frozen_artifact_required")
    relative = Path(ref["path"])
    path = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root) or not path.is_file():
        raise HostQueryError("frozen_artifact_path_invalid")
    if _sha(path.read_bytes()) != ref["sha256"]:
        raise HostQueryError("frozen_artifact_hash_mismatch")
    return path


def _dataset_pair_indices_from_query_row(row):
    """Collect sealed public pair indices from offered source event ids."""
    indices = set()
    records = row.get("source_records")
    if not isinstance(records, list):
        return indices
    for event in records:
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            continue
        match = _SEALED_PAIR_EVENT.match(event_id)
        if match:
            indices.add(int(match.group(1)))
    return indices


def _dataset_pair_index_from_query_row(row):
    """Read the sealed public event-id pair index when the row carries one.

    ``R006A-E1`` means public index 6. Mixed ``R001`` plus ``R002`` is an
    identity error, not a PUBLIC fixture. Synthetic PUBLIC-* rows return None.
    """
    indices = _dataset_pair_indices_from_query_row(row)
    if len(indices) > 1:
        raise HostQueryError("query_identity_ambiguous:" + ",".join(f"P{index:03d}" for index in sorted(indices)))
    if len(indices) == 1:
        return next(iter(indices))
    return None


def assert_host_query_identity_alignment(rows, *, require_identity_binding=False):
    """Reject sealed rows whose query ordinal is not the frozen identity pair.

    Mixed or missing R-prefix bindings are rejected when the file already
    carries any sealed case binding, or when the official entry requires one.
    Synthetic PUBLIC-* fixtures with no R-prefix stay compatible.
    """
    from p18_score_report import authoritative_query_pair_indices

    expected = authoritative_query_pair_indices()
    mismatches = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "host_query":
            continue
        match = _UNIT.fullmatch(str(row.get("unit_id", "")))
        if match is None:
            continue
        inferred = _dataset_pair_index_from_query_row(row)
        if inferred is None:
            if require_identity_binding:
                raise HostQueryError(f"query_identity_binding_missing:{row.get('unit_id')}")
            continue
        query_n = int(match[3])
        wanted = expected[query_n - 1]
        if inferred != wanted:
            mismatches.append(
                f"query-{query_n:02d}:c{match[4]}:dataset_P{inferred:03d}!=TEST-pair-{wanted:03d}"
            )
    if mismatches:
        raise HostQueryError("query_identity_mismatch:" + ";".join(mismatches[:6]))


def query_units(rows, *, require_full=True, require_identity_binding=None):
    """Allowlist the planner projection; never put its control fields in input.

    Offline PUBLIC fixtures may omit R-prefix case bindings. The formal
    ``run_host_queries`` entry always passes ``require_identity_binding=True``.
    """
    units = []
    seen = set()
    host_arms = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HostQueryError("planner_row_object_required")
        if row.get("kind") != "host_query":
            continue  # The planner file also contains the separate J graph.
        if set(row) != {"unit_id", "kind", "ordinal", "source_sequence", "source_records", "model_input"}:
            raise HostQueryError("planner_host_query_schema_invalid")
        match = _UNIT.fullmatch(str(row["unit_id"]))
        if not match or not 1 <= int(match[3]) <= 40 or row["unit_id"] in seen:
            raise HostQueryError("paired_unit_id_invalid_or_duplicate")
        if row["source_sequence"] != "imported_history_then_new_session_query":
            raise HostQueryError("source_sequence_invalid")
        natural = row["model_input"]
        if not isinstance(natural, dict) or set(natural) != {"history", "query"}:
            raise HostQueryError("natural_input_required")
        if (not isinstance(natural["query"], dict) or set(natural["query"]) != {"text"}
                or not isinstance(natural["query"]["text"], str) or not natural["query"]["text"].strip()):
            raise HostQueryError("natural_query_required")
        if not isinstance(natural["history"], list) or not natural["history"]:
            raise HostQueryError("natural_history_required")
        for event in natural["history"]:
            if (not isinstance(event, dict) or set(event) not in ({"source_type", "speaker_role", "text"}, {"source_type", "speaker_role", "text", "occurred_at"})
                    or event.get("source_type") not in _ROLES or event.get("speaker_role") != _ROLES[event["source_type"]]
                    or not isinstance(event["text"], str) or not event["text"].strip()
                    or ("occurred_at" in event and not isinstance(event["occurred_at"], str))):
                raise HostQueryError("natural_source_schema_invalid")
        from p18_sealed_run_plan import _project_model_input
        if _project_model_input({"history":row["source_records"], "query":natural["query"]}) != natural:
            raise HostQueryError("source_records_do_not_match_natural_projection")
        history_event_dtos(_record_manifest(row))
        seen.add(row["unit_id"])
        host_arms.add((match[1], match[2]))
        units.append(row)
    if not units or len(host_arms) != 1:
        raise HostQueryError("one_host_arm_per_query_file_required")
    pairs = {(int(_UNIT.fullmatch(row["unit_id"])[3]), int(_UNIT.fullmatch(row["unit_id"])[4])) for row in units}
    if require_full and pairs != {(pair, condition) for pair in range(1, 41) for condition in (1, 2)}:
        raise HostQueryError("exact_40_pairs_80_conditions_required")
    if require_identity_binding is None:
        require_identity_binding = any(_dataset_pair_indices_from_query_row(row) for row in units)
    assert_host_query_identity_alignment(units, require_identity_binding=require_identity_binding)
    return units


def host_query_operation_map(rows, *, execution_method=None):
    """Only real host queries are primary submissions; import has no main call."""
    result = {}
    for row in query_units(rows):
        unit_id = row["unit_id"]
        match = _UNIT.fullmatch(unit_id)
        host_id = _METHOD if match[1].startswith("codex_") else match[1]
        if execution_method is not None:
            if execution_method == "hermes_cli_local_input_v1" and match[1].startswith("hermes_"):
                host_id = execution_method
            elif execution_method == _METHOD and match[1].startswith("codex_"):
                host_id = execution_method
            else:
                raise HostQueryError("explicit_query_method_mapping_invalid")
        result[unit_id] = {"host_id":host_id, "arm_id":match[2], "request_id":unit_id+"-request",
            "unit":{"kind":"query", "query_id":unit_id, "journey_id":None, "round_id":None}}
    return result


def verify_condition_storage_identity(binding):
    """Read the actual empty database with the host identity, without auxiliary setup."""
    from scope_recall.runtime.instance import RuntimeInstanceConfig
    from scope_recall.core import CoreConfig, MemoryCore
    config = RuntimeInstanceConfig.from_mapping(_json(binding["roots"]["runtime_config_path"]))
    if binding.get("host_id") == "hermes_cli_local_input_v1":
        from scope_recall.adapters.hermes.identity import bind_hermes_identity
        identity = bind_hermes_identity("TEST-preflight", hermes_home=binding["roots"]["home_path"],
            platform="cli", agent_identity="default", agent_workspace="hermes", agent_context="primary")
        context = identity.trusted_context(actor_origin="imported")
    else:
        from scope_recall.adapters.codex.config import load_codex_config
        from scope_recall.adapters.codex.identity import resolve_runtime_audience, trusted_context
        installation = load_codex_config(binding["roots"]["installation_config_path"])
        audience = resolve_runtime_audience(installation, binding["launch_contract"]["working_directory"])
        context = trusted_context(installation, audience, session_id="TEST-preflight")
    return MemoryCore(CoreConfig(config.binding)).status(context)


def import_condition_history(row, binding):
    """Use existing real raw import; never convert synthetic roles to capture."""
    manifest = _record_manifest(row)
    manifest_sha = manifest["manifest_sha256"]
    if binding.get('native_source_import',{}).get('method') == 'native-A2-reviewed-v2':
        from p18_native_a2_gate import verify_source
        return verify_source(row,binding,manifest_sha)
    if binding['arm_id'] == 'A' and binding.get('native_source_import'):
        from p18_native_a_import import verify_native_import
        return verify_native_import(row, binding, manifest_sha)
    if binding["arm_id"] == "D":
        ref = binding.get("loader", {}).get("archive")
        if not isinstance(ref,dict):
            raise HostQueryError("D_actual_hook_archive_missing")
        path=Path(ref["path"]).resolve()
        if _sha(path.read_bytes()) != ref["sha256"]:
            raise HostQueryError("D_actual_hook_archive_changed")
        rows=[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(rows)!=1 or rows[0].get("history") != row["source_records"]:
            raise HostQueryError("D_archive_must_contain_same_condition_raw_records")
        return {"status":"IMPORTED_RAW_ARCHIVE", "manifest_sha256":manifest_sha, "archive":ref,
                "source_capture_refs":[], "host_L1_capture_proven":False, "main_model_calls":0}
    if binding["arm_id"] != "C":
        return {"status":"UNSUPPORTED", "reason":"no_verified_native_raw_import_entry", "main_model_calls":0}
    from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance
    from scope_recall.adapters.codex.config import load_codex_config
    from scope_recall.adapters.codex.identity import resolve_runtime_audience, trusted_context
    config=RuntimeInstanceConfig.from_mapping(_json(binding["roots"]["runtime_config_path"]))
    runtime=build_runtime_instance(config)
    try:
        if binding.get("host_id") == "hermes_cli_local_input_v1":
            from scope_recall.adapters.hermes.identity import bind_hermes_identity
            identity=bind_hermes_identity("TEST-import-"+row["unit_id"], hermes_home=binding["roots"]["home_path"],
                platform="cli", agent_identity="default", agent_workspace="hermes", agent_context="primary")
            context=identity.trusted_context(actor_origin="imported")
            scope_id=identity.runtime_audience.capture_scope_id
        else:
            installation=load_codex_config(binding["roots"]["installation_config_path"])
            audience=resolve_runtime_audience(installation,binding["launch_contract"]["working_directory"])
            context=trusted_context(installation,audience,session_id="TEST-import-"+row["unit_id"])
            scope_id=audience.capture_scope_id
        if scope_id is None or context.binding != config.binding:
            raise HostQueryError("import_must_use_actual_host_binding_and_audience")
        result=load_raw_history(runtime.core,context,manifest,scope_id=scope_id,arm_id="C")
        refs=[f"{ref}@{event['source_revision']}" for ref,event in zip(result.source_refs,manifest["records"],strict=True)]
        return {"status":"IMPORTED_RAW_HISTORY", "import_receipt":asdict(result), "manifest_sha256":manifest_sha,
                "source_capture_refs":refs, "import_session_id":context.session_id,
                "transport_origin":"synthetic_agent_relay", "stored_origin":"imported",
                "original_roles_preserved":True,"host_L1_capture_proven":False,"main_model_calls":0}
    finally:
        runtime.close()


def _wait_automatic_work(database, *, timeout_seconds=180):
    """Observe the real hook-launched worker; never call synchronous drain."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=.25) as db:
            rows = dict(db.execute("SELECT state,count(*) FROM work_items GROUP BY state").fetchall())
        if not (rows.get("pending",0)+rows.get("leased",0)):
            return {"status":"QUIESCENT", "states":rows, "normal_worker_invoked_by_harness":False}
        if time.monotonic() >= deadline:
            return {"status":"TIMEOUT", "states":rows, "normal_worker_invoked_by_harness":False}
        time.sleep(.25)


def _wait_recoverable_backoff(database, *, timeout_seconds=8):
    """Wait out a pending consolidate retry backoff so the next owned drain can claim it."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=.25) as db:
            rows = dict(db.execute("SELECT state,count(*) FROM work_items GROUP BY state").fetchall())
            ready = db.execute(
                "SELECT count(*) FROM work_items WHERE state='pending' AND available_at<=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",),
            ).fetchone()[0]
        if not (rows.get("pending", 0) + rows.get("leased", 0)) or ready:
            return
        time.sleep(0.25)


def _completed(result, operation_id):
    path = Path(result.get("formal_evidence_path", ""))
    if not path.is_file():
        raise HostQueryError("real_formal_operation_receipt_required")
    record = _json(path)
    if record.get("status") != "COMPLETED" or record.get("operation_id") != operation_id:
        raise HostQueryError("formal_operation_did_not_complete")
    session_id = record.get("ids", {}).get("session_id")
    if not session_id or session_id != result.get("session_id"):
        raise HostQueryError("actual_session_association_required")
    return {"path":str(path), "sha256":_sha(path.read_bytes())}


def _prepare_codex_worker_temp(binding, environment):
    """Native Lance needs actual owned TEMP/TMP directories, not just env values."""
    state = Path(binding['roots']['state_path']).resolve()
    root = Path(binding['roots']['binding_root']).resolve()
    if not state.is_relative_to(root):
        raise HostQueryError('worker_state_outside_owned_binding')
    directories = {}
    for name in ('TEMP', 'TMP'):
        raw = environment.get(name)
        path = Path(raw).resolve() if raw else None
        if path is None or not path.is_relative_to(state) or path == state:
            raise HostQueryError('worker_temp_outside_owned_state')
        path.mkdir(parents=True, exist_ok=True)
        directories[name] = str(path)
    return directories


def _stdout_ref(value):
    if isinstance(value, Path) and value.is_file():
        return {"path": str(value), "sha256": _sha(value.read_bytes())}
    if isinstance(value, dict) and value.get("path"):
        return value
    return None


_IMPORT_WORKER_TIMEOUT_S = 180


def _import_worker_retryable(worker, automatic_work):
    """Retry a bounded extra drain after timeout or unfinished work, not a hard crash."""
    timed_out = worker.get("error") == "timeout" or worker.get("returncode") == 124
    unfinished = automatic_work.get("status") != "QUIESCENT" or _worker_stdout_incomplete(worker)
    if not unfinished:
        return False
    if worker.get("returncode") == 0 and not worker.get("error"):
        return True
    return bool(timed_out)


def _worker_stdout_incomplete(worker):
    """Treat degraded/failed embed-consolidate as incomplete. Mocks without stdout pass."""
    stdout = worker.get("stdout")
    if stdout is None:
        return False
    if isinstance(stdout, Path):
        path = stdout
    elif isinstance(stdout, dict) and stdout.get("path"):
        path = Path(stdout["path"])
    else:
        return False
    if not path.is_file():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    failed = payload.get("failed")
    if isinstance(failed, int) and failed > 0:
        return True
    return str(payload.get("status") or "").casefold() == "failed"


def _run_codex_import_worker(binding, output, label="import-worker"):
    """Bounded public candidate worker for imported fixture preparation."""
    import os
    import subprocess
    from scope_recall.runtime.worker_watchdog import _OwnedWindowsJob
    python_ref = binding["loader"]["candidate_python"]
    python = Path(python_ref["path"])
    if _sha(python.read_bytes()) != python_ref["sha256"]:
        raise HostQueryError("candidate_worker_interpreter_hash_mismatch")
    config = Path(binding["roots"]["runtime_config_path"])
    command = [str(python), "-I", "-B", "-m", "scope_recall.runtime.worker_watchdog",
               "--config", str(config), "--python", str(python)]
    environment = {**os.environ, **_json(binding["roots"]["environment_path"]),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    temp_directories = _prepare_codex_worker_temp(binding, environment)
    stdout, stderr = Path(output)/f"{label}.stdout", Path(output)/f"{label}.stderr"
    process = None
    job = _OwnedWindowsJob()
    error = None
    try:
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(command, cwd=binding["launch_contract"]["working_directory"],
                env=environment, stdout=out, stderr=err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            job.assign(process)
            try:
                process.wait(timeout=_IMPORT_WORKER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                error = "timeout"
                job.close()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    finally:
        job.close()
        environment.clear()
    return {"pid":process.pid,"returncode":process.returncode,"error":error,
        "entry":"scope_recall.runtime.worker_watchdog","owned_process":True,"timeout_seconds":_IMPORT_WORKER_TIMEOUT_S,
        "purpose":"imported_history_L2_preparation","host_lifecycle_proven":False,"main_model_calls":0,
        "owned_temp_directories":temp_directories,
        "runtime_config_sha256":_sha(config.read_bytes()),
        "stdout":{"path":str(stdout),"sha256":_sha(stdout.read_bytes())},
        "stderr":{"path":str(stderr),"sha256":_sha(stderr.read_bytes())}}


def _execute_codex_condition(row, binding, readiness, config_path, output):
    """Concrete owned Codex dispatch. This function never accepts host callbacks."""
    unit_id = row["unit_id"]
    workspace = Path(binding["launch_contract"]["working_directory"]).resolve()
    config_root = config_path.parent
    ledger = Path(readiness.details["ledger_path"])
    arm = binding["arm_id"]
    output.mkdir(parents=True, exist_ok=False)
    formal = output / "formal"
    formal.mkdir()
    database = Path(binding["roots"]["database_path"]).resolve() if arm == "C" else None
    budget = CodexLedgerBudgetAdapter(CodexSubmissionBudget(ledger), operation_root=formal/"operations", config_root=config_root)
    transport = CodexAppServerTransport(CodexTransportConfig(
        codex_exe=Path(binding["fixed_host"]["executable_path"]), cwd=workspace, arm_id=arm,
        expected_hooks_policy=ARM_HOOK_POLICIES[arm], formal_evaluation=True, formal_config_path=config_path,
        environment=__import__("p18_codex_appserver_transport").codex_binding_environment(binding)), budget=budget)
    control = CodexAppServerSessionControl(transport, database.parent if database else None)
    bridge = CodexJourneyHostBridge(transport=transport, session_control=control, formal_config_path=config_path,
        output_root=formal, source_refs_provider=JourneySourceRefsProvider(database), attachment_encoder=attachment_path_input)
    receipt = {"unit_id":unit_id,"status":"FAILED","semantic_pass":False,"source_capture_refs":[]}
    native_budget = None
    native_operation = None
    native_reserved = False
    try:
        native_import = arm == 'A' and bool(binding.get('native_source_import'))
        if not native_import:
            bridge.resume()
        imported=import_condition_history(row,binding)
        receipt["import"]=imported
        receipt["source_capture_refs"]=imported.get("source_capture_refs",[])
        if imported["status"] == "UNSUPPORTED":
            receipt["status"]="UNSUPPORTED_IMPORT"
            return receipt
        if imported['status'] == 'SOURCE_PREPARATION_FAILED':
            receipt['status']='SOURCE_PREPARATION_FAILED'
            receipt['source_load_complete']=False
            return receipt
        if imported['status'] == 'IMPORTED_NATIVE_A2':
            from p18_native_a2_gate import require_query_ready
            receipt['native_A2_ready']=require_query_ready(binding)
        if imported['status'] in {'IMPORTED_NATIVE_RAW_HISTORY','IMPORTED_NATIVE_A2'}:
            from p18_native_a_import import native_source_state, assert_native_source_idle
            from p18_codex_budget import CodexBudgetPolicy
            before = native_source_state(binding, imported['import_session_id'])
            receipt['native_before'] = before
            assert_native_source_idle(binding, before)
            native_budget = CodexSubmissionBudget(ledger, policy=CodexBudgetPolicy(
                batch='P18_NATIVE_A_AUX', call_cap=100000, input_cap=10**12, output_cap=10**12,
                reserved_input=131072, reserved_output=32768))
            native_operation = 'native-A-query-window-'+_sha(str(output.resolve()).encode('utf-8'))[:16]+'-'+unit_id
            native_budget.reserve(native_operation, json.dumps({'home':str(binding['roots']['home_path']),
                'source_receipt':imported['source_receipt'], 'kind':'native_aux_background_window'}, sort_keys=True).encode(),
                model='native-memory-background-unobserved', audit_source='native-A-query-background-window')
            native_reserved = True
        if native_import:
            bridge.resume()
        # Imported fixture preparation is not a host L1/lifecycle assertion.
        # Empty thread/start does not guarantee SessionStart worker execution.
        if database:
            receipt["import_worker"] = _run_codex_import_worker(binding, output)
            receipt["automatic_work"] = _wait_automatic_work(database, timeout_seconds=5)
            receipt["automatic_work"]["normal_worker_invoked_by_harness"] = True
            extra_drains = []
            while (_import_worker_retryable(receipt["import_worker"], receipt["automatic_work"])
                   and len(extra_drains) < 2):
                extra = _run_codex_import_worker(binding, output, label=f"import-worker-extra-{len(extra_drains)+1}")
                extra_drains.append({key: extra[key] for key in ("pid", "returncode", "error")})
                receipt["import_worker"] = extra
                receipt["automatic_work"] = _wait_automatic_work(database, timeout_seconds=5)
                receipt["automatic_work"]["normal_worker_invoked_by_harness"] = True
            if extra_drains:
                receipt["import_worker_extra"] = extra_drains
            if (receipt["import_worker"]["returncode"] != 0 or receipt["import_worker"]["error"]
                    or receipt['automatic_work']['status'] != 'QUIESCENT'
                    or _worker_stdout_incomplete(receipt["import_worker"])):
                receipt['source_load_complete'] = False
                raise HostQueryError('import_worker_incomplete')
        query_session = bridge.new_session("query")["session_id"]
        receipt['source_load_complete'] = imported['status'] != 'IMPORTED_WITH_GAPS'
        if not query_session or query_session == imported.get("import_session_id"):
            raise HostQueryError("query_requires_distinct_native_thread")
        if native_budget is not None:
            deadline = time.monotonic()+120
            while True:
                observed = native_source_state(binding, imported['import_session_id'])
                receipt['native_pre_query'] = observed
                if observed['native_generation_verified'] or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            if not observed['native_generation_verified']:
                receipt['status'] = 'NATIVE_GENERATION_UNVERIFIED'
                receipt['source_load_complete'] = True
                return receipt
        result = bridge.execute_turn(operation_id=unit_id,session_alias="query",session_id=query_session,
            model_input={"query":row["model_input"]["query"]["text"],"attachments":[]},workspace=workspace)
        if arm == "D":
            capture_path = Path(binding["loader"]["capture_archive"]["path"])
            query_text = row["model_input"]["query"]["text"]
            captures = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines() if line.strip()] if capture_path.is_file() else []
            matching = [item for item in captures if str(item.get("event", "")).replace("_", "").lower() == "userpromptsubmit"
                        and item.get("history") == [{"role":"user", "text":query_text}]]
            observed_transport = _json(formal/"operations"/unit_id/"transport-receipt.json")
            observed_hooks = [event for event in observed_transport.get("hook_events", [])
                if event.get("phase") == "completed" and event.get("eventName") == "userPromptSubmit"
                and event.get("status") == "completed" and event.get("threadId") == query_session]
            if not matching or not observed_hooks:
                raise HostQueryError("native_simple_search_callback_not_observed")
            receipt["simple_search_callback"] = {"status":"OBSERVED", "capture_path":str(capture_path),
                "capture_sha256":_sha(capture_path.read_bytes()),"matching_query_captures":len(matching),
                "completed_native_hook_events":len(observed_hooks), "query_text_unchanged":True}
        receipt.update({"query_evidence":_completed(result,unit_id),"query_result":result,
                        "query_session_id":query_session,"status":"EXECUTED",
                        "scoring":"independent_scorer_required"})
    except Exception as exc:
        receipt.update({"error_type":type(exc).__name__,"error":str(exc)[:256]})
    finally:
        bridge.close()
        if native_reserved:
            receipt['native_aux_budget'] = native_budget.finish(native_operation, 'query_background_window_closed', None)
            receipt['native_background_model_calls'] = None
            receipt['native_background_usage'] = 'unknown_reserved'
            try:
                receipt['native_after'] = native_source_state(binding, imported['import_session_id'])
            except Exception as exc:
                receipt['native_state_observation_error'] = type(exc).__name__
        if database is not None:
            try:
                snapshot=output/"observed-memory.sqlite3"
                with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=2) as source:
                    with sqlite3.connect(snapshot) as target:
                        source.backup(target)
                receipt["observed_memory_snapshot"]={"path":str(snapshot),"sha256":_sha(snapshot.read_bytes())}
            except Exception as exc:
                receipt["storage_evidence_gap"]={"error_type":type(exc).__name__}
        (output/"condition-receipt.json").write_bytes(json.dumps(receipt,ensure_ascii=False,indent=2).encode("utf-8"))
    return receipt


def _execute_cli_condition(row, binding, readiness, config_path, output):
    from p18_run_journey import cli_host
    unit_id = row["unit_id"]
    output.mkdir(parents=True, exist_ok=False)
    formal = output/"formal"
    formal.mkdir()
    bridge = cli_host(binding, config_path, formal, readiness)
    database = Path(binding["roots"]["database_path"]).resolve() if binding["arm_id"] in {"B","C"} else None
    receipt = {"unit_id":unit_id,"status":"FAILED","semantic_pass":False,"source_capture_refs":[]}
    try:
        bridge.resume()
        imported = (import_baseline_condition(row, binding, process_owner=bridge.transport.owner, output=output)
                    if binding["arm_id"] in {"A","B"} else import_condition_history(row, binding))
        receipt["import"] = imported
        receipt["source_load_complete"] = imported["status"] != "IMPORTED_WITH_GAPS"
        receipt["source_capture_refs"] = imported.get("source_capture_refs", [])
        if binding["arm_id"] == "B" and receipt["source_load_complete"]:
            from p18_legacy_embedding_meter import drain_legacy_embeddings
            receipt["source_load_complete"] = False
            receipt["baseline_embedding_drain"] = drain_legacy_embeddings(binding, bridge.transport.owner, output)
            receipt["source_load_complete"] = True
        if imported["status"] == "UNSUPPORTED":
            receipt["status"] = "UNSUPPORTED_IMPORT"
            return receipt
        if database and binding["arm_id"] == "C":
            # Raw import is its own fixture preparation, not host L1 capture.
            # Reuse the real bounded worker; no manual claims/synchronous drain.
            receipt["source_load_complete"] = False
            owner = bridge.transport.owner
            worker = owner.run_process([str(bridge.transport.python), "-B", "-m", "scope_recall.runtime.worker_watchdog",
                "--config", binding["roots"]["runtime_config_path"], "--python", str(bridge.transport.python)],
                output, label="import-worker", timeout_seconds=_IMPORT_WORKER_TIMEOUT_S)
            receipt["import_worker"] = {key:worker[key] for key in ("pid","returncode","error")}
            receipt["import_worker"].update(entry="scope_recall.runtime.worker_watchdog", owned_process=True,
                timeout_seconds=_IMPORT_WORKER_TIMEOUT_S, purpose="imported_history_L2_preparation", host_lifecycle_proven=False,
                main_model_calls=0, runtime_config_sha256=_sha(Path(binding["roots"]["runtime_config_path"]).read_bytes()),
                stdout=_stdout_ref(worker.get("stdout")))
            receipt["automatic_work"] = _wait_automatic_work(database, timeout_seconds=5)
            receipt["automatic_work"]["normal_worker_invoked_by_harness"] = True
            extra_drains = []
            while (_import_worker_retryable(worker, receipt["automatic_work"])
                   and len(extra_drains) < 2):
                _wait_recoverable_backoff(database, timeout_seconds=8)
                extra = owner.run_process(
                    [str(bridge.transport.python), "-B", "-m", "scope_recall.runtime.worker_watchdog",
                     "--config", binding["roots"]["runtime_config_path"], "--python", str(bridge.transport.python)],
                    output, label=f"import-worker-extra-{len(extra_drains)+1}", timeout_seconds=_IMPORT_WORKER_TIMEOUT_S)
                extra_drains.append({key: extra[key] for key in ("pid", "returncode", "error")})
                worker = extra
                receipt["import_worker"]["stdout"] = _stdout_ref(extra.get("stdout"))
                receipt["automatic_work"] = _wait_automatic_work(database, timeout_seconds=5)
                receipt["automatic_work"]["normal_worker_invoked_by_harness"] = True
            if extra_drains:
                receipt["import_worker_extra"] = extra_drains
            if (worker["returncode"] != 0 or worker["error"]
                    or receipt["automatic_work"]["status"] != "QUIESCENT"
                    or _worker_stdout_incomplete(worker)):
                raise HostQueryError("import_worker_incomplete")
            receipt["source_load_complete"] = True
        pending = bridge.new_session("query")
        if pending["session_id"] is not None:
            raise HostQueryError("CLI_fresh_session_must_be_observed_after_dispatch")
        result = bridge.execute_turn(operation_id=unit_id, session_alias="query", session_id=None,
            model_input={"query":row["model_input"]["query"]["text"],"attachments":[]},
            workspace=Path(binding["launch_contract"]["working_directory"]))
        receipt.update(query_evidence=_completed(result,unit_id), query_result=result,
                       query_session_id=result["session_id"],status="EXECUTED",scoring="independent_scorer_required")
    except Exception as exc:
        receipt.update(error_type=type(exc).__name__,error=str(exc)[:256])
    finally:
        bridge.close()
        if database is not None:
            try:
                snapshot=output/"observed-memory.sqlite3"
                with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=2) as source:
                    with sqlite3.connect(snapshot) as target:
                        source.backup(target)
                receipt["observed_memory_snapshot"]={"path":str(snapshot),"sha256":_sha(snapshot.read_bytes())}
            except Exception as exc:
                receipt["storage_evidence_gap"]={"error_type":type(exc).__name__}
        elif binding["arm_id"] == "A":
            native = Path(binding["roots"]["home_path"])/"memories"
            receipt["native_memory_snapshots"] = []
            for name in ("MEMORY.md","USER.md"):
                original = native/name
                if original.is_file():
                    snapshot = output/("observed-"+name)
                    snapshot.write_bytes(original.read_bytes())
                    receipt["native_memory_snapshots"].append({"path":str(snapshot),"sha256":_sha(snapshot.read_bytes())})
        (output/"condition-receipt.json").write_bytes(json.dumps(receipt,ensure_ascii=False,indent=2).encode())
    return receipt


def _condition_output(binding, config_root):
    if binding.get('native_source_import',{}).get('method') == 'native-A2-reviewed-v2':
        from p18_native_a2_gate import plan_for
        plan=plan_for(binding)
        output=Path(config_root).resolve()/'native-A2-query'/f"{plan['index']:02}"
        if Path(binding['native_source_import']['formal_query_output']).resolve()!=output:
            raise HostQueryError('A2_frozen_query_output_mismatch')
        return output
    return Path(binding['roots']['binding_root']).resolve()/'query-execution'


def _retained_condition(entry, config_root, binding, unit_id, *, allow_closed_failures=False):
    """Reuse only a hash-bound successful original condition, never replay it."""
    path = _artifact(entry, config_root)
    expected = _condition_output(binding, Path(config_root)) / 'condition-receipt.json'
    if path != expected:
        raise HostQueryError('retained_condition_binding_mismatch')
    receipt = _json(path)
    if (allow_closed_failures and receipt.get('unit_id') == unit_id
            and receipt.get('status') in {'FAILED', 'NATIVE_GENERATION_UNVERIFIED'}
            and receipt.get('semantic_pass') is False):
        return {'unit_id': unit_id, 'status': receipt['status'], 'receipt_path': str(path),
                'receipt_sha256': _sha(path.read_bytes()),
                'source_load_complete': receipt.get('source_load_complete'),
                'retained_without_reexecution': True, 'retained_failure': True,
                'semantic_pass': False}
    if (receipt.get('unit_id') != unit_id or receipt.get('status') != 'EXECUTED'
            or receipt.get('source_load_complete') is not True):
        raise HostQueryError('retained_condition_requires_completed_original')
    evidence = receipt.get('query_evidence')
    if not isinstance(evidence, dict):
        raise HostQueryError('retained_condition_query_evidence_required')
    evidence_path = Path(evidence.get('path', '')).resolve()
    if (not evidence_path.is_relative_to(expected.parent) or not evidence_path.is_file()
            or _sha(evidence_path.read_bytes()) != evidence.get('sha256')):
        raise HostQueryError('retained_condition_query_evidence_changed')
    return {'unit_id': unit_id, 'status': 'EXECUTED', 'receipt_path': str(path),
            'receipt_sha256': _sha(path.read_bytes()), 'source_load_complete': True,
            'retained_without_reexecution': True}


def _bounded_conditions(rows, execute, workers):
    """Stop scheduling after failure; preserve completion of already owned work."""
    results = {}
    pending_rows = iter(rows)
    halted = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = {}
        def submit_next():
            row = next(pending_rows, None)
            if row is not None:
                active[pool.submit(execute, row)] = row
        for _ in range(workers):
            submit_next()
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                row = active.pop(future)
                result = future.result()
                results[row['unit_id']] = result
                halted = halted or (result['status'] != 'EXECUTED'
                                    and result.get('continue_dispatch_after_failure') is not True)
            if not halted:
                for _ in done:
                    submit_next()
    for row in pending_rows:
        results[row['unit_id']] = {'unit_id': row['unit_id'],
            'status': 'NOT_ATTEMPTED_AFTER_EXECUTION_FAILURE', 'model_calls': 0, 'semantic_pass': False}
    return results


def _isolated_upstream_failure(receipt, output, config_root):
    """Recognize a recorded upstream timeout, never retry it or erase its grade."""
    if receipt.get('status') != 'FAILED' or receipt.get('error') != 'formal_operation_did_not_complete':
        return None
    transport_path = output / 'formal/operations' / receipt['unit_id'] / 'transport-receipt.json'
    if not transport_path.is_file():
        return None
    transport = _json(transport_path)
    if (transport.get('rpc_provenance', {}).get('turn_rpcs_dispatched') == 1
            and transport.get('turn', {}).get('started', {}).get('turnId')
            and transport.get('errors') == [{'error_type': 'turn_completed_timeout', 'kind': 'timeout'}]
            and transport.get('formal_usage', {}).get('entries')):
        return {'reason': 'recorded_primary_turn_timeout_no_retry',
                'transport': {'path': transport_path.relative_to(config_root).as_posix(),
                              'sha256': _sha(transport_path.read_bytes())}}
    ids = {row.get('id') for row in transport.get('formal_usage', {}).get('entries', [])}
    for ref in transport.get('provider_response_archives', []):
        archive = _json(_artifact(ref, config_root))
        if (archive.get('error_type') == 'timeout' and archive.get('http_status') == 502
                and archive.get('ledger_status') == 'network_error_usage_unknown_reserved_charge_retained'
                and archive.get('ledger_request_id') in ids
                and archive.get('p18_active_operation', {}).get('operation_id') == receipt['unit_id']):
            return {'reason': 'recorded_upstream_timeout_no_retry', 'provider_archive': ref,
                    'ledger_request_id': archive['ledger_request_id']}
    return None


def run_host_queries(*, formal_config_path, output_root, run=False):
    """Use the config's frozen host_query_inputs and host_query_bindings refs.

    host_query_bindings is a JSON object mapping each existing planner unit_id
    to a {path,sha256} reference to its real p18_host_arm_binding manifest.
    Artifact paths are relative to the formal config directory. The mutable
    shared ledger may remain outside it via the frozen original-file binding.
    """
    config_path = Path(formal_config_path).resolve()
    readiness = verify_formal_run_config(config_path)
    if not readiness.formal_execution_allowed:
        raise HostQueryError("formal_freeze_required:"+",".join(readiness.reasons))
    raw = _json(config_path)
    declared_identity = raw.get("identity_map_sha256")
    from p18_score_report import IDENTITY_MAP_SHA256
    if declared_identity is None:
        raise HostQueryError("identity_map_sha256_required")
    if declared_identity != IDENTITY_MAP_SHA256:
        raise HostQueryError("identity_map_sha256_mismatch")
    units_path = _artifact(raw.get("host_query_inputs"), config_path.parent)
    bindings_path = _artifact(raw.get("host_query_bindings"), config_path.parent)
    loaded_rows = [json.loads(line) for line in units_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    units = query_units(loaded_rows, require_identity_binding=True)
    execution_method = raw.get("host_query_method_id")
    expected = host_query_operation_map(units, execution_method=execution_method)
    if any(readiness.details["operations"].get(key) != value for key,value in expected.items()):
        raise HostQueryError("query_operations_must_match_freeze")
    bindings = _json(bindings_path)
    if not isinstance(bindings,dict) or set(bindings) != {row["unit_id"] for row in units}:
        raise HostQueryError("one_actual_binding_per_condition_required")
    retained_refs = (_json(_artifact(raw['host_query_retained_conditions'], config_path.parent))
                     if 'host_query_retained_conditions' in raw else {})
    if not isinstance(retained_refs, dict) or not set(retained_refs).issubset(bindings):
        raise HostQueryError('retained_condition_not_in_frozen_plan')
    retained = {}
    workers = raw.get('host_query_concurrency', 1)
    if type(workers) is not int or not 1 <= workers <= 4:
        raise HostQueryError('query_concurrency_must_be_between_one_and_four')
    output = Path(output_root).resolve()
    if not output.is_relative_to(config_path.parent) or "test" not in str(output).lower() or output.exists():
        raise HostQueryError("new_TEST_output_inside_formal_bundle_required")
    actual = {}
    roots_seen = set()
    candidate_checked = False
    executable_hashes = {}
    method = _json(readiness.details["method_path"])["method"]
    for row in units:
        binding = _json(_artifact(bindings[row["unit_id"]],config_path.parent))
        match = _UNIT.fullmatch(row["unit_id"])
        expected_host = expected[row["unit_id"]]["host_id"]
        permitted = {expected_host, match[1]} if match[1].startswith("codex_") else {expected_host}
        if binding.get("host_id") not in permitted or binding.get("arm_id") != match[2]:
            raise HostQueryError("condition_host_arm_binding_mismatch")
        if row['unit_id'] in retained_refs:
            retained[row['unit_id']] = _retained_condition(
                retained_refs[row['unit_id']], config_path.parent, binding, row['unit_id'],
                allow_closed_failures=raw.get('host_query_retain_closed_failures') is True)
        is_cli = expected_host == "hermes_cli_local_input_v1"
        if not match[1].startswith("codex_") and not is_cli:
            raise HostQueryError("Hermes_query_requires_adjudicated_CLI_not_A2A")
        if binding["arm_id"] == "B" and binding.get("status") == "UNSUPPORTED":
            actual[row["unit_id"]]=binding
            continue
        if is_cli:
            from p18_hermes_cli_transport import HermesCLITransport
            HermesCLITransport(binding, config_path, None)  # validate only; constructor starts no process
            if "hermes_method_path" not in readiness.details:
                raise HostQueryError("CLI_method_not_bound_to_G2")
        else:
            executable = Path(binding["fixed_host"]["executable_path"]).resolve()
            if executable not in executable_hashes:
                executable_hashes[executable] = _sha(executable.read_bytes())
            authorized_exe = {
                method["executable_sha256"],
                # User-authorized 2026-09-10: current freeze C-arm official appserver pin.
                # Does not rewrite sealed method.json bytes / G2 METHOD_SHA256.
                "ccdc9eb9dd71fbcfb03ad42c4eca2b0d6ff6fbd32ebe9416550e6244561e559b",
            }
            if (executable_hashes[executable] not in authorized_exe
                    or binding["fixed_host"].get("version") != method["executable_version"]):
                raise HostQueryError("actual_executable_must_match_frozen_method")
        if binding.get('native_source_import',{}).get('method') == 'native-A2-reviewed-v2':
            source_state=import_condition_history(row,binding)
            if source_state['status']=='SOURCE_PREPARATION_FAILED':
                actual[row['unit_id']]=binding
                continue
        root = Path(binding["roots"]["binding_root"]).resolve()
        from p18_native_a2_gate import permits_external_root
        external_A2 = permits_external_root(binding)
        for path in (root,Path(binding["roots"]["home_path"]).resolve(),Path(binding["roots"]["database_path"]).resolve(),
                     Path(binding["launch_contract"]["working_directory"]).resolve()):
            if not path.is_relative_to(root) or (not root.is_relative_to(config_path.parent) and not external_A2) or "test" not in str(root).lower():
                raise HostQueryError("condition_requires_isolated_TEST_paths")
            # Include path type: home and cwd may intentionally coincide in a
            # legacy binding, but no resource can be shared between conditions.
            if (str(path),row["unit_id"]) not in roots_seen and any(p==str(path) for p,_ in roots_seen):
                raise HostQueryError("cross_condition_storage_reuse")
            roots_seen.add((str(path),row["unit_id"]))
        if units_path.is_relative_to(Path(binding["launch_contract"]["working_directory"]).resolve()):
            raise HostQueryError("private_planner_file_inside_model_workspace")
        if binding["arm_id"] == "C" and row['unit_id'] not in retained:
            from scope_recall.runtime.instance import RuntimeInstanceConfig
            runtime = RuntimeInstanceConfig.from_mapping(_json(binding["roots"]["runtime_config_path"]))
            if runtime.auxiliary and (runtime.auxiliary.external_embedding or runtime.auxiliary.external_consolidation):
                if runtime.auxiliary.ledger_path is None or runtime.auxiliary.ledger_path.resolve() != Path(readiness.details["ledger_path"]).resolve():
                    raise HostQueryError("shared_auxiliary_ledger_required")
            verify_condition_storage_identity(binding)
            database = Path(binding["roots"]["database_path"]).resolve()
            with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=.25) as db:
                if db.execute("SELECT count(*) FROM source_events").fetchone()[0]:
                    raise HostQueryError("condition_source_database_not_empty")
            if not candidate_checked:
                _candidate_import_matches(readiness.details["candidate_wheel_path"])
                candidate_checked = True
        actual[row["unit_id"]] = binding
        if binding["arm_id"] == "D":
            import_condition_history(row,binding)  # verify existing immutable raw archive only
        if binding['arm_id'] == 'A' and binding.get('native_source_import'):
            import_condition_history(row,binding)  # read-only receipt/rollout integrity check
    if workers > 1:
        from p18_hermes_cli_transport import frozen_meter_port
        ports = [frozen_meter_port(b) for uid, b in actual.items()
                 if uid not in retained and b['host_id'] == 'hermes_cli_local_input_v1']
        if len(set(ports)) != len(ports):
            raise HostQueryError('parallel_Hermes_conditions_require_distinct_frozen_meter_ports')
    summary = {"status":"PREFLIGHT_ONLY","query_conditions":80,"independent_query_pairs":40,"semantic_pass":False,
               'max_parallel_conditions': workers,
               "history_entry":"synthetic_imported_dialogue_not_host_L1_capture",
               "unsupported_import_arms":sorted({b["arm_id"] for b in actual.values()
                   if b["arm_id"] in {"A","B"} and b["host_id"] != "hermes_cli_local_input_v1"
                   and not (b['arm_id'] == 'A' and b.get('native_source_import'))})}
    if not run:
        return summary
    output.mkdir(parents=True)
    completed = dict(retained)
    executable = []
    for row in units:
        if row['unit_id'] in retained:
            continue
        binding = actual[row["unit_id"]]
        if (binding["arm_id"] in {"A","B"} and binding["host_id"] != "hermes_cli_local_input_v1"
                and not (binding['arm_id'] == 'A' and binding.get('native_source_import'))):
            completed[row['unit_id']] = {"unit_id":row["unit_id"],"status":"UNSUPPORTED_IMPORT","reason":"no_verified_native_raw_import_entry","model_calls":0,"semantic_pass":False}
            continue
        executable.append(row)
    def execute_row(row):
        binding = actual[row['unit_id']]
        condition_output = _condition_output(binding, config_path.parent)
        if binding.get('native_source_import',{}).get('method') == 'native-A2-reviewed-v2':
            imported=import_condition_history(row,binding)
            if imported['status']=='SOURCE_PREPARATION_FAILED':
                condition_output.mkdir(parents=True,exist_ok=False)
                receipt={'unit_id':row['unit_id'],'status':'SOURCE_PREPARATION_FAILED','semantic_pass':False,'source_load_complete':False,'model_calls':0,'formal_query_dispatched':False,'import':imported}
                receipt_path=condition_output/'condition-receipt.json'
                receipt_path.write_bytes(json.dumps(receipt,ensure_ascii=False,indent=2).encode('utf-8'))
                return {'unit_id':row['unit_id'],'status':receipt['status'],'receipt_path':str(receipt_path),'receipt_sha256':_sha(receipt_path.read_bytes()),'source_load_complete':False}
        execute = _execute_cli_condition if binding["host_id"] == "hermes_cli_local_input_v1" else _execute_codex_condition
        receipt = execute(row,binding,readiness,config_path,condition_output)
        receipt_path=condition_output/"condition-receipt.json"
        result = {"unit_id":row["unit_id"],"status":receipt["status"],"receipt_path":str(receipt_path),
                         "receipt_sha256":_sha(receipt_path.read_bytes()),
                         "source_load_complete":receipt.get("source_load_complete")}
        if raw.get('host_query_continue_isolated_upstream_failures') is True:
            decision = _isolated_upstream_failure(receipt, condition_output, config_path.parent)
            if decision is not None:
                result.update(continue_dispatch_after_failure=True, dispatch_decision=decision)
        return result
    completed.update(_bounded_conditions(executable, execute_row, workers))
    receipts = [completed[row['unit_id']] for row in units]
    summary.update({"status":"EXECUTED" if all(r["status"]=="EXECUTED" and r.get("source_load_complete") is not False for r in receipts)
                    else "EXECUTED_WITH_GAPS", "conditions":receipts})
    (output/"host-query-receipt.json").write_bytes(json.dumps(summary,ensure_ascii=False,indent=2).encode("utf-8"))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-config-path",required=True)
    parser.add_argument("--output-root",required=True)
    parser.add_argument("--run",action="store_true")
    outcome = run_host_queries(**vars(parser.parse_args()))
    print(json.dumps({key:outcome[key] for key in ("status","query_conditions","independent_query_pairs","semantic_pass")}))
