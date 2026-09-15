"""Direct P18 journey entry: frozen inputs -> owned host -> formal receipts.

Default invocation only checks configuration. --run starts owned TEST hosts;
no user supplies callback implementations. Semantic scoring stays independent.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import zipfile

from p18_formal_evidence import verify_formal_run_config
from p18_journey_execution import CoreJourneyControls, JourneyExecutionError, load_journey
from p18_journey_host_bridge import (
    CodexAppServerSessionControl, CodexJourneyHostBridge, HermesJourneyHostBridge,
    HermesRoutingSessionControl, HermesRoutingSessionObserver, JourneySourceRefsProvider,
)
from p18_owned_host_lifecycle import OwnedHermesProcess, ensure_frozen_hermes_attempt_authorization

# Direct script invocation must resolve the existing probes package too.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def attachment_path_input(model_input, workspace):
    """SVG/text files go through actual host file tools, not fake FilePart OCR.

    Frozen Hermes _prepare_task extracts text; raw FilePart bytes are not
    decoded. Codex's public UserInput includes localImage, but SVG/text use
    the exact authorized local file path. No assertion/expected field enters.
    """
    root = Path(workspace).resolve()
    paths = []
    for row in model_input["attachments"]:
        path = (root / row["workspace_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
            raise JourneyExecutionError("actual_workspace_attachment_required")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["asset"]["sha256"]:
            raise JourneyExecutionError("actual_workspace_attachment_hash_mismatch")
        paths.append(str(path))
    return model_input["query"] + ("\n附件是已授权读取的本地文件，请用文件工具读取准确路径：\n" + "\n".join(paths) if paths else "")


def _candidate_import_matches(wheel):
    import scope_recall
    package = Path(scope_recall.__file__).resolve().parent
    with zipfile.ZipFile(wheel) as archive:
        files = [name for name in archive.namelist() if name.startswith("scope_recall/") and not name.endswith("/")]
        for name in files:
            local = package / name.removeprefix("scope_recall/")
            if not local.is_file() or local.read_bytes() != archive.read(name):
                raise JourneyExecutionError("run_interpreter_does_not_import_frozen_candidate")
    return len(files)


def _install_worktree_historical_hermes_cover():
    """After the freeze-wheel import check, load the dirty-tree 4340 cover.

    The frozen candidate still fail-closes any meter_breach. hold_real_consolidation
    runs in this process and must see AuxiliaryBudgetLedger.covered_historical_breaches.
    Host capture still uses the binding's extracted wheel. This is not a new candidate.
    """
    if not os.environ.get("SCOPE_RECALL_HERMES_ATTEMPT_AUTHORIZATION"):
        return False
    repo = Path(__file__).resolve().parents[2]

    def load(name, relative):
        path = repo / relative
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    models = load("scope_recall.adapters.models", Path("adapters") / "models.py")
    if not hasattr(models, "_consolidation_request_headers"):
        raise JourneyExecutionError("worktree_opencode_session_header_not_imported")
    budget = load("scope_recall.runtime.model_budget", Path("runtime") / "model_budget.py")
    if not hasattr(budget, "load_hermes_attempt_authorization"):
        raise JourneyExecutionError("worktree_auxiliary_historical_cover_not_imported")
    auxiliary = load("scope_recall.runtime.auxiliary", Path("runtime") / "auxiliary.py")
    instance = sys.modules.get("scope_recall.runtime.instance")
    if instance is not None:
        instance.build_auxiliary_runtime = auxiliary.build_auxiliary_runtime
    return True


def _codex_hook_interpreter_handler_path(binding):
    """Locate the handler file that `python -I -m scope_recall...` actually loads."""
    python = Path(binding["loader"]["candidate_python"]["path"]).resolve()
    return python.parent.parent / "Lib" / "site-packages" / "scope_recall" / "adapters" / "codex" / "handler.py"


def _install_worktree_codex_persist_cover(binding):
    """After the freeze-wheel import check, overlay persist-before-runtime into the hook interpreter.

    Codex hooks use `python -I` and ignore PYTHONPATH, so the per-slot extracted
    wheel never reaches capture. This copies dirty adapters/codex/handler.py over
    the candidate interpreter site-packages handler, then restores it. Timeouts
    stay 1s/2s. This is not a new candidate.
    """
    if not os.environ.get("SCOPE_RECALL_CODEX_ATTEMPT_AUTHORIZATION"):
        return None
    repo = Path(__file__).resolve().parents[2]
    dirty = repo / "adapters" / "codex" / "handler.py"
    dirty_bytes = dirty.read_bytes()
    if b"def _ensure_host_runtime(" not in dirty_bytes:
        raise JourneyExecutionError("worktree_codex_persist_cover_missing")
    if b"_CAPTURE_TIMEOUT_S = 1.0" not in dirty_bytes or b"_TOTAL_BUDGET_S = 2.0" not in dirty_bytes:
        raise JourneyExecutionError("worktree_codex_persist_cover_timeout_changed")
    target = _codex_hook_interpreter_handler_path(binding)
    if not target.is_file():
        raise JourneyExecutionError("codex_hook_interpreter_handler_missing")
    if target.resolve().is_relative_to(repo / "adapters"):
        raise JourneyExecutionError("codex_persist_cover_refuses_to_overwrite_worktree_source")
    original = target.read_bytes()
    if original == dirty_bytes:
        return None
    backup = target.with_name("handler.py.frozen-backup")
    backup.write_bytes(original)
    target.write_bytes(dirty_bytes)

    def restore():
        current = target.read_bytes()
        if current == dirty_bytes:
            target.write_bytes(original)
        if backup.is_file() and backup.read_bytes() == original:
            backup.unlink()

    return restore


def journey_operation_map(bundle_path, journey_id, *, host_id, arm_id, operation_prefix=""):
    """Planner interface: freeze only executable turn metadata, never inputs."""
    _, journey = load_journey(Path(bundle_path), journey_id)
    result = {}
    fault = None
    for action in journey["actions"]:
        if action["kind"] == "sqlite_unavailable":
            fault = "sqlite_unavailable"
        elif action["kind"] == "sqlite_restore":
            fault = None
        elif action["kind"] == "host_turn":
            operation_id = operation_prefix + action["operation_id"]
            value = {"host_id": host_id, "arm_id": arm_id, "request_id": operation_id + "-request",
                     "unit": {"kind": "round", "query_id": None, "journey_id": journey_id,
                              "round_id": str(action["primary_round_ordinal"])}}
            if fault:
                value["fault"] = fault
            result[operation_id] = value
    return result


def bound_p11_bridge_ledger(formal_config_path, readiness_ledger=None):
    """Same hash-bound formal ledger the owned P11 meter process will open."""
    from probes.hermes.p11_a2a_testkit import resolve_hash_bound_formal_ledger

    config_path = Path(formal_config_path).resolve()
    resolved = resolve_hash_bound_formal_ledger(
        config_path, hashlib.sha256(config_path.read_bytes()).hexdigest()
    )
    if readiness_ledger is not None and Path(readiness_ledger).resolve() != resolved:
        raise JourneyExecutionError("existing_P11_bridge_ledger_binding_mismatch")
    return resolved


def cli_host(binding, formal_config_path, formal_output, readiness):
    """Concrete metered official CLI; callers supply no lifecycle callbacks."""
    from p18_hermes_cli_transport import HermesCLITransport
    from p18_hermes_operation_budget import HermesOperationBudget
    from p18_journey_host_bridge import HermesCLISessionControl, HermesCLIJourneyHostBridge
    config_path = Path(formal_config_path).resolve()
    ledger = bound_p11_bridge_ledger(config_path, readiness.details["ledger_path"])
    budget = HermesOperationBudget(binding["roots"]["binding_root"], ledger,
        freeze_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        operation_root=Path(formal_output)/"operations", config_root=config_path.parent,
        operation_lease_seconds=120)
    transport = HermesCLITransport(binding, config_path, budget)
    control = HermesCLISessionControl(transport)
    return HermesCLIJourneyHostBridge(transport=transport, session_control=control, session_observer=control.observe,
        formal_config_path=config_path, output_root=formal_output,
        source_refs_provider=JourneySourceRefsProvider(binding["roots"]["database_path"] if binding["arm_id"] == "C" else None),
        attachment_encoder=attachment_path_input)


def run_journey(*, host_binding_path, formal_config_path, journey_bundle_path, journey_id,
                output_root, operation_prefix="", run=False):
    ensure_frozen_hermes_attempt_authorization()
    binding = _json(host_binding_path)
    readiness = verify_formal_run_config(formal_config_path)
    if not readiness.formal_execution_allowed:
        raise JourneyExecutionError("formal_freeze_required:" + ",".join(readiness.reasons))
    config_root = Path(formal_config_path).resolve().parent
    root = Path(binding["roots"]["binding_root"]).resolve()
    output = Path(output_root).resolve()
    if not output.is_relative_to(root) or not root.is_relative_to(config_root) or "test" not in str(root).lower():
        raise JourneyExecutionError("output_must_be_in_bound_TEST_arm_and_formal_bundle")
    input_root, journey = load_journey(Path(journey_bundle_path), journey_id)
    if binding["host_id"] not in {"hermes_a2a", "hermes_cli_local_input_v1", "codex_windows_desktop", "codex_windows_appserver_native_hooks_v2"}:
        raise JourneyExecutionError("unrecognized_frozen_host_binding")
    host_id = binding["host_id"] if binding["host_id"].startswith("hermes_") else "codex_windows_appserver_native_hooks_v2"
    operations = readiness.details["operations"]
    active_fault = None
    for action in journey["actions"]:
        if action["kind"] == "sqlite_unavailable":
            active_fault = "sqlite_unavailable"
        elif action["kind"] == "sqlite_restore":
            active_fault = None
        elif action["kind"] == "host_turn":
            op = operations.get(operation_prefix + action["operation_id"])
            if (not isinstance(op, dict) or op["host_id"] != host_id or op["arm_id"] != binding["arm_id"]
                    or op["unit"]["journey_id"] != journey_id or op.get("fault") != active_fault):
                raise JourneyExecutionError("journey_action_not_bound_to_frozen_host_arm_fault_map")
    workspace = Path(binding["launch_contract"]["working_directory"]).resolve()
    # Models may read the explicitly supplied files; never put fixture/assertion
    # directories in their cwd. Private bundle remains outside this workspace.
    if input_root.is_relative_to(workspace) or workspace == root:
        raise JourneyExecutionError("private_inputs_must_be_outside_host_workspace")
    arm = binding["arm_id"]
    capture_scope = None
    runtime_config = None
    ledger = Path(readiness.details["ledger_path"]).resolve()
    if arm == "C":
        from scope_recall.runtime.instance import RuntimeInstanceConfig
        raw = _json(binding["roots"]["runtime_config_path"])
        runtime_config = RuntimeInstanceConfig.from_mapping(raw)
        if (runtime_config.auxiliary and (runtime_config.auxiliary.external_embedding or runtime_config.auxiliary.external_consolidation)
                and (runtime_config.auxiliary.ledger_path is None or runtime_config.auxiliary.ledger_path.resolve() != ledger)):
            raise JourneyExecutionError("auxiliary_must_use_frozen_shared_ledger")
        matched = _candidate_import_matches(readiness.details["candidate_wheel_path"])
        _install_worktree_historical_hermes_cover()
    else:
        matched = None
    if host_id == "hermes_a2a":
        manifest = binding.get("installation_manifest") or {}
        audiences = (manifest.get("payload") or {}).get("audiences", [])
        matches = [a for a in audiences if a.get("platform") == "a2a" and a.get("chat_type") == "dm"
                   and a.get("thread_id") == ""]
        if arm == "C" and len(matches) != 1:
            raise JourneyExecutionError("exact_actual_A2A_dm_audience_required")
        context_id = matches[0]["chat_id"] if matches else binding.get("host_context_id")
        if not isinstance(context_id, str) or not context_id.strip():
            raise JourneyExecutionError("bound_Hermes_context_required")
        capture_scope = matches[0]["capture_scope_id"] if matches else None
        config = _json(binding["roots"]["config_path"])
        reset = config.get("session_reset", {})
        if reset.get("mode") != "idle" or not 0 < reset.get("idle_minutes", 0) <= 60:
            raise JourneyExecutionError("frozen_Hermes_idle_reset_configuration_required")
        if (workspace == Path(binding["roots"]["home_path"]).resolve()
                or Path(config.get("terminal", {}).get("cwd", "")).resolve() != workspace):
            raise JourneyExecutionError("Hermes_needs_separate_actual_tool_workspace")
    result = {"status": "PREFLIGHT_ONLY", "host_id": host_id, "arm_id": arm, "journey_id": journey_id,
              "candidate_package_files_matched": matched, "model_calls": 0, "semantic_pass": False}
    if host_id == "hermes_cli_local_input_v1":
        from p18_hermes_cli_transport import HermesCLITransport
        HermesCLITransport(binding, formal_config_path, None)  # no process or credentials at construction
        if "hermes_method_path" not in readiness.details:
            raise JourneyExecutionError("CLI_method_must_be_frozen_in_G2")
        audiences = ((binding.get("installation_manifest") or {}).get("payload") or {}).get("audiences", [])
        capture_scope = next((a["capture_scope_id"] for a in audiences if a.get("platform") == "cli"
                              and a.get("chat_id") == "local" and a.get("thread_id") == "main"), None)
        if arm == "C" and capture_scope is None:
            raise JourneyExecutionError("actual_CLI_audience_required")
    if not run:
        return result
    if output.exists():
        raise JourneyExecutionError("journey_output_must_be_new")
    output.mkdir(parents=True)
    restore_codex_cover = None
    runtime = None
    host = None
    try:
        if host_id.startswith("codex"):
            restore_codex_cover = _install_worktree_codex_persist_cover(binding)
        source_refs = JourneySourceRefsProvider(binding["roots"]["database_path"] if arm == "C" else None)
        if host_id == "hermes_cli_local_input_v1":
            formal_output = output / "formal"
            formal_output.mkdir()
            host = cli_host(binding, formal_config_path, formal_output, readiness)
        elif host_id == "hermes_a2a":
            from p18_hermes_a2a_transport import HermesA2ATransport, HermesTransportConfig
            from p18_hermes_operation_budget import HermesOperationBudget
            ledger = bound_p11_bridge_ledger(formal_config_path, ledger)
            owner = OwnedHermesProcess(binding, formal_config_path=formal_config_path, context_id=context_id)
            # Frozen build_session_key(SessionSource(platform=a2a,chat_type=dm,
            # chat_id=context_id), profile=default): the sender is not appended in DM.
            route = {"context_id": context_id, "session_key": f"agent:main:a2a:dm:{context_id}",
                     "scope": str((owner.home / "sessions").resolve()), "platform": "a2a"}
            aliases = {a["session_alias"] for a in journey["actions"]}
            control = HermesRoutingSessionControl(state_db=owner.home / "state.db", routes={a:route for a in aliases},
                        config_root=root, process_owner=owner, idle_seconds=reset["idle_minutes"]*60)
            budget = HermesOperationBudget(root, ledger, freeze_sha256=hashlib.sha256(Path(formal_config_path).read_bytes()).hexdigest(),
                                           operation_root=output / "formal/operations", config_root=config_root)
            env = _json(binding["roots"]["environment_path"])
            transport = HermesA2ATransport(HermesTransportConfig(endpoint="http://127.0.0.1:19921",
                expected_agent_card_identity={"name":env["A2A_AGENT_NAME"]}, isolation_root=root,
                formal_config_path=Path(formal_config_path), formal_evaluation=True, allow_diagnostic_fixture=False), budget)
            kwargs = {"session_control":control, "session_observer":HermesRoutingSessionObserver(control)}
            cls = HermesJourneyHostBridge
        else:
            from p18_codex_appserver_transport import CodexAppServerTransport, CodexTransportConfig, ARM_HOOK_POLICIES
            from p18_codex_budget import CodexSubmissionBudget
            from p18_formal_runner import CodexLedgerBudgetAdapter
            config = CodexTransportConfig(codex_exe=Path(binding["fixed_host"]["executable_path"]), cwd=workspace,
                arm_id=arm, expected_hooks_policy=ARM_HOOK_POLICIES[arm], formal_evaluation=True,
                formal_config_path=Path(formal_config_path), environment=__import__("p18_codex_appserver_transport").codex_binding_environment(binding))
            budget = CodexLedgerBudgetAdapter(CodexSubmissionBudget(ledger), operation_root=output / "formal/operations", config_root=config_root)
            transport = CodexAppServerTransport(config,budget=budget)
            control = CodexAppServerSessionControl(transport, runtime_config.binding.data_directory if runtime_config else None)
            kwargs = {"session_control":control}
            if arm == "D":
                kwargs["simple_capture_path"] = Path(binding["loader"]["capture_archive"]["path"])
            cls = CodexJourneyHostBridge
        if host_id != "hermes_cli_local_input_v1":
            formal_output = output / "formal"
            formal_output.mkdir()
            host = cls(transport=transport, formal_config_path=formal_config_path, output_root=formal_output,
                       source_refs_provider=source_refs, attachment_encoder=attachment_path_input, **kwargs)
        if runtime_config is not None:
            from scope_recall.runtime.instance import build_runtime_instance
            runtime = build_runtime_instance(runtime_config)
        controls = CoreJourneyControls(runtime=runtime, operator_context=runtime_config.context() if runtime_config else None,
            scope_id=capture_scope or (next(iter(runtime_config.allowed_scope_ids)) if runtime_config else ""), arm_root=root,
            workspace=workspace, evidence_root=output / "controls", host=host)
        host.journey_controls = controls
        host.resume()
        from p18_journey_execution import execute_journey
        result = execute_journey(Path(journey_bundle_path),journey_id,host=host,controls=controls,operation_prefix=operation_prefix)
        (output / "journey-receipt.json").write_bytes(json.dumps(result,ensure_ascii=False,indent=2).encode("utf-8"))
        return result
    finally:
        if restore_codex_cover is not None:
            restore_codex_cover()
        if host is not None:
            host.close()
        if runtime is not None:
            runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host-binding-path", "formal-config-path", "journey-bundle-path", "journey-id", "output-root"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--operation-prefix",default="")
    parser.add_argument("--run",action="store_true")
    args = vars(parser.parse_args())
    result = run_journey(**args)
    print(json.dumps({k:v for k,v in result.items() if k in {"status","host_id","arm_id","journey_id","semantic_pass"}}))
    return 0 if not result["status"].startswith("FAILED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
