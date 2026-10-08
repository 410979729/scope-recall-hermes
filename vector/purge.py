"""What a purge of the vector companions removes: the validated targets of one purge and, among a store's rows, the
ids of the governed ones.  One rule for every companion store (``store.LanceVectorStore`` and
``sqlite_store.SQLiteBruteForceVectorStore``): a row whose writer metadata cannot be read makes the whole inventory
unknown, and an unknown inventory is never acknowledged as empty."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

_PURGE_KINDS = frozenset({"event", "claim", "episode", "artifact", "reference"})
_PURGE_METADATA_KEYS = (
    "object_kind",
    "object_ref",
    "vector_id",
    "embedding_space",
    "agent_id",
    "installation_id",
    "logical_scope_id",
)


def purge_request(*, members, agent_id, installation_id, partitions):
    """The validated targets and governed partitions of one purge, for whichever store carries it out."""
    if not isinstance(agent_id, str) or not agent_id or not isinstance(installation_id, str) or not installation_id:
        raise ValueError("trusted purge identity required")
    targets = {(entry["kind"], entry["ref"]) for entry in members}
    if any(kind not in _PURGE_KINDS or not isinstance(ref, str) or not ref for kind, ref in targets):
        raise ValueError("invalid purge members")
    governed = {(entry["scope_id"], entry["embedding_space"]): entry["physical_scope_id"] for entry in partitions}
    return targets, governed


def governed_row_ids(
    rows: Iterable[dict[str, Any]],
    *,
    targets,
    governed,
    agent_id,
    installation_id,
    project_id,
    branch_id,
    check_budget: Callable[[], None] = lambda: None,
) -> list[str] | None:
    """Ids of the governed rows among ``rows``, or ``None`` when any row cannot be classified.

    One rule for every companion store: a row whose writer metadata cannot be
    read makes the whole inventory unknown, and an unknown inventory is never
    acknowledged as empty.
    """
    scopes = {scope for scope, _ in governed}
    matched: list[str] = []
    seen: set[str] = set()
    for row in rows:
        check_budget()
        row_id = row.get("id")
        if type(row_id) is not str or not row_id or row_id in seen:
            return None
        seen.add(row_id)
        metadata = _purge_metadata(row)
        if metadata is None:
            return None
        if (metadata["agent_id"], metadata["installation_id"]) != (agent_id, installation_id):
            continue
        if (metadata["object_kind"], metadata["object_ref"]) not in targets:
            continue
        if metadata["logical_scope_id"] not in scopes or (metadata["project_id"], metadata["branch_id"]) != (
            project_id,
            branch_id,
        ):
            continue
        partition = governed.get((metadata["logical_scope_id"], metadata["embedding_space"]))
        if partition is None or row["scope_id"] != partition:
            return None
        matched.append(row_id)
    return matched


def _purge_metadata(row: dict[str, Any]) -> dict[str, Any] | None:
    """The writer metadata of one row, or ``None`` when the row cannot be classified."""
    try:
        metadata = json.loads(row["target"])
        if any(type(metadata.get(key)) is not str or not metadata[key] for key in _PURGE_METADATA_KEYS):
            return None
        if metadata["object_kind"] not in _PURGE_KINDS:
            return None
        if type(metadata.get("object_revision")) is not int or metadata["object_revision"] < 1:
            return None
        for key in ("project_id", "branch_id"):
            if key not in metadata or (
                metadata[key] is not None and (type(metadata[key]) is not str or not metadata[key])
            ):
                return None
        if row["id"] != metadata["vector_id"] or row["source"] != metadata["object_ref"]:
            return None
        return metadata
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
