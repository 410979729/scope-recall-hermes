"""Apply an installation-qualified disaster mirror through the normal restore boundary."""

from __future__ import annotations

import json
from dataclasses import replace

from ..contracts import ContractError
from ..core.restore import (
    InstallationMaintenance,
    export_deletion_ledger,
    reconcile_recovery_scopes,
    replay_deletion_ledger,
    stage_recovery_ledger,
)
from ..core.storage import SQLiteStorage


def replay_deletion_mirror(storage, authority: InstallationMaintenance, records: list[dict]) -> dict:
    """Preserve snapshot suppression and queued input while adding later deletion constraints."""
    scopes = set(storage.binding.scope_ids)
    for item in records:
        if item["kind"] == "operation":
            scopes.update(json.loads(item["operation"]["scope_ids_json"]))
        elif item["kind"] in {"source_group", "absence"}:
            scopes.add(item["data"]["scope_id"])
    reconcile_recovery_scopes(storage, authority, frozenset(scopes))
    binding = replace(storage.binding, scope_ids=frozenset(scopes))
    storage = SQLiteStorage(binding)
    authority = InstallationMaintenance(
        replace(authority.context, binding=binding, allowed_scope_ids=binding.scope_ids)
    )
    ledger = export_deletion_ledger(storage, authority, restoring=True)
    operations = {item["operation"]["operation_id"]: item for item in ledger["operations"]}
    groups = {item["group_sha256"]: item for item in ledger["source_groups"]}
    absent = {(item["object_kind"], item["object_ref"]): item for item in ledger["absent_objects"]}
    new_count = 0
    latest = None
    for item in records:
        if item["kind"] == "operation":
            operation = item["operation"]
            if operation["operation_id"] != item["record_id"] or operation["mode"] != "delete":
                raise ContractError("RESTORE_UNVERIFIED", "mirror_operation")
            new_count += item["record_id"] not in operations
            operations[item["record_id"]] = {"operation": operation, "members": item["members"]}
            ledger["memory_epoch"] = max(ledger["memory_epoch"], operation["memory_epoch"])
            latest = max(latest or "", operation["created_at"])
        elif item["kind"] == "source_group":
            groups[item["data"]["group_sha256"]] = item["data"]
        elif item["kind"] == "absence":
            data = item["data"]
            absent[(data["object_kind"], data["object_ref"])] = data
    ledger.update(
        operations=list(operations.values()), source_groups=list(groups.values()), absent_objects=list(absent.values())
    )
    stage_recovery_ledger(storage, authority, ledger)
    result = replay_deletion_ledger(storage, authority, ledger, preserve_capture_inbox=True, reopen=False)
    return {
        "status": "replayed",
        "replayed_operations": new_count,
        "affected_objects": result.get("affected_objects", 0),
        "latest_mirrored_at": latest,
    }
