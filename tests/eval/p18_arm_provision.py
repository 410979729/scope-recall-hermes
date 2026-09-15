"""Offline P18 four-arm provision planning.

This module prepares reviewable, isolated arm/host plans without starting a
host, opening the sealed fixture, creating a Core database, or installing a
plugin.  The public fixture is copied into each plan entry so every arm has
its own source snapshot.  Arm B may also receive a read-only git archive of
the frozen 578b source; that archive is never installed or imported here.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Iterable, Mapping


BASELINE_578B = "578b955802df753f2e2208e26eab6f71971285a0"
HOSTS: tuple[str, ...] = ("hermes_a2a", "codex_windows_desktop")
ARMS: tuple[str, ...] = ("A", "B", "C", "D")
PUBLIC_FIXTURE = Path(__file__).with_name("public_fixture.jsonl")
CONTROL_FIELDS = frozenset(
    {
        "case_id",
        "case_index",
        "group_id",
        "core_class",
        "condition",
        "answerability",
        "required_facts",
        "prohibited_errors",
        "gold",
        "expected",
        "oracle",
        "control_only",
    }
)


class ArmProvisionError(ValueError):
    """Invalid public input or an unsafe provision-plan destination."""


def _safe_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    lowered = str(root).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise ArmProvisionError("formal F:\\Agents roots are forbidden")
    return root


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
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _load_public_fixture(path: Path) -> tuple[list[dict[str, Any]], str]:
    if not path.is_file():
        raise ArmProvisionError("public fixture is missing")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArmProvisionError(f"public fixture JSON error at line {number}") from exc
        if not isinstance(item, dict):
            raise ArmProvisionError(f"public fixture row {number} is not an object")
        forbidden = sorted(set(_walk_keys(item)) & CONTROL_FIELDS)
        if forbidden:
            raise ArmProvisionError("public fixture contains control/oracle fields: " + ",".join(forbidden))
        history = item.get("history")
        query = item.get("query")
        if not isinstance(history, list) or not isinstance(query, dict):
            raise ArmProvisionError(f"public fixture row {number} lacks history/query")
        if not isinstance(query.get("text"), str) or not query["text"].strip():
            raise ArmProvisionError(f"public fixture row {number} lacks query text")
        for event in history:
            if not isinstance(event, dict) or not isinstance(event.get("text"), str) or not event["text"].strip():
                raise ArmProvisionError(f"public fixture row {number} has invalid history")
        rows.append(item)
    if not rows:
        raise ArmProvisionError("public fixture is empty")
    return rows, _sha256(path)


def _archive_baseline(repo_root: Path, destination: Path) -> dict[str, Any]:
    if not repo_root.is_dir():
        raise ArmProvisionError("baseline repository root is missing")
    check = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{BASELINE_578B}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if check.returncode != 0:
        return {"status": "UNAVAILABLE", "baseline_ref": BASELINE_578B, "reason": "git_object_missing"}
    process = subprocess.run(
        ["git", "-C", str(repo_root), "archive", "--format=tar", BASELINE_578B],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if process.returncode != 0:
        return {
            "status": "UNAVAILABLE",
            "baseline_ref": BASELINE_578B,
            "reason": "git_archive_failed",
            "git_returncode": process.returncode,
        }
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(process.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")
    required = ("pyproject.toml", "plugin.yaml", "provider.py", "cli.py")
    missing = [name for name in required if not (destination / name).is_file()]
    pyproject = (destination / "pyproject.toml").read_text(encoding="utf-8") if not missing else ""
    version_match = re.search(r"^version\s*=\s*\"([^\"]+)\"", pyproject, re.MULTILINE)
    project_match = re.search(r"^name\s*=\s*\"([^\"]+)\"", pyproject, re.MULTILINE)
    plugin_text = (destination / "plugin.yaml").read_text(encoding="utf-8") if not missing else ""
    hooks = [line.strip()[2:] for line in plugin_text.splitlines() if line.strip().startswith("- ")]
    return {
        "status": "PASS" if not missing else "INCOMPLETE",
        "baseline_ref": BASELINE_578B,
        "archive_root": str(destination.resolve()),
        "source_sha256": _tree_sha256(destination),
        "required_files": required,
        "missing_files": missing,
        "project_name": project_match.group(1) if project_match else None,
        "project_version": version_match.group(1) if version_match else None,
        "install_entrypoint": "hermes-scope-recall = scope_recall.cli:main",
        "provider_entrypoint": "scope_recall.provider:ScopeRecallMemoryProvider",
        "plugin_hooks": hooks,
        "installed": False,
        "host_started": False,
    }


def _fixture_projection(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in rows:
        history = []
        for event in row["history"]:
            item = {
                "source_type": event["source_type"],
                "speaker_role": event["speaker_role"],
                "text": event["text"],
            }
            if isinstance(event.get("occurred_at"), str) and event["occurred_at"]:
                item["occurred_at"] = event["occurred_at"]
            history.append(item)
        projected.append({"history": history, "query": {"text": row["query"]["text"]}})
    return projected


def _host_plan(host_id: str, entry_root: Path, arm_id: str) -> dict[str, Any]:
    if host_id == "hermes_a2a":
        entry = "actual Hermes A2A gateway"
        tool = "official Hermes A2A memory/provider interface"
        adapter_status = "NOT_STARTED"
    elif host_id == "codex_windows_desktop":
        entry = "actual Codex Windows desktop UI input/send path"
        tool = "official Codex desktop memory interface"
        adapter_status = "NOT_STARTED"
    else:
        raise ArmProvisionError(f"unsupported host: {host_id}")
    return {
        "host_id": host_id,
        "arm_id": arm_id,
        "entry": entry,
        "tool": tool,
        "adapter_status": adapter_status,
        "network_calls": 0,
        "model_calls": 0,
        "host_started": False,
        "isolated_test_root": str(entry_root.resolve()),
        "planned_storage": {
            "database_path": str((entry_root / "planned-host-database.sqlite3").resolve()),
            "database_created": False,
            "core_schema_created": False,
            "claims_created": False,
        },
    }


def _arm_details(arm_id: str, host_id: str, entry_root: Path, baseline: dict[str, Any] | None) -> dict[str, Any]:
    if arm_id == "A":
        return {
            "configuration": "host native memory enabled; Scope Recall disabled",
            "tools": ["host_native_memory_tool"],
            "storage": "host-native memory store selected by host at execution",
            "code_source": {"status": "HOST_PROVIDED_AT_EXECUTION", "sha256": None},
            "status": "PENDING_HOST_NATIVE_BINDING",
            "reason": "actual host native memory binding is not configured in offline preparation",
        }
    if arm_id == "B":
        if host_id == "codex_windows_desktop":
            return {
                "configuration": "exact 578b Hermes plugin baseline",
                "tools": ["hermes_scope_recall_plugin"],
                "storage": "isolated Hermes plugin store, only after explicit install",
                "code_source": baseline,
                "status": "UNSUPPORTED",
                "reason": "codex_native_does_not_support_hermes_plugin_baseline",
            }
        return {
            "configuration": "exact 578b Hermes plugin baseline",
            "tools": ["hermes-scope-recall CLI", "scope_recall.provider:ScopeRecallMemoryProvider"],
            "storage": "isolated Hermes plugin store, only after explicit install",
            "code_source": baseline,
            "status": "PENDING_ISOLATED_INSTALL",
            "reason": "source archive verified; installation and actual Hermes host binding remain unrun",
        }
    if arm_id == "C":
        return {
            "configuration": "current candidate after independent source freeze",
            "tools": ["candidate official adapter, to be frozen by root"],
            "storage": "isolated candidate store, only after source freeze/install",
            "code_source": {"status": "CANDIDATE_FREEZE_REQUIRED", "sha256": None},
            "status": "PENDING_CANDIDATE_FREEZE",
            "reason": "candidate source manifest was not supplied to this offline plan",
        }
    if arm_id == "D":
        return {
            "configuration": "raw archive plus deterministic simple literal search",
            "tools": ["JSONL archive reader", "deterministic literal search"],
            "storage": "per-entry raw-archive.jsonl and no Core database",
            "code_source": {"status": "THIS_OFFLINE_PLANNER_ONLY", "sha256": _sha256(Path(__file__))},
            "status": "PASS",
            "reason": "offline archive and simple-search snapshot can be prepared without a host",
        }
    raise ArmProvisionError(f"unsupported arm: {arm_id}")


def provision_public_arm_plan(
    run_root: str | Path,
    *,
    repo_root: str | Path,
    fixture_path: str | Path = PUBLIC_FIXTURE,
    hosts: tuple[str, ...] = HOSTS,
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    """Create an isolated, no-host/no-network provision plan for public data."""

    root = _safe_root(run_root)
    if root.exists() and any(root.iterdir()):
        raise ArmProvisionError("provision root must be new and empty")
    if not hosts or not arms or any(host not in HOSTS for host in hosts) or any(arm not in ARMS for arm in arms):
        raise ArmProvisionError("unsupported host or arm selection")
    repo = Path(repo_root).expanduser().resolve()
    fixture = Path(fixture_path).expanduser().resolve()
    rows, fixture_sha = _load_public_fixture(fixture)
    root.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    for host_id in hosts:
        for arm_id in arms:
            entry_root = root / host_id / f"arm-{arm_id}"
            input_root = entry_root / "input"
            input_root.mkdir(parents=True, exist_ok=False)
            input_path = input_root / "public_fixture.jsonl"
            shutil.copyfile(fixture, input_path)
            baseline = None
            if arm_id == "B":
                baseline = _archive_baseline(repo, entry_root / "source" / "baseline-578b")
            if arm_id == "D":
                archive_path = entry_root / "archive" / "raw-archive.jsonl"
                archive_path.parent.mkdir(parents=True, exist_ok=False)
                projection = _fixture_projection(rows)
                archive_path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in projection),
                    encoding="utf-8",
                    newline="\n",
                )
                literal_matches = sum(
                    1
                    for row in projection
                    if row["query"]["text"].casefold()
                    in " ".join(event["text"] for event in row["history"]).casefold()
                )
            else:
                archive_path = None
                literal_matches = None
            plan = {
                "schema": "scope-recall.p18.arm-provision-entry.v1",
                "host_id": host_id,
                "arm_id": arm_id,
                "host": _host_plan(host_id, entry_root, arm_id),
                "arm": _arm_details(arm_id, host_id, entry_root, baseline),
                "input": {
                    "source": "public_fixture.jsonl",
                    "path": str(input_path.resolve()),
                    "sha256": _sha256(input_path),
                    "rows": len(rows),
                    "control_fields": False,
                },
                "storage": {
                    "database_created": False,
                    "core_schema_created": False,
                    "claims_created": False,
                    "archive_path": str(archive_path.resolve()) if archive_path else None,
                    "archive_sha256": _sha256(archive_path) if archive_path else None,
                    "literal_query_matches": literal_matches,
                },
                "fairness": {
                    "same_public_input_digest": fixture_sha,
                    "cross_arm_database_handoff": False,
                    "prewritten_claims_or_answers": False,
                    "oracle_or_gold_input": False,
                },
            }
            (entry_root / "arm-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            entries.append(plan)
    source_hashes = {
        "public_fixture": fixture_sha,
        "provision_module": _sha256(Path(__file__)),
    }
    baseline_entries = [entry["arm"]["code_source"] for entry in entries if entry["arm_id"] == "B"]
    return {
        "schema": "scope-recall.p18.arm-provision-plan.v1",
        "status": "PREPARATION_PASS",
        "formal_execution_started": False,
        "network_calls": 0,
        "model_calls": 0,
        "host_started": False,
        "sealed_raw_or_gold_opened": False,
        "entry_count": len(entries),
        "hosts": list(hosts),
        "arms": list(arms),
        "all_roots_distinct": len({entry["host"]["isolated_test_root"] for entry in entries}) == len(entries),
        "all_input_hashes_equal": len({entry["input"]["sha256"] for entry in entries}) == 1,
        "all_entries_without_core_schema": all(not entry["storage"]["core_schema_created"] for entry in entries),
        "source_hashes": source_hashes,
        "baseline_snapshots": baseline_entries,
        "entries": entries,
        "remaining_real_dependencies": [
            "Hermes isolated TEST gateway and per-arm host bindings",
            "Codex actual Windows desktop driver and fresh TEST homes",
            "Root-frozen candidate source manifest for arm C",
            "Explicit arm A native-memory host binding",
            "Formal evaluator authorization, sealed fixture access and shared ledger freeze",
        ],
    }


def load_public_fixture(path: str | Path = PUBLIC_FIXTURE) -> tuple[list[dict[str, Any]], str]:
    """Read the bounded public fixture with the same control-field checks as planning."""
    return _load_public_fixture(Path(path).expanduser().resolve())


__all__ = [
    "ARMS",
    "BASELINE_578B",
    "HOSTS",
    "ArmProvisionError",
    "load_public_fixture",
    "provision_public_arm_plan",
]
