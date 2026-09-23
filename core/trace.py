"""Read-only, bounded evidence paths composed from the existing entity view.

No graph database, new authority, model call, or inferred edge lives here.
Names are exact scoped subjects; only the existing admitted alias resolver may
resolve a name. Each path stays in one authorized scope and one frozen time.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import time
from types import SimpleNamespace

from ..contracts import ContractError, validate_model_request, validate_payload
from .read_views import read_entity

TRACE_GUIDANCE = (
    "Read-only evidence paths for questions connecting people, projects and facts. "
    "Use ordinary recall/entity for direct questions; use trace when a relation chain is needed. "
    "Default two hops, maximum three. Exact names only; no identity inference or cross-scope joins. "
    "Paths are supporting evidence, never new facts. Missing paths do not prove no relationship exists."
)


def trace_tool_schema():
    schema = json.loads(
        (
            Path(__file__).parents[1] / "contracts" / "trace_request.schema.json"
        ).read_text(encoding="utf-8")
    )
    schema.pop("$schema", None)
    schema.pop("title", None)
    schema["required"] = ["protocol_version", "subject"]
    return {"name": "trace", "description": TRACE_GUIDANCE, "parameters": schema}


class _DeadlineStorage:
    """Only the read port needed by entity; reuse its release/visibility fences."""

    def __init__(self, storage, deadline):
        self.storage, self.deadline = storage, deadline

    @contextmanager
    def read(self, context, *, remaining_seconds=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        with self.storage.read(
            context, remaining_seconds=min(remaining, remaining_seconds or remaining)
        ) as tx:
            yield tx


def _node(scope: str, subject: str) -> dict:
    key = hashlib.sha256(
        json.dumps([scope, subject], ensure_ascii=False).encode()
    ).hexdigest()
    return {"id": "entity:" + key, "label": subject}


def read_trace(storage, clock, context, request, *, seconds: float = 5.0) -> dict:
    if type(request) is not dict:
        raise ContractError("INPUT_INVALID", "object")
    body = {
        "max_hops": 2,
        "max_nodes": 24,
        "max_paths": 12,
        "budget_bytes": 16384,
        "direction": "both",
        **request,
    }
    validate_model_request("trace_request", body, context)
    if type(seconds) not in (int, float) or not 0 < seconds <= 5:
        raise ValueError("trace deadline must be between zero and five seconds")
    deadline = time.monotonic() + seconds
    bounded = _DeadlineStorage(storage, deadline)
    frozen = SimpleNamespace(utc_now=lambda: now)
    now = clock.utc_now()
    with bounded.read(context) as tx:
        epoch = tx.status().memory_epoch
    result = dict(
        protocol_version="1.1",
        request_id=body["request_id"],
        status="ok",
        memory_epoch=epoch,
        as_of=now,
        paths=[],
        gaps=[],
        coverage="bounded",
        answerability="unknown",
        visited_nodes=0,
        truncated=False,
        budget_unit="utf8_bytes",
        facts_written=0,
    )
    queue = deque()
    # Do not cross-join identical strings belonging to different audiences.
    scopes = sorted(context.allowed_scope_ids)
    for scope in scopes[: body["max_nodes"]]:
        queue.append((scope, body["subject"], [], [], frozenset()))
    gaps = set()
    if len(scopes) > body["max_nodes"]:
        gaps.add("node_limit")
    cache = {}
    candidates = []
    path_keys = set()
    # Both unique reads and expanded paths are capped; cycles/diamonds cannot
    # evade the work bound merely by reusing an already read node.
    expansions = 0
    try:
        while queue:
            if expansions >= body["max_nodes"]:
                gaps.add("node_limit")
                break
            scope, subject, nodes, edges, seen = queue.popleft()
            expansions += 1
            if time.monotonic() >= deadline:
                raise TimeoutError
            cache_key = (scope, subject)
            view = cache.get(cache_key)
            if view is None:
                scoped = replace(context, allowed_scope_ids=frozenset({scope}))
                view = read_entity(
                    bounded,
                    frozen,
                    scoped,
                    {
                        "protocol_version": "1.1",
                        "request_id": body["request_id"],
                        "subject": subject,
                        "action": "related",
                        "direction": body["direction"],
                        "max_items": 30,
                        "budget_tokens": 8000,
                    },
                )
                cache[cache_key] = view
            if view.get("memory_epoch") != epoch or view["status"] == "unavailable":
                return _unavailable(result, "memory_changed")
            if view.get("alias_resolution") == "ambiguous":
                gaps.add("ambiguous_entity")
                continue
            if view.get("truncated") or view.get("scan_capped"):
                gaps.add("edge_limit")
            canonical = view.get("resolved_subject") or subject
            if canonical in seen:
                continue
            current_nodes = nodes + [_node(scope, canonical)]
            current_seen = seen | {canonical, subject}
            for edge in view.get("statements", []):
                if (
                    edge["kind"] != "fact"
                    or edge["claim_state"] != "active"
                    or edge["temporal_status"] != "current"
                ):
                    continue
                if edge.get("conditions"):
                    gaps.add("conditional_relation_not_traversed")
                    continue
                next_subject = (
                    edge["value_text"]
                    if edge["direction"] == "outgoing"
                    else edge["subject"]
                )
                if not next_subject.strip():
                    continue
                if next_subject in current_seen:
                    continue
                next_edges = edges + [edge]
                path = {
                    "nodes": current_nodes + [_node(scope, next_subject)],
                    "edges": next_edges,
                    "hops": len(next_edges),
                    "basis": "recorded_relations",
                }
                key = tuple(
                    (e["ref"], e["revision"], e["direction"]) for e in next_edges
                )
                if key not in path_keys and (
                    not body.get("target") or next_subject == body["target"]
                ):
                    path_keys.add(key)
                    candidates.append(path)
                # A long attribute can be a terminal answer (e.g. a recorded
                # requirement), but cannot be a subject under the Core schema.
                expandable = (
                    len(next_edges) < body["max_hops"] and len(next_subject) <= 240
                )
                if expandable and len(queue) < body["max_nodes"]:
                    queue.append(
                        (scope, next_subject, current_nodes, next_edges, current_seen)
                    )
                elif expandable:
                    gaps.add("node_limit")
    except TimeoutError:
        gaps.add("deadline")
    # Each step is already released by entity. A final epoch fence rejects a
    # deletion/revision racing any earlier hop; stale prefixes are never sent.
    with storage.read(
        context, remaining_seconds=max(0.001, deadline - time.monotonic())
    ) as tx:
        if tx.status().memory_epoch != epoch:
            return _unavailable(result, "memory_changed")
    result["visited_nodes"] = len(cache)
    candidates.sort(key=lambda p: (-p["hops"], tuple(e["ref"] for e in p["edges"])))
    result["paths"] = candidates[: body["max_paths"]]
    if len(candidates) > body["max_paths"]:
        gaps.add("path_limit")
    if not candidates:
        gaps.add("no_supported_path")
    result.update(
        gaps=sorted(gaps),
        truncated=bool(gaps & {"edge_limit", "node_limit", "path_limit", "deadline"}),
    )
    if result["truncated"]:
        result["status"] = "partial"
    if result["paths"]:
        result["answerability"] = "supported_paths_only"
    while (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        > body["budget_bytes"]
    ):
        if not result["paths"]:
            raise ContractError("INPUT_INVALID", "budget_bytes")
        result["paths"].pop()
        result.update(status="partial", truncated=True)
        result["gaps"] = sorted(set(result["gaps"]) | {"byte_limit"})
        if not result["paths"]:
            result["answerability"] = "unknown"
    validate_payload("trace_view", result)
    return result


def _unavailable(result, gap):
    result.update(
        status="unavailable",
        paths=[],
        gaps=[gap],
        coverage="unknown",
        answerability="unknown",
    )
    return result


def fence_trace_epoch(view, current_epoch, *, retracted=None):
    """Shared host-delivery fence; adapters do not invent visibility policy.

    ``retracted(epoch)`` says whether a deletion or suppression in the caller's scopes came after
    the view's epoch; any other move of the epoch is a capture, which withdraws nothing.
    """
    if view["memory_epoch"] == current_epoch:
        return view
    if retracted is not None and type(view["memory_epoch"]) is int and not retracted(view["memory_epoch"]):
        return view
    return _unavailable(
        dict(view, memory_epoch=current_epoch), "memory_changed_before_delivery"
    )
