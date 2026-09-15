"""Pure journal match-planning helpers. No SQL writes."""

from __future__ import annotations

from typing import Any

_WORKFLOW_CONTINUATION_TOKENS = {
    "journal-first",
    "journal-digest",
    "journal",
    "digest",
    "merge/upsert",
    "merge",
    "upsert",
    "日记",
    "合并",
}


def _workflow_continuation_tokens(content: str, tags: set[str], entities: set[str]) -> set[str]:
    del content  # generated heuristic prefixes contain "Journal digest" for every candidate
    values: list[str] = []
    for tag in tags:
        clean = tag.lower()
        if clean.startswith("topic:"):
            values.append(clean.removeprefix("topic:"))
    values.extend(entity.lower() for entity in entities)
    haystack = "\n".join(values)
    return {token for token in _WORKFLOW_CONTINUATION_TOKENS if token in haystack}


def _is_workflow_continuation(candidate_tokens: set[str], existing_tokens: set[str]) -> bool:
    if candidate_tokens & existing_tokens:
        return True
    update_tokens = {"merge/upsert", "merge", "upsert", "合并"}
    journal_anchor_tokens = {"journal-first", "journal", "digest", "journal-digest", "日记"}
    return bool(candidate_tokens & update_tokens and existing_tokens & journal_anchor_tokens)


def _metadata_entities(metadata: dict[str, Any]) -> set[str]:
    raw = metadata.get("entities", []) if isinstance(metadata, dict) else []
    return {str(entity).strip() for entity in raw if str(entity).strip()}
