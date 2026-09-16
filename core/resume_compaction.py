"""Select complete resume fields when a full episode resume cannot fit.

This is field selection, never string slicing: every retained value and
evidence reference stays intact JSON.  Trusted source order -- the
``episode_events`` sequence attached at hydration -- decides which correction
or progress entry is current; the order of a model-written list never does.
"""
from __future__ import annotations

import unicodedata

from .retrieval import RetrievedObject, optional_json

_EMPTY = (None, "", [], {})


def resume_fields(obj: RetrievedObject) -> dict | None:
    """The resume JSON of an episode object, or ``None`` for anything else."""
    value = optional_json(obj.content) if obj.kind == "episode" else None
    return value if type(value) is dict else None


def resume_evidence_refs(value: object) -> tuple[str, ...]:
    """Every ``evidence_refs``/``next_step_evidence_refs`` entry under ``value``,
    in document order, once."""
    refs: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key in ("evidence_refs", "next_step_evidence_refs"):
                raw = item.get(key)
                if isinstance(raw, list):
                    refs.extend(ref for ref in raw if type(ref) is str)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return tuple(dict.fromkeys(refs))


def source_order(obj: RetrievedObject) -> dict[str, int]:
    """Trusted event sequence per retained ``ref@revision``, attached during hydration."""
    values = optional_json(dict(obj.metadata).get("source_order"))
    if not isinstance(values, list):
        return {}
    order: dict[str, int] = {}
    for value in values[:32]:
        if (isinstance(value, list) and len(value) == 3 and type(value[0]) is str
                and type(value[1]) is int and type(value[2]) is int):
            order[f"{value[0]}@{value[1]}"] = value[2]
    return order


def source_texts(obj: RetrievedObject) -> dict[str, str]:
    """Freshly hydrated source text kept beside the trusted ordering."""
    values = optional_json(dict(obj.metadata).get("source_texts"))
    if not isinstance(values, dict):
        return {}
    return {ref: text for ref, text in values.items() if type(ref) is str and type(text) is str}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _mentions_next_step(source_text: str, next_step: object) -> bool:
    if type(next_step) is not str or not next_step.strip():
        return False
    needle = _normalized(next_step)
    return bool(needle and needle in _normalized(source_text))


def _supported_next_refs(resume: dict, order: dict[str, int], texts: dict[str, str]) -> list[str]:
    """Retained refs whose fresh text contains the proposed step, in trusted sequence.

    Sequence alone is provenance, not semantic support: ``next_step``
    historically carried only global evidence refs.
    """
    next_step = resume.get("next_step")
    refs = [ref for ref in resume.get("evidence_refs", ())
            if type(ref) is str and ref in order and _mentions_next_step(texts.get(ref, ""), next_step)]
    return sorted(refs, key=lambda ref: order[ref])


def next_step_provenance_supported(obj: RetrievedObject) -> bool:
    """Whether a resume next step has fresh text support.

    Without trusted order (older readers, test doubles) the original support
    refs are kept.  Once trusted order exists, an unverified next step must be
    omitted even when the full episode fits the packet budget.
    """
    resume = resume_fields(obj)
    if resume is None or resume.get("next_step") in _EMPTY:
        return True
    order = source_order(obj)
    if not order:
        return True
    return bool(_supported_next_refs(resume, order, source_texts(obj)))


def latest_resume_entry(value: object, order: dict[str, int]) -> dict | None:
    """The current entry of a resume list, chosen by trusted source sequence.

    A known sequence cannot establish "latest" while another entry lacks
    trusted order: carrying the known one would turn incomplete provenance
    into a current claim.  Without order, only a single complete entry is
    safe to carry.
    """
    if not isinstance(value, list):
        return None
    entries = [entry for entry in value if isinstance(entry, dict) and entry]
    if not entries:
        return None
    sequenced: list[tuple[int, int, dict]] = []
    for index, entry in enumerate(entries):
        refs = resume_evidence_refs(entry)
        if not refs or any(ref not in order for ref in refs):
            return entries[0] if len(entries) == 1 else None
        sequenced.append((max(order[ref] for ref in refs), -index, entry))
    return max(sequenced, key=lambda item: item[:2])[2]


def compact_episode_variants(obj: RetrievedObject) -> tuple[dict, ...]:
    """Complete-field subsets of an episode resume, most complete first.

    The current correction and resumable next step are preferred over stale
    history and bookkeeping.  No variant removes a field's evidence refs after
    keeping that field, and the full resume itself is never a variant.
    """
    resume = resume_fields(obj)
    if resume is None:
        return ()
    order, texts = source_order(obj), source_texts(obj)
    selected: dict[str, object] = {}
    for key in ("decisions", "verified_progress"):
        entry = latest_resume_entry(resume.get(key), order)
        if entry is not None:
            selected[key] = [entry]

    next_step = resume.get("next_step")
    if next_step not in _EMPTY:
        if order:
            refs = _supported_next_refs(resume, order, texts)
        else:
            refs = [ref for ref in resume.get("evidence_refs", ()) if type(ref) is str]
        if refs or not order:
            selected["next_step"] = next_step
            if resume.get("next_step_basis") not in _EMPTY:
                selected["next_step_basis"] = resume["next_step_basis"]
            if refs:
                selected["next_step_evidence_refs"] = refs
    if "next_step" not in selected:
        open_item = latest_resume_entry(resume.get("open_items"), order)
        if open_item is not None:
            selected["open_items"] = [open_item]
    if not any(key in selected for key in ("decisions", "verified_progress")) and resume.get("goal") not in _EMPTY:
        selected["goal"] = resume["goal"]
    if not selected:
        return ()

    # All independently supported fields first, then progressively fewer.
    variants = [dict(selected)]
    for drop in (("goal",), ("verified_progress",), ("open_items",),
                 ("next_step", "next_step_basis", "next_step_evidence_refs")):
        candidate = {key: value for key, value in selected.items() if key not in drop}
        if candidate and candidate not in variants:
            variants.append(candidate)
    return tuple(variant for variant in variants if variant != resume)
