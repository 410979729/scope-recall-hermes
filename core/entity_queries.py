"""Read-only mapping of names in a question to admitted entity identities."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from ..contracts import ContractError
from .claims import select_effective
from .read_views import resolve_subject


def _mention_spans(question: str, name: str):
    """Yield complete Latin identifiers while allowing names embedded in Chinese prose."""
    offset = question.find(name)
    while offset >= 0:
        before, after = question[:offset], question[offset + len(name) :]
        left = not (
            name[0].isascii()
            and name[0].isalnum()
            and before
            and before[-1].isascii()
            and (before[-1].isalnum() or before[-1] in "_-/\\.")
        )
        right = not (
            name[-1].isascii()
            and name[-1].isalnum()
            and after
            and after[0].isascii()
            and (after[0].isalnum() or after[0] in "_-/\\.")
        )
        if left and right:
            yield offset, offset + len(name)
        offset = question.find(name, offset + 1)


def _query_mapping(tx, query, now):
    """Map simultaneous name spans; generated labels cannot trigger another replacement."""
    lookup = getattr(tx.claims, "aliases_for_query", None)
    if not callable(lookup):
        return query, (), ()
    refs = cast(tuple[str, ...], lookup(query, limit=200))
    if len(refs) >= 200:
        return query, (), ()
    if not refs:
        return query, (), ()
    labels = set()
    for ref in refs:
        alias = select_effective(tx.claims.versions(ref), now)
        if alias is not None and alias.state == "active":
            labels.update((alias.payload.get("alias", {}).get("name", ""), alias.payload["subject"]))
    literals = tx.claims.entity_names_for_query(query, matches=labels, limit=200)
    if literals is None or len(literals) >= 200:
        return query, (), ()
    spans = sorted(
        (start, end, name) for name in labels.union(literals) if name for start, end in _mention_spans(query, name)
    )
    pieces, names, fence, position, resolutions = [], set(), set(), 0, {}
    for start, end, name in sorted(spans, key=lambda item: (item[0], -item[1])):
        if start < position:
            continue
        canonical = name
        if name in labels:
            if name not in resolutions:
                resolutions[name] = resolve_subject(tx, name, now)
            result = resolutions[name]
            if result.status == "resolved":
                canonical = result.subject
                names.update(result.names)
                fence.update((item.ref, item.revision) for item in result.fence)
        pieces.extend((query[position:start], canonical))
        position = end
    return "".join((*pieces, query[position:])), tuple(sorted(names)), tuple(sorted(fence))


def resolved_query(tx, query: str, now: str) -> str:
    """Return the canonical question without relation expansion or a model call."""
    return _query_mapping(tx, query, now)[0]


def resolve_query_context(tx, context, clock):
    """Freeze names and proofs in the caller's entry read, including a literal miss."""
    if context.entity_names_resolved:
        return context
    context = replace(context, entity_names_resolved=True)
    seconds = context.deadline - clock.monotonic()
    if context.entity_fence or seconds <= 0 or context.mode in {"history", "as_of"}:
        return context
    try:
        if not hasattr(tx, "claims"):
            return context
        query, names, fence = _query_mapping(tx, context.query, context.now)
        alternatives = [name for name in names if not tuple(_mention_spans(query, name))]
        if alternatives:
            query += " " + " ".join(alternatives)
    except (ContractError, OSError):
        return context
    if len(query) > 8192:
        return context
    return context if not fence else replace(context, query=query, entity_fence=fence)


def fence_entity_query(tx, context, pairs, gaps):
    """Withhold an aliased answer if any identity proof changed before packet release."""
    from .claims import valid_at
    from .read_views import _accepted_alias

    for ref, revision in context.entity_fence:
        version = tx.claims.version(ref, revision)
        if (
            version is None
            or version.current_revision != revision
            or version.state != "active"
            or not valid_at(version, context.now, historical=False)
        ):
            gaps.append("entity_identity_changed")
            return []
        if version.payload["kind"] == "alias" and _accepted_alias(tx, ref, context.now) is None:
            gaps.append("entity_identity_changed")
            return []
    return pairs
