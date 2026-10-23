"""Validate complete content-free mirror records before recovery writes any files."""

from __future__ import annotations

import json
from collections.abc import Set

from .delete_storage import OBJECT_TABLES

DELETION_MIRROR_FORMAT = "scope-recall-deletion-mirror/1"

_MEMBER = {"object_kind", "object_ref", "scope_id", "project_id", "branch_id"}
_OPERATION = {
    "operation_id",
    "request_sha256",
    "mode",
    "scope_ids_json",
    "project_id",
    "branch_id",
    "requested_refs_json",
    "expected_revisions_json",
    "created_at",
    "memory_epoch",
    "layers_json",
    "active_content_removed",
}
_GROUP = {"group_sha256", "scope_id", "project_id", "branch_id", "read_blocked", "suppressed", "operation_id"}
_ABSENCE = _MEMBER | {"checkpoint_sha256", "reason"}


def _shape(value, fields: set[str], *, nullable: Set[str] = frozenset(), numeric: Set[str] = frozenset()) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid deletion mirror payload shape")
    for field in fields - numeric:
        if field in nullable and value[field] is None:
            continue
        if type(value[field]) is not str or not value[field]:
            raise ValueError("invalid deletion mirror field")
    if any(type(value[field]) is not int or not 0 <= value[field] < 9223372036854775807 for field in numeric):
        raise ValueError("invalid deletion mirror counter")


def _operation(item: dict) -> None:
    operation = item.get("operation")
    if not isinstance(operation, dict):
        raise ValueError("invalid deletion mirror operation")
    _shape(
        operation, _OPERATION, nullable={"project_id", "branch_id"}, numeric={"memory_epoch", "active_content_removed"}
    )
    if operation["mode"] != "delete" or operation["operation_id"] != item["record_id"]:
        raise ValueError("invalid deletion mirror operation identity")
    scopes = json.loads(operation["scope_ids_json"])
    refs = json.loads(operation["requested_refs_json"])
    revisions = json.loads(operation["expected_revisions_json"])
    layers = json.loads(operation["layers_json"])
    if (
        type(scopes) is not list
        or not scopes
        or any(type(scope) is not str or not scope for scope in scopes)
        or type(refs) is not list
        or any(type(ref) is not str for ref in refs)
        or type(revisions) is not dict
        or any(type(v) is not int or v < 1 for v in revisions.values())
        or type(layers) is not dict
        or operation["active_content_removed"] not in (0, 1)
    ):
        raise ValueError("invalid deletion mirror operation payload")
    members = item.get("members")
    if type(members) is not list:
        raise ValueError("invalid deletion mirror members")
    for member in members:
        _shape(member, _MEMBER, nullable={"project_id", "branch_id"})
        if member["object_kind"] not in OBJECT_TABLES or member["scope_id"] not in scopes:
            raise ValueError("invalid deletion mirror member")


def validate_mirror_record(item: dict) -> None:
    """Require the exact payload needed by replay, including its scope identities."""
    kind = item["kind"]
    common = {"format", "installation_id", "kind", "record_id"}
    extra = {"operation", "members"} if kind == "operation" else (set() if kind == "identity" else {"data"})
    if set(item) != common | extra:
        raise ValueError("invalid deletion mirror record shape")
    if kind == "identity":
        if item["record_id"] != item["installation_id"]:
            raise ValueError("invalid deletion mirror identity")
    elif kind == "operation":
        try:
            _operation(item)
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("invalid deletion mirror operation") from exc
    elif kind == "source_group":
        data = item["data"]
        _shape(data, _GROUP, nullable={"project_id", "branch_id"}, numeric={"read_blocked", "suppressed"})
        if data["group_sha256"] != item["record_id"] or data["read_blocked"] != 1 or data["suppressed"] != 1:
            raise ValueError("invalid deletion mirror source group")
    else:
        data = item["data"]
        _shape(data, _ABSENCE, nullable={"project_id", "branch_id"})
        if data["object_kind"] != "claim":
            raise ValueError("invalid deletion mirror absence")


def decode_mirror_record(line: bytes) -> dict:
    """Decode one complete line with a valid envelope and replayable payload."""
    item = json.loads(line)
    if (
        type(item) is not dict
        or item.get("format") != DELETION_MIRROR_FORMAT
        or type(item.get("kind")) is not str
        or item.get("kind") not in {"identity", "operation", "source_group", "absence"}
        or not all(isinstance(item.get(k), str) and item[k] for k in ("installation_id", "record_id"))
    ):
        raise ValueError("invalid deletion mirror record")
    validate_mirror_record(item)
    return item
