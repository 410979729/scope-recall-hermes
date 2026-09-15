"""P18 formal freeze tool.

Assembles the frozen formal-run config (``scope-recall.p18-formal-run-config.v1``)
that ``p18_formal_evidence.verify_formal_run_config`` accepts.

The main line built every input artifact but never built the freeze step itself:

- protocol / method / hermes-method artifacts already exist with bytes pinned by
  hash in ``p18_formal_evidence``;
- the candidate build receipt + wheel come from the candidate freeze env
  (``freeze_candidate.py`` pattern);
- the operations map derives deterministically from the sealed run plan units
  (``p18_run_host_queries.host_query_operation_map``) and the journey bundle
  (``p18_run_journey.journey_operation_map``) — no invented identifiers;
- the G2 PASS report is the independent auditor's artifact; this tool binds it
  but never produces it.

The tool stages immutable evidence artifacts into the freeze directory.  A
prepared host matrix remains in place; only its real input projections and
binding maps are added beside the formal config.  The original mutable ledger
is referenced by an identity binding.  It then runs the authoritative verifier
and the query preflight, recording ``NOT_READY`` for any missing or mismatched
input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from p18_formal_evidence import (
    CONFIG_SCHEMA,
    HERMES_METHOD_ID,
    METHOD_ID,
    verify_formal_run_config,
)
from p18_score_report import IDENTITY_MAP_SHA256
from p18_ledger_reference import freeze_ledger_binding, ledger_path_reference
from p18_run_host_queries import _ROLES, _UNIT
from p18_run_journey import journey_operation_map

FREEZE_RECEIPT_SCHEMA = "scope-recall.p18-formal-freeze-receipt.v1"

_QUERY_HOSTS = {"codex_windows_desktop", "codex_windows_appserver_native_hooks_v2",
                "hermes_a2a", "hermes_cli_local_input_v1"}


def _execution_method(source_host: str) -> str:
    """Map a historical planner host name to its actual frozen method."""
    if source_host.startswith("codex_"):
        return METHOD_ID
    if source_host in {"hermes_a2a", HERMES_METHOD_ID}:
        return HERMES_METHOD_ID
    raise FreezeError(f"unsupported_planner_host:{source_host}")


def _planner_query_operation_map(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive query operations from sealed planner rows.

    Mirrors p18_run_host_queries.host_query_operation_map conventions, but
    validates the planner row shape ({kind, model_input, ordinal,
    source_sequence, unit_id}) rather than the executor shape: sealed planner
    rows deliberately carry no source_records (those stay in the private
    association until execution).
    """
    operations: dict[str, Any] = {}
    seen: set[str] = set()
    pairs: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise FreezeError("planner_row_object_required")
        if row.get("kind") != "host_query":
            continue  # planner files also carry the separate journey graph
        if set(row) != {"unit_id", "kind", "ordinal", "source_sequence", "model_input"}:
            raise FreezeError("planner_host_query_schema_invalid")
        match = _UNIT.fullmatch(str(row["unit_id"]))
        if not match or not 1 <= int(match[3]) <= 40 or row["unit_id"] in seen:
            raise FreezeError("planner_unit_id_invalid_or_duplicate")
        if row["source_sequence"] != "source_seed_then_new_session_query":
            raise FreezeError("planner_source_sequence_invalid")
        natural = row["model_input"]
        if not isinstance(natural, dict) or set(natural) != {"history", "query"}:
            raise FreezeError("planner_natural_input_required")
        query = natural["query"]
        if not isinstance(query, dict) or set(query) != {"text"} or not isinstance(query["text"], str) or not query["text"].strip():
            raise FreezeError("planner_natural_query_required")
        history = natural["history"]
        if not isinstance(history, list) or not history:
            raise FreezeError("planner_natural_history_required")
        for event in history:
            if (not isinstance(event, dict) or set(event) not in ({"source_type", "speaker_role", "text"}, {"source_type", "speaker_role", "text", "occurred_at"})
                    or event.get("source_type") not in _ROLES or event.get("speaker_role") != _ROLES[event["source_type"]]
                    or not isinstance(event["text"], str) or not event["text"].strip()
                    or ("occurred_at" in event and not isinstance(event["occurred_at"], str))):
                raise FreezeError("planner_natural_source_schema_invalid")
        seen.add(row["unit_id"])
        pairs.add((int(match[3]), int(match[4])))
        unit_id = row["unit_id"]
        # Preserve the historical unit id, but bind execution to the real
        # adjudicated method used by the formal runner.
        host_id = _execution_method(match[1])
        operations[unit_id] = {"host_id": host_id, "arm_id": match[2], "request_id": unit_id + "-request",
                               "unit": {"kind": "query", "query_id": unit_id, "journey_id": None, "round_id": None}}
    if pairs != {(pair, condition) for pair in range(1, 41) for condition in (1, 2)}:
        raise FreezeError("planner_exact_40_pairs_80_conditions_required")
    return operations


class FreezeError(ValueError):
    """A freeze input is missing, unreadable, or inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeError(f"freeze_input_unreadable:{path}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise FreezeError(f"freeze_unit_row_invalid:{path}")
                rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeError(f"freeze_input_unreadable:{path}") from exc
    return rows


def _stage_artifact(source: Path, stage_dir: Path, name: str) -> dict[str, str]:
    """Copy an artifact into the freeze dir; return its relative reference."""
    if not source.is_file():
        raise FreezeError(f"freeze_artifact_missing:{source}")
    stage_dir.mkdir(parents=True, exist_ok=True)
    target = stage_dir / name
    if target.exists():
        raise FreezeError(f"freeze_output_exists:{target}")
    shutil.copyfile(source, target)
    return {"path": f"{stage_dir.name}/{name}", "sha256": _sha256(target)}


def _derive_operations(plan_root: Path, journey_bundle: Path, journey_ids: list[str]) -> dict[str, Any]:
    """Derive the frozen operations map from the sealed plan + journey bundle."""
    operations: dict[str, Any] = {}
    units_root = plan_root / "units"
    for host_dir in sorted(p for p in units_root.iterdir() if p.is_dir()):
        # Exactly one host-arm per planner file.
        for arm_file in sorted(host_dir.glob("*.jsonl")):
            query_ops = _planner_query_operation_map(_read_jsonl(arm_file))
            for operation_id, value in query_ops.items():
                if operation_id in operations:
                    raise FreezeError(f"duplicate_operation_id:{operation_id}")
                operations[operation_id] = value
    for journey_id in journey_ids:
        for host_id, arm_ids in _journey_host_arms(plan_root).items():
            for arm_id in arm_ids:
                prefix = f"{host_id}-{arm_id}-{journey_id}-"
                journey_ops = journey_operation_map(
                    journey_bundle, journey_id, host_id=host_id, arm_id=arm_id, operation_prefix=prefix
                )
                for operation_id, value in journey_ops.items():
                    if operation_id in operations:
                        raise FreezeError(f"duplicate_operation_id:{operation_id}")
                    operations[operation_id] = value
    if not operations:
        raise FreezeError("operations_derivation_empty")
    return operations


def _journey_host_arms(plan_root: Path) -> dict[str, list[str]]:
    """Journey host arms mirror the query unit host/arm directories."""
    result: dict[str, list[str]] = {}
    for host_dir in sorted(p for p in (plan_root / "units").iterdir() if p.is_dir()):
        host_id = _execution_method(host_dir.name)
        arms = sorted(path.stem for path in host_dir.glob("*.jsonl"))
        if not arms:
            raise FreezeError(f"journey_host_arms_missing:{host_dir.name}")
        result[host_id] = arms
    return result


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _read_matrix_group_map(matrix_root: Path, group: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    path = matrix_root / str(group.get("path", ""))
    if not path.is_file() or group.get("sha256") != _sha256(path):
        raise FreezeError(f"host_matrix_group_map_hash_mismatch:{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeError(f"host_matrix_group_map_unreadable:{path}") from exc
    if not isinstance(value, dict) or len(value) != 80:
        raise FreezeError("host_matrix_group_must_contain_80_bindings")
    for unit_id, reference in value.items():
        if not isinstance(unit_id, str) or not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise FreezeError("host_matrix_binding_reference_invalid")
        binding_path = matrix_root / str(reference["path"])
        if not binding_path.is_file() or reference["sha256"] != _sha256(binding_path):
            raise FreezeError(f"host_matrix_binding_hash_mismatch:{binding_path}")
    return value


def _stage_host_matrix(*, matrix_root: Path, plan_root: Path, output_dir: Path) -> dict[str, dict[str, Any]]:
    """Stage the prepared matrix and emit eight actual runner group refs."""
    matrix_root = matrix_root.expanduser().resolve()
    if not matrix_root.is_dir():
        raise FreezeError(f"host_matrix_root_missing:{matrix_root}")
    summary_path = matrix_root / "matrix-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeError("host_matrix_summary_unreadable") from exc
    groups = summary.get("group_maps") if isinstance(summary, dict) else None
    required = {f"{method}/{arm}" for method in ("codex_windows_desktop", HERMES_METHOD_ID) for arm in ("A", "B", "C", "D")}
    if not isinstance(groups, dict) or set(groups) != required:
        raise FreezeError("host_matrix_must_contain_eight_groups")

    # Keep prepared instances in their original matrix root.  The freeze only
    # adds input projections and maps under the common config parent.

    result: dict[str, dict[str, Any]] = {}
    for group_key in sorted(groups):
        source_method_id, arm_id = group_key.split("/", 1)
        method_id = METHOD_ID if source_method_id == "codex_windows_desktop" else source_method_id
        refs = _read_matrix_group_map(matrix_root, groups[group_key])
        source_host = "hermes_a2a" if source_method_id == HERMES_METHOD_ID else "codex_windows_desktop"
        plan_path = plan_root / "units" / source_host / f"{arm_id}.jsonl"
        if not plan_path.is_file():
            raise FreezeError(f"host_query_plan_missing:{plan_path}")
        input_rows: list[dict[str, Any]] = []
        for line in plan_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("kind") != "host_query":
                continue
            unit_id = str(row.get("unit_id", ""))
            if unit_id not in refs:
                raise FreezeError(f"host_matrix_binding_unit_missing:{unit_id}")
            source_path = matrix_root / "conditions" / source_method_id / arm_id / unit_id / "input" / "source-records.json"
            try:
                source_value = json.loads(source_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FreezeError(f"host_matrix_source_input_unreadable:{source_path}") from exc
            if not isinstance(source_value, dict) or not isinstance(source_value.get("history"), list):
                raise FreezeError("host_matrix_source_history_missing")
            natural = row.get("model_input")
            if not isinstance(natural, dict):
                raise FreezeError("host_matrix_query_input_missing")
            input_rows.append({"unit_id": unit_id, "kind": "host_query", "ordinal": row.get("ordinal"),
                               "source_sequence": "imported_history_then_new_session_query",
                               "source_records": source_value["history"], "model_input": natural})
        if len(input_rows) != 80:
            raise FreezeError(f"host_matrix_query_group_must_contain_80:{group_key}")
        input_path = output_dir / "frozen-host-query-inputs" / method_id / f"{arm_id}.jsonl"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        if input_path.exists():
            raise FreezeError(f"freeze_output_exists:{input_path}")
        input_path.write_bytes(b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8") for row in input_rows))
        output_refs: dict[str, dict[str, str]] = {}
        for unit_id, reference in refs.items():
            staged_binding = matrix_root / str(reference["path"])
            if not staged_binding.is_file():
                raise FreezeError(f"host_matrix_binding_missing:{staged_binding}")
            output_refs[unit_id] = {"path": staged_binding.relative_to(output_dir).as_posix(), "sha256": _sha256(staged_binding)}
        map_path = output_dir / "frozen-host-query-bindings" / method_id / f"{arm_id}.json"
        map_path.parent.mkdir(parents=True, exist_ok=True)
        if map_path.exists():
            raise FreezeError(f"freeze_output_exists:{map_path}")
        map_path.write_bytes(_json_bytes(output_refs))
        result[group_key] = {"method_id": method_id, "arm_id": arm_id, "query_count": 80,
                             "host_query_inputs": {"path": input_path.relative_to(output_dir).as_posix(), "sha256": _sha256(input_path)},
                             "host_query_bindings": {"path": map_path.relative_to(output_dir).as_posix(), "sha256": _sha256(map_path)}}
    return result


def build_frozen_config(
    *,
    plan_root: Path,
    journey_bundle: Path,
    journey_ids: list[str],
    protocol_artifact: Path,
    method_artifact: Path,
    hermes_method_artifact: Path | None,
    candidate_receipt: Path,
    candidate_wheel: Path,
    candidate_source_root: Path,
    candidate_git_root: Path | None,
    source_commit: str,
    source_manifest: Path,
    g2_report: Path,
    g2_evidence: list[Path],
    ledger: Path,
    output_dir: Path,
    host_matrix_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble the frozen config directory; returns the freeze receipt."""
    ledger_resolved = ledger.resolve()
    if not ledger_resolved.is_file():
        raise FreezeError(f"ledger_missing:{ledger}")
    output_dir = output_dir.expanduser().resolve()
    matrix_resolved = host_matrix_root.expanduser().resolve() if host_matrix_root is not None else None
    if matrix_resolved is not None:
        if not matrix_resolved.is_dir() or not matrix_resolved.is_relative_to(output_dir):
            raise FreezeError("host_matrix_root_must_live_under_output_dir")
        if not output_dir.is_dir():
            raise FreezeError("freeze_output_parent_must_exist_for_prepared_matrix")
    elif output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise FreezeError(f"freeze_output_exists:{output_dir}")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = output_dir / "artifacts"

    config: dict[str, Any] = {"schema": CONFIG_SCHEMA}
    config["protocol"] = _stage_artifact(protocol_artifact, artifacts, "protocol.json")

    method_reference = _stage_artifact(method_artifact, artifacts, "method.json")
    config["method"] = {"id": METHOD_ID, "artifact": method_reference}

    operations = _derive_operations(plan_root, journey_bundle, journey_ids)
    uses_hermes_cli = any(op["host_id"] == HERMES_METHOD_ID for op in operations.values())
    if uses_hermes_cli:
        if hermes_method_artifact is None:
            raise FreezeError("hermes_cli_arm_requires_hermes_method_artifact")
        hermes_reference = _stage_artifact(hermes_method_artifact, artifacts, "hermes-method.json")
        config["hermes_method"] = {"id": HERMES_METHOD_ID, "artifact": hermes_reference}

    candidate: dict[str, Any] = {
        "source_commit": source_commit,
        "receipt": _stage_artifact(candidate_receipt, artifacts, "candidate-build-receipt.json"),
        "wheel": _stage_artifact(candidate_wheel, artifacts, candidate_wheel.name),
    }
    # The verifier resolves candidate paths relative to the config dir and
    # rejects anything escaping it, so the frozen source tree is copied in.
    if not candidate_source_root.is_dir():
        raise FreezeError(f"candidate_source_root_missing:{candidate_source_root}")
    staged_source = artifacts / "candidate-source"
    shutil.copytree(candidate_source_root, staged_source)
    candidate["source_root"] = "artifacts/candidate-source"
    if candidate_git_root is not None:
        git_resolved = candidate_git_root.resolve()
        if git_resolved.is_dir() and git_resolved.is_relative_to(output_dir.resolve()):
            candidate["git_root"] = str(git_resolved.relative_to(output_dir.resolve()))
    config["candidate"] = candidate

    config["g2_review"] = {"artifact": _stage_artifact(g2_report, artifacts, "g2-report.json")}
    if not g2_evidence:
        raise FreezeError("g2_evidence_missing")
    # G2 evidence artifacts are referenced from inside the report; the verifier
    # resolves them relative to the config dir, so stage them alongside.
    for index, evidence_path in enumerate(g2_evidence):
        _stage_artifact(evidence_path, artifacts / "g2-evidence", f"{index:02d}-{evidence_path.name}")

    # The source-inputs digest follows scripts/check.py exactly: sha256 over the
    # canonical JSON of the git-blob source manifest (verification//docs excluded
    # upstream by the producer).  The manifest itself is staged for audit.
    manifest_value = _read_json(source_manifest)
    if not isinstance(manifest_value, dict) or not manifest_value or any(
        type(key) is not str or type(value) is not str for key, value in manifest_value.items()
    ):
        raise FreezeError("source_manifest_invalid")
    source_inputs_sha256 = hashlib.sha256(
        json.dumps(manifest_value, sort_keys=True).encode()
    ).hexdigest()
    _stage_artifact(source_manifest, artifacts, "source-manifest.json")
    config["source"] = {"commit": source_commit, "source_inputs_sha256": source_inputs_sha256}
    config["operations"] = operations
    config["ledger_path"] = ledger_path_reference(ledger_resolved, output_dir)
    config["ledger_binding"] = freeze_ledger_binding(ledger_resolved)
    config["identity_map_sha256"] = IDENTITY_MAP_SHA256
    config["official_query_pair_order"] = "identity_map_sorted_query_pair_ids"

    groups: dict[str, dict[str, Any]] = {}
    if host_matrix_root is not None:
        groups = _stage_host_matrix(matrix_root=host_matrix_root, plan_root=plan_root, output_dir=output_dir)
        config["host_query_groups"] = {
            key: {**value, "config_path": f"formal-query-{value['method_id']}-{value['arm_id']}.json"}
            for key, value in groups.items()
        }

    config_path = output_dir / "formal-run-config.json"
    with config_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    readiness = verify_formal_run_config(config_path)
    receipt = {
        "schema": FREEZE_RECEIPT_SCHEMA,
        "status": "READY" if readiness.formal_execution_allowed else "NOT_READY",
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "operation_count": len(operations),
        "readiness": {
            "status": readiness.status,
            "reasons": list(readiness.reasons),
            "details": {key: value for key, value in readiness.details.items() if key != "operations"},
        },
    }
    # Each group config is a direct input to run_host_queries(run=False).  It
    # carries exactly the 80 operations represented by its staged input and
    # binding map, while retaining the same immutable method/G2/ledger gates.
    for group_key, group in groups.items():
        group_config = dict(config)
        group_config["operations"] = {
            operation_id: operation for operation_id, operation in operations.items()
            if operation["unit"]["kind"] == "query"
            and operation["arm_id"] == group["arm_id"]
            and operation["host_id"] == group["method_id"]
            and operation_id.startswith(("hermes_a2a-", "codex_windows_desktop-"))
        }
        group_config["host_query_inputs"] = group["host_query_inputs"]
        group_config["host_query_bindings"] = group["host_query_bindings"]
        if group["method_id"] == HERMES_METHOD_ID:
            group_config["host_query_method_id"] = HERMES_METHOD_ID
        else:
            group_config.pop("host_query_method_id", None)
        group_config.pop("host_query_groups", None)
        group_config_path = output_dir / f"formal-query-{group['method_id']}-{group['arm_id']}.json"
        if group_config_path.exists():
            raise FreezeError(f"freeze_output_exists:{group_config_path}")
        group_config_path.write_bytes(_json_bytes(group_config))

    preflight_reasons: list[str] = []
    if not groups:
        preflight_reasons.append("host_query_matrix_missing")
    else:
        from p18_run_host_queries import HostQueryError, run_host_queries
        for group in groups.values():
            group_config_path = output_dir / f"formal-query-{group['method_id']}-{group['arm_id']}.json"
            probe_output = output_dir / f"TEST-query-preflight-{group['method_id']}-{group['arm_id']}"
            try:
                outcome = run_host_queries(formal_config_path=group_config_path, output_root=probe_output, run=False)
            except (HostQueryError, OSError, ValueError, KeyError) as exc:
                preflight_reasons.append(f"host_query_preflight_failed:{group['method_id']}/{group['arm_id']}:{exc}")
            else:
                if outcome.get("status") != "PREFLIGHT_ONLY" or outcome.get("query_conditions") != 80:
                    preflight_reasons.append(f"host_query_preflight_incomplete:{group['method_id']}/{group['arm_id']}")

    receipt["status"] = "READY" if readiness.formal_execution_allowed and not preflight_reasons else "NOT_READY"
    receipt["readiness"]["status"] = "READY" if receipt["status"] == "READY" else "NOT_READY"
    receipt["readiness"]["reasons"] = list(readiness.reasons) + preflight_reasons
    with (output_dir / "freeze-receipt.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze the P18 formal run config from sealed artifacts.")
    parser.add_argument("--plan-root", type=Path, required=True)
    parser.add_argument("--journey-bundle", type=Path, required=True)
    parser.add_argument("--journey-ids", type=str, required=True, help="comma-separated, e.g. J01,J02,...,J08")
    parser.add_argument("--protocol-artifact", type=Path, required=True)
    parser.add_argument("--method-artifact", type=Path, required=True)
    parser.add_argument("--hermes-method-artifact", type=Path)
    parser.add_argument("--candidate-receipt", type=Path, required=True)
    parser.add_argument("--candidate-wheel", type=Path, required=True)
    parser.add_argument("--candidate-source-root", type=Path, required=True)
    parser.add_argument("--candidate-git-root", type=Path)
    parser.add_argument("--source-commit", type=str, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True,
                        help="git-blob source manifest JSON produced via scripts/check.py machinery")
    parser.add_argument("--g2-report", type=Path, required=True)
    parser.add_argument("--g2-evidence", type=Path, nargs="*", default=[])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--host-matrix-root", type=Path,
                        help="prepared p18_prepare_host_matrix output with eight 80-condition groups")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        receipt = build_frozen_config(
            plan_root=args.plan_root,
            journey_bundle=args.journey_bundle,
            journey_ids=[item.strip() for item in args.journey_ids.split(",") if item.strip()],
            protocol_artifact=args.protocol_artifact,
            method_artifact=args.method_artifact,
            hermes_method_artifact=args.hermes_method_artifact,
            candidate_receipt=args.candidate_receipt,
            candidate_wheel=args.candidate_wheel,
            candidate_source_root=args.candidate_source_root,
            candidate_git_root=args.candidate_git_root,
            source_commit=args.source_commit,
            source_manifest=args.source_manifest,
            g2_report=args.g2_report,
            g2_evidence=list(args.g2_evidence),
            ledger=args.ledger,
            output_dir=args.output,
            host_matrix_root=args.host_matrix_root,
        )
    except FreezeError as exc:
        print(json.dumps({"status": "FREEZE_ERROR", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": receipt["status"], "operation_count": receipt["operation_count"],
                      "reasons": receipt["readiness"]["reasons"]}, ensure_ascii=False))
    return 0 if receipt["status"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
