"""The content-release policy for stored items sent to an agent or model.

Local owner management uses the stored originals. All model-facing readers
withhold whole items, including nested evidence, and report only a count.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from typing import cast

from .retrieval import RetrievedObject
from .secret_patterns import SECRET_RULES_VERSION, secret_structure_pattern_names

WITHHELD_PREFIX = "secrets_withheld:"


def merge_secret_gaps(gaps, additions) -> list[str]:
    """Combine withheld counts from distinct reads into one content-free marker."""
    markers = [*gaps, *additions]
    count = sum(int(gap[len(WITHHELD_PREFIX) :]) for gap in markers if gap.startswith(WITHHELD_PREFIX))
    rest = [gap for gap in markers if not gap.startswith(WITHHELD_PREFIX)]
    return [f"{WITHHELD_PREFIX}{count}", *rest] if count else rest


def _decoded_json(value):
    """Read a stored JSON field once; retain plain metadata strings as written."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _stored_value(value):
    """Read repository objects without treating their display encoding as source text.

    Retrieved objects carry JSON payloads and JSON metadata as strings. Scanning
    those encodings would turn escaped quotes around placeholders into values.
    Event bodies and artifact labels remain literal source text.
    """
    if not is_dataclass(value) or isinstance(value, type):
        return value
    result = asdict(value)
    result.pop("content_secret_scan", None)
    if "content" in result and "metadata" in result:
        result["metadata"] = [(key, _decoded_json(member)) for key, member in result["metadata"]]
        if result.get("kind") in {"claim", "procedure", "episode", "reference"}:
            result["content"] = _decoded_json(result["content"])
    return result


def stored_item_pattern_names(value) -> tuple[str, ...]:
    """Classify a stored item's visible text and any structured claim assignment.

    Claims can separate a credential's label and its value into ``predicate``
    and ``value_text``. Those fields describe one assignment as well as two
    separate texts; inspecting only the separate texts would miss it.
    """
    if type(value) is RetrievedObject:
        try:
            hash(value)
            if len(value.content) + sum(len(member) for _, member in value.metadata) <= 131072:
                return _retrieved_pattern_names(value, SECRET_RULES_VERSION)
        except TypeError:
            # Runtime callers may supply mutable metadata despite the type hint.
            # Such views remain structurally scanned on every release.
            pass
    if type(value) is RetrievedObject:
        return _retrieved_pattern_names.__wrapped__(value, SECRET_RULES_VERSION)
    return secret_structure_pattern_names(_stored_value(value))


@lru_cache(maxsize=1024)
def _retrieved_pattern_names(value: RetrievedObject, rules_version: str) -> tuple[str, ...]:
    """Reuse only complete immutable views; rules are fixed for the process lifetime.

    A full view key includes the complete scan receipt, metadata and evidence, so changed
    projections and equal references in independent stores cannot share verdicts.
    Large views bypass the bounded cache to avoid retaining oversized originals.
    """
    stored = cast(dict, _stored_value(value))
    verdict = value.content_secret_scan
    if (
        value.kind == "event"
        and verdict is not None
        and verdict[0] == rules_version
        and verdict[1] == hashlib.sha256(value.content.encode("utf-8")).hexdigest()
    ):
        # Only the exact body was attested. Entries, evidence and every other
        # projection still cross the canonical structural scanner on release.
        stored.pop("content")
        return tuple(sorted(set(verdict[2]) | set(secret_structure_pattern_names(stored))))
    return secret_structure_pattern_names(stored)


def withhold_secret_items(items, gaps: list[str], *, content=lambda item: item) -> list:
    """Return safe items and accumulate a content-free withheld count in ``gaps``.

    ``content`` selects a stored object from a retrieval pair. Dataclass objects
    are inspected structurally so quotes, evidence and metadata cross the same
    boundary as their displayed content. No match text or object identity leaves
    this function in a diagnostic.
    """
    kept = []
    withheld = 0
    for item in items:
        if stored_item_pattern_names(content(item)):
            withheld += 1
        else:
            kept.append(item)
    if withheld:
        gaps[:] = merge_secret_gaps(gaps, [f"{WITHHELD_PREFIX}{withheld}"])
    return kept
