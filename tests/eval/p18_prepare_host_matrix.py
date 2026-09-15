"""Prepare one isolated host binding per frozen P18 paired condition.

This is a caller around the existing :mod:`p18_host_arm_binding` factory.  It
does not start a host, import a plugin, call an API, or create Core claims.  A
normal invocation materializes immutable per-condition ``arm-plan.json`` and
the D archive input.  ``--prepare`` then calls the existing factory for every
unit; the default is intentionally only a preparation manifest so the final
candidate/method freeze remains an explicit gate.

The sealed planner and the host-query executor deliberately have different
row contracts.  Planner rows contain only the allowlisted ``model_input`` and
``source_seed_then_new_session_query``; executor rows additionally carry
``source_records`` and use ``imported_history_then_new_session_query``.  The
planner path below joins only the private event identity metadata needed to
materialize executor inputs; it never adds that identity to ``model_input``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping, Sequence


FROZEN_HERMES_COMMIT = "79445a496c86a19332ad786494b8384d2167e2d0"
HERMES_METHOD = "hermes_cli_local_input_v1"
CODEX_METHOD = "codex_windows_desktop"
HOST_METHODS = {"hermes_a2a": HERMES_METHOD, "codex_windows_desktop": CODEX_METHOD}
ARMS = ("A", "B", "C", "D")
_BASELINE_578B = "578b955802df753f2e2208e26eab6f71971285a0"
_PLANNER_SOURCE_SEQUENCE = "source_seed_then_new_session_query"
_EXECUTOR_SOURCE_SEQUENCE = "imported_history_then_new_session_query"


class HostMatrixError(ValueError):
    """Fail-closed matrix preparation error."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _safe_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise HostMatrixError("formal F:\\Agents root is forbidden")
    if "test" not in lowered:
        raise HostMatrixError("TEST output root is required")
    return root


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _manifest_sha256(path: Path) -> str:
    """Hash the manifest payload without its self-referential digest field."""

    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop("manifest_sha256", None)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_c_native_vector_path(manifest: Mapping[str, Any], arm_id: str) -> str | None:
    """Run the installed candidate's existing native path preflight for C."""
    if arm_id != "C":
        return None
    roots = manifest.get("roots")
    runtime_path = roots.get("runtime_config_path") if isinstance(roots, Mapping) else None
    if not isinstance(runtime_path, str) or not runtime_path.strip():
        return None
    config_path = Path(runtime_path).resolve()
    if not config_path.is_file():
        raise HostMatrixError("C runtime vector config is missing")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    vector = payload.get("vector")
    if not isinstance(vector, Mapping):
        raise HostMatrixError("C runtime vector config is missing")
    storage_dir = vector.get("storage_dir")
    table_name = vector.get("table_name")
    dimensions = vector.get("dimensions")
    if not isinstance(storage_dir, str) or not isinstance(table_name, str) or type(dimensions) is not int:
        raise HostMatrixError("C runtime vector config is incomplete")
    from scope_recall.lance_process_store import ProcessLanceVectorStore
    error = ProcessLanceVectorStore(Path(storage_dir), table_name=table_name, dimensions=dimensions).native_path_error()
    if error is not None:
        raise HostMatrixError(error)
    return "checked"


def _unit_rows(plan_root: Path, host_id: str, arm_id: str) -> list[dict[str, Any]]:
    path = plan_root / "units" / host_id / f"{arm_id}.jsonl"
    if not path.is_file():
        raise HostMatrixError(f"sealed host units missing: {host_id}")
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [row for row in rows if isinstance(row, dict) and row.get("kind") == "host_query"]
    if len(selected) != 80:
        raise HostMatrixError(f"expected 80 host-query units for {host_id}")

    planner_fields = {"unit_id", "kind", "ordinal", "source_sequence", "model_input"}
    executor_fields = planner_fields | {"source_records"}
    if all(set(row) == executor_fields for row in selected):
        seen: set[str] = set()
        for row in selected:
            unit_id = str(row.get("unit_id", ""))
            if unit_id in seen or not unit_id:
                raise HostMatrixError("duplicate host-query unit id")
            seen.add(unit_id)
            if not isinstance(row.get("source_records"), list) or not isinstance(row.get("model_input"), dict):
                raise HostMatrixError("host-query natural source projection missing")
            if row.get("source_sequence") != _EXECUTOR_SOURCE_SEQUENCE:
                raise HostMatrixError("host-query source sequence mismatch")
        return selected
    if not all(set(row) == planner_fields for row in selected):
        raise HostMatrixError("host-query planner schema mismatch")

    association_path = plan_root / "private-association.json"
    try:
        association_payload = json.loads(association_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostMatrixError("sealed private association is missing or unreadable") from exc
    if not isinstance(association_payload, dict) or association_payload.get("visibility") != "independent_executor_private":
        raise HostMatrixError("sealed private association identity mismatch")
    associations: dict[str, Mapping[str, Any]] = {}
    raw_associations = association_payload.get("associations")
    if not isinstance(raw_associations, list):
        raise HostMatrixError("sealed private association rows missing")
    for association in raw_associations:
        if (not isinstance(association, Mapping) or association.get("kind") != "host_query"
                or association.get("host_id") != host_id or association.get("arm_id") != arm_id):
            continue
        unit_id = association.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id or unit_id in associations:
            raise HostMatrixError("sealed private association unit id invalid or duplicate")
        associations[unit_id] = association
    if len(associations) != 80:
        raise HostMatrixError("sealed private association must cover 80 host-query units")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in selected:
        unit_id = str(row.get("unit_id", ""))
        if unit_id in seen or not unit_id:
            raise HostMatrixError("duplicate host-query unit id")
        seen.add(unit_id)
        natural = row.get("model_input")
        if not isinstance(natural, dict) or set(natural) != {"history", "query"}:
            raise HostMatrixError("host-query natural source projection missing")
        history = natural.get("history")
        if not isinstance(history, list) or not history:
            raise HostMatrixError("host-query natural history missing")
        if row.get("source_sequence") != _PLANNER_SOURCE_SEQUENCE:
            raise HostMatrixError("host-query source sequence mismatch")
        association = associations.get(unit_id)
        if association is None:
            raise HostMatrixError("host-query private association missing")
        event_ids = association.get("event_ids")
        event_sequences = association.get("event_sequences")
        if (not isinstance(event_ids, list) or not isinstance(event_sequences, list)
                or len(event_ids) != len(history) or len(event_sequences) != len(history)):
            raise HostMatrixError("host-query private source identity mismatch")
        source_records: list[dict[str, Any]] = []
        for event, event_id, sequence in zip(history, event_ids, event_sequences, strict=True):
            if (not isinstance(event, Mapping) or not isinstance(event_id, str) or not event_id
                    or type(sequence) is not int or sequence < 1):
                raise HostMatrixError("host-query private source identity invalid")
            source_record = dict(event)
            source_record.update(event_id=event_id, sequence=sequence)
            source_records.append(source_record)
        normalized.append({**row, "source_records": source_records})
    return normalized


def _arm_plan(
    *,
    entry: Path,
    method_id: str,
    arm_id: str,
    source: Mapping[str, Any],
    history_path: Path,
    history_sha256: str,
    archive_path: Path | None,
    archive_sha256: str | None,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "schema": "scope-recall.p18.arm-provision-entry.v1",
        "host_id": method_id,
        "arm_id": arm_id,
        "host": {
            "host_id": method_id,
            "arm_id": arm_id,
            "entry": "existing p18_host_arm_binding factory",
            "network_calls": 0,
            "model_calls": 0,
            "host_started": False,
            "isolated_test_root": str(entry),
        },
        "arm": {
            "configuration": f"P18 arm {arm_id}; one isolated condition",
            "status": "PENDING_HOST_BINDING",
            "code_source": dict(source),
        },
        "input": {
            "source_records_path": str(history_path),
            "source_records_sha256": history_sha256,
            "source_sequence": _EXECUTOR_SOURCE_SEQUENCE,
            "control_fields": False,
        },
        "storage": {"database_created": False, "core_schema_created": False, "claims_created": False},
        "fairness": {
            "source_records_sha256": history_sha256,
            "cross_condition_database_handoff": False,
            "prewritten_claims_or_answers": False,
            "oracle_or_gold_input": False,
        },
    }
    if archive_path is not None:
        plan["storage"].update({"archive_path": str(archive_path), "archive_sha256": archive_sha256})
    return plan


def _hardlink_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def _copy_baseline(source_archive: Path, entry: Path, source_sha256: str) -> tuple[Path, str]:
    destination = entry / "source" / "baseline-578b"
    shutil.copytree(source_archive, destination, copy_function=_hardlink_or_copy)
    return destination, source_sha256


def prepare_host_query_matrix(
    *,
    plan_root: str | Path,
    output_root: str | Path,
    hermes_executable: str | Path,
    hermes_version: str,
    hermes_source_root: str | Path,
    hermes_source_commit: str = FROZEN_HERMES_COMMIT,
    codex_executable: str | Path | None = None,
    codex_version: str | None = None,
    candidate_wheel: str | Path | None = None,
    candidate_sha256: str | None = None,
    baseline_archive: str | Path | None = None,
    prepare: bool = False,
    factory: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prepare a binding map for 80 paired conditions per host/arm.

    ``prepare=False`` only writes each condition plan and the D raw archive.
    ``prepare=True`` delegates actual binding creation to the existing factory;
    no host process is started by either mode.
    """
    plan_dir = Path(plan_root).expanduser().resolve()
    output = _safe_root(output_root)
    if output.exists() and any(output.iterdir()):
        raise HostMatrixError("output root must be new and empty")
    output.mkdir(parents=True, exist_ok=False)
    hermes_exe = Path(hermes_executable).expanduser().resolve()
    if not hermes_exe.is_file():
        raise HostMatrixError("Hermes executable is missing")
    hermes_executable_sha256 = _sha256(hermes_exe)
    source_root = Path(hermes_source_root).expanduser().resolve()
    if hermes_source_commit != FROZEN_HERMES_COMMIT or not source_root.is_dir():
        raise HostMatrixError("frozen Hermes 79445 source is required")
    hermes_source_sha256 = _tree_sha256(source_root)
    if codex_executable is not None:
        codex_exe = Path(codex_executable).expanduser().resolve()
        if not codex_exe.is_file() or not codex_version:
            raise HostMatrixError("fixed Codex executable/version required")
        codex_executable_sha256 = _sha256(codex_exe)
    else:
        codex_exe = None
        codex_executable_sha256 = None
    if candidate_wheel is not None:
        wheel = Path(candidate_wheel).expanduser().resolve()
        if not wheel.is_file() or not candidate_sha256 or _sha256(wheel) != candidate_sha256:
            raise HostMatrixError("candidate wheel/hash mismatch")
    else:
        wheel = None
    baseline = Path(baseline_archive).expanduser().resolve() if baseline_archive is not None else None
    if baseline is not None and not baseline.is_dir():
        raise HostMatrixError("baseline 578b archive is missing")
    baseline_sha256 = _tree_sha256(baseline) if baseline is not None else None
    binding_map: dict[str, dict[str, Any]] = {}
    grouped_bindings: dict[str, dict[str, dict[str, Any]]] = {}
    seen_units: set[str] = set()
    plan_entries = 0
    for source_host in HOST_METHODS:
        method_id = HOST_METHODS[source_host]
        for arm_id in ARMS:
            group_key = f"{method_id}/{arm_id}"
            grouped_bindings[group_key] = {}
            rows = _unit_rows(plan_dir, source_host, arm_id)
            for condition_ordinal, row in enumerate(rows, start=1):
                unit_id = str(row["unit_id"])
                if unit_id in seen_units:
                    raise HostMatrixError("host-query unit id reused across host/arm groups")
                seen_units.add(unit_id)
                condition_entry = output / "conditions" / method_id / arm_id / unit_id
                condition_entry.mkdir(parents=True, exist_ok=False)
                method_tag = "h" if method_id == HERMES_METHOD else "c"
                entry = output / "bindings" / method_tag / arm_id / f"{condition_ordinal:02d}"
                entry.mkdir(parents=True, exist_ok=False)
                source = (
                    {"status": "HOST_NATIVE_AT_EXECUTION"}
                    if arm_id == "A"
                    else {"status": "FROZEN_BASELINE_578B", "baseline_ref": _BASELINE_578B}
                    if arm_id == "B"
                    else {"status": "FROZEN_CANDIDATE_WHEEL"}
                    if arm_id == "C"
                    else {"status": "ARCHIVE_SIMPLE_SEARCH"}
                )
                source_records = row["source_records"]
                history_path = condition_entry / "input" / "source-records.json"
                _write_json(history_path, {"history": source_records})
                history_sha = _sha256(history_path)
                archive_path = None
                archive_sha = None
                if arm_id == "D":
                    archive_path = entry / "input" / "archive.jsonl"
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    archive_path.write_text(
                        json.dumps({"history": source_records}, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    archive_sha = _sha256(archive_path)
                if arm_id == "B" and baseline is not None and prepare:
                    assert baseline_sha256 is not None
                    copied, copied_sha = _copy_baseline(baseline, entry, baseline_sha256)
                    source = {"status": "PASS", "baseline_ref": _BASELINE_578B, "archive_root": str(copied), "source_sha256": copied_sha}
                plan = _arm_plan(entry=entry, method_id=method_id, arm_id=arm_id, source=source,
                                 history_path=history_path, history_sha256=history_sha,
                                 archive_path=archive_path, archive_sha256=archive_sha)
                plan_path = entry / "arm-plan.json"
                _write_json(plan_path, plan)
                plan_entries += 1
                if not prepare:
                    details = {"status": "PLAN_ONLY", "method_id": method_id, "arm_id": arm_id,
                               "path": str(plan_path.relative_to(output)), "sha256": _sha256(plan_path)}
                    binding_map[unit_id] = details
                    grouped_bindings[group_key][unit_id] = details
                    continue
                if factory is None:
                    from p18_host_arm_binding import prepare_host_arm_binding
                    factory = prepare_host_arm_binding
                executable = hermes_exe if method_id == HERMES_METHOD else codex_exe
                if executable is None:
                    raise HostMatrixError("Codex executable required for Codex matrix")
                manifest = factory(
                    entry,
                    host_executable=executable,
                    host_version=hermes_version if method_id == HERMES_METHOD else str(codex_version),
                    host_executable_sha256=(
                        hermes_executable_sha256 if method_id == HERMES_METHOD else codex_executable_sha256
                    ),
                    host_source_root=source_root if method_id == HERMES_METHOD else None,
                    host_source_commit=hermes_source_commit if method_id == HERMES_METHOD else None,
                    host_source_sha256=hermes_source_sha256 if method_id == HERMES_METHOD else None,
                    candidate_wheel=wheel if arm_id == "C" else None,
                    candidate_sha256=candidate_sha256 if arm_id == "C" else None,
                    baseline_source_sha256=baseline_sha256 if arm_id == "B" else None,
                    host_context_id=f"TEST-P18-{method_id}-{arm_id}-{unit_id.rsplit('-query-', 1)[-1]}",
                )
                manifest_path = Path(str(manifest.get("binding_manifest_path", ""))).resolve()
                if not manifest_path.is_file() or not manifest_path.is_relative_to(entry):
                    raise HostMatrixError("factory returned binding outside condition entry")
                _check_c_native_vector_path(manifest, arm_id)
                details = {"status": manifest.get("status"), "method_id": method_id, "arm_id": arm_id,
                           "path": str(manifest_path.relative_to(output)), "sha256": _sha256(manifest_path)}
                binding_map[unit_id] = details
                grouped_bindings[group_key][unit_id] = details
    if _sha256(hermes_exe) != hermes_executable_sha256 or _tree_sha256(source_root) != hermes_source_sha256:
        raise HostMatrixError("frozen Hermes inputs changed during matrix preparation")
    if codex_exe is not None and _sha256(codex_exe) != codex_executable_sha256:
        raise HostMatrixError("fixed Codex executable changed during matrix preparation")
    if baseline is not None and _tree_sha256(baseline) != baseline_sha256:
        raise HostMatrixError("frozen baseline archive changed during matrix preparation")
    if plan_entries != 640 or len(grouped_bindings) != len(HOST_METHODS) * len(ARMS):
        raise HostMatrixError("host-query matrix must contain eight groups")
    group_maps: dict[str, dict[str, Any]] = {}
    for group_key, details_by_unit in grouped_bindings.items():
        if len(details_by_unit) != 80:
            raise HostMatrixError(f"host-query group must contain 80 bindings: {group_key}")
        refs: dict[str, dict[str, str]] = {}
        for unit_id, details in details_by_unit.items():
            artifact = (output / str(details["path"])).resolve()
            if not artifact.is_file() or not artifact.is_relative_to(output):
                raise HostMatrixError("binding artifact must remain under formal config root")
            refs[unit_id] = {"path": artifact.relative_to(output).as_posix(), "sha256": str(details["sha256"])}
        method_id, arm_id = group_key.split("/", 1)
        map_path = output / "host-query-bindings" / method_id / f"{arm_id}.json"
        _write_json(map_path, refs)
        group_maps[group_key] = {
            "method_id": method_id,
            "arm_id": arm_id,
            "unit_count": len(refs),
            "path": map_path.relative_to(output).as_posix(),
            "sha256": _sha256(map_path),
            "entry_schema": ["path", "sha256"],
        }
    result = {
        "schema": "scope-recall.p18-host-query-binding-matrix.v1",
        "status": "PREPARED" if prepare else "PLAN_ONLY",
        "formal_execution_started": False,
        "network_calls": 0,
        "model_calls": 0,
        "plan_root": str(plan_dir),
        "output_root": str(output),
        "methods": sorted({item["method_id"] for item in binding_map.values()}),
        "arms": list(ARMS),
        "conditions_per_host_arm": 80,
        "binding_count": len(binding_map),
        "plan_entry_count": plan_entries,
        "candidate_wheel_sha256": candidate_sha256,
        "frozen_hermes_commit": hermes_source_commit,
        "required_formal_config_root": str(output),
        "group_maps": group_maps,
        "bindings": binding_map,
    }
    summary_path = output / "matrix-summary.json"
    result["summary_path"] = str(summary_path)
    _write_json(summary_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--hermes-executable", required=True, type=Path)
    parser.add_argument("--hermes-version", required=True)
    parser.add_argument("--hermes-source-root", required=True, type=Path)
    parser.add_argument("--candidate-wheel", type=Path)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--baseline-archive", type=Path)
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--codex-version")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    result = prepare_host_query_matrix(**vars(args))
    print(json.dumps({key: value for key, value in result.items() if key != "bindings"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CODEX_METHOD", "HERMES_METHOD", "HostMatrixError", "prepare_host_query_matrix"]
