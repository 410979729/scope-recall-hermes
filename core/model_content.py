"""The content-release policy for stored items sent to an agent or model.

Local owner management uses the stored originals. All model-facing readers
withhold whole items, including nested evidence, and report only a count.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from .secret_patterns import secret_structure_pattern_names

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
    return secret_structure_pattern_names(_stored_value(value))


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
