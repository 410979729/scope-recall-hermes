"""The v1.1 tool JSON boundary both host surfaces share.

Host adapters translate requests and bind trusted identity.  The bounded JSON
contract, the reply envelope and the memory-epoch delivery fence are the same
on every host, so they live here once and never construct identity.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, is_dataclass
from datetime import datetime, tzinfo
import json
from pathlib import Path
import re
from typing import Any, Callable, cast
import uuid

from scope_recall.contracts import ContractError

PROTOCOL_VERSION = "1.1"
MAX_CONTENT = 8192
MAX_REFS = 32
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 32
#: Every rendered/body surface a raced stale packet must not carry.
_CONTENT_SURFACES = frozenset({
    "canonical_text", "context", "rendered", "additionalContext", "injection_text", "text", "body", "content",
})

#: What each read view still has to carry once the epoch fence empties it.
FENCED_RECALL: dict[str, Any] = {"items": []}
FENCED_PROFILE: dict[str, Any] = {
    "resolved_subject": None,
    "alias_resolution": "none",
    "sections": {"facts": [], "preferences": [], "constraints": [], "decisions": [], "pending_intentions": []},
    "disputed": [],
}
FENCED_ENTITY: dict[str, Any] = {"resolved_subject": None, "alias_resolution": "none", "statements": []}

#: An instant as the contracts store it: always UTC.
_UTC_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", re.ASCII)
#: Time fields besides the ``*_at`` ones.
_TIME_KEYS = frozenset({"as_of", "valid_from", "valid_to"})


def strict_object(value: object, *, allowed: frozenset[str], required: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Apply the bounded v1.1 JSON contract to an untyped host object before Core sees it."""
    if type(value) is not dict:
        raise ContractError("INPUT_INVALID", "object")
    keys = frozenset(value)
    if not keys <= allowed:
        raise ContractError("INPUT_INVALID", "unknown_field")
    if not required <= keys:
        raise ContractError("INPUT_INVALID", "required_field")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ContractError("INPUT_INVALID", "size")
        _walk(value)
        # The round trip gives the host object the same bounded JSON semantics
        # as the public contracts and strips any custom object the host passed.
        decoded = json.loads(encoded)
    except ContractError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise ContractError("INPUT_INVALID", "json_value") from None
    if type(decoded) is not dict:
        raise ContractError("INPUT_INVALID", "object")
    return decoded


def _walk(item: object, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ContractError("INPUT_INVALID", "nesting")
    if type(item) in (str, int, float, bool) or item is None:
        return
    if type(item) is list:
        for child in item:
            _walk(child, depth + 1)
        return
    if type(item) is dict:
        for key, child in item.items():
            if type(key) is not str:
                raise ContractError("INPUT_INVALID", "json_key")
            _walk(child, depth + 1)
        return
    raise ContractError("INPUT_INVALID", "json_value")


def check_protocol(payload: dict[str, Any]) -> None:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ContractError("INPUT_INVALID", "protocol_version")


def request_id(payload: dict[str, Any], *, prefix: str) -> str:
    """The caller's request id, or a fresh ``prefix:hex`` one when it sent none."""
    value = payload.get("request_id")
    if value is None:
        return f"{prefix}:{uuid.uuid4().hex}"
    if type(value) is not str or not 1 <= len(value) <= 100:
        raise ContractError("INPUT_INVALID", "request_id")
    return value


def revision_ref(value: object) -> tuple[str, int | None]:
    """Split ``ref@revision`` into its parts; a bare ref means the current revision."""
    if type(value) is not str or not 1 <= len(value) <= 240:
        raise ContractError("INPUT_INVALID", "ref")
    if "@" not in value:
        return value, None
    ref, raw = value.rsplit("@", 1)
    try:
        revision = int(raw)
    except ValueError:
        raise ContractError("INPUT_INVALID", "ref") from None
    if not ref or revision < 1 or raw != str(revision):
        raise ContractError("INPUT_INVALID", "ref")
    return ref, revision


def json_value(value: object) -> object:
    """Convert Core dataclasses, tuples and paths into plain JSON values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {str(key): json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def local_times(value: object, zone: tzinfo | None = None) -> object:
    """``value`` with every stored UTC instant written in ``zone``, offset included.

    Storage and the contracts keep UTC.  A model shown ``06:52:03+00:00``, whose
    prompt gave it only the date and its zone, told the user it was 6:52 in the
    morning when the host's clock read 2:52; the same instant in the host's own
    zone needs no arithmetic.  ``None`` is this machine's zone.  Only time fields
    change, never what a memory says; an instant the zone cannot place stays UTC.
    """
    if isinstance(value, dict):
        return {key: _local_instant(item, zone) if key in _TIME_KEYS or key.endswith("_at") else local_times(item, zone)
                for key, item in value.items()}
    if isinstance(value, list):
        return [local_times(item, zone) for item in value]
    return value


def _local_instant(value: object, zone: tzinfo | None) -> object:
    if type(value) is not str or not _UTC_INSTANT.fullmatch(value):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone).isoformat()
    except (ValueError, OverflowError, OSError):
        # Windows cannot place an instant before 1970 in the machine's zone.
        return value


def envelope(request_id: str, result: object, *, origin: str, capability_gaps: tuple[str, ...] = (),
             zone: tzinfo | None = None) -> dict[str, Any]:
    """The bounded v1.1 reply, times in the host's ``zone``; oversized or unserializable results fail as OUTPUT_LIMIT."""
    converted = local_times(json_value(result), zone)
    try:
        encoded = json.dumps(converted, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ContractError("OUTPUT_LIMIT", "result") from None
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ContractError("OUTPUT_LIMIT", "result")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "origin": origin,
        "capability_gaps": list(capability_gaps),
        "result": converted,
    }


def scrub_no_content(value: object) -> object:
    """Blank every rendered/body surface of a packet, recursively."""
    if isinstance(value, dict):
        return {key: ("" if key in _CONTENT_SURFACES else scrub_no_content(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_no_content(item) for item in value]
    return value


def fence_epoch(view: dict[str, Any], current_epoch: object, empty: dict[str, Any], *,
                retracted: Callable[[int], bool] | None = None) -> dict[str, Any]:
    """Never deliver a view compiled before a withdrawal it may hold.

    A deletion can race the read-only compiler.  Rather than hand a host a
    stale packet, blank its content, mark it unavailable at the current epoch
    and tell the caller to retry; ``empty`` restores the view's own required
    keys so hosts can still read the fenced shape.  ``retracted`` answers
    whether a deletion or suppression in the caller's scopes came after the
    view's epoch: every capture moves the epoch, so without it every move
    blanks, which on a store several entries write to was most views.
    """
    if view.get("memory_epoch") is None or view["memory_epoch"] == current_epoch:
        return view
    if retracted is not None and type(view["memory_epoch"]) is int and not retracted(view["memory_epoch"]):
        return view
    fenced = cast(dict[str, Any], scrub_no_content(view))
    fenced.update(
        status="unavailable",
        memory_epoch=current_epoch,
        **copy.deepcopy(empty),
        gaps=[*fenced.get("gaps", []), "memory_epoch_changed_before_delivery"],
        answerability="unknown",
        coverage="unknown",
        unmet_needs=[*fenced.get("unmet_needs", []), "retry_against_current_epoch"],
    )
    return fenced
