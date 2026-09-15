"""Bounded read-only profile and one-hop entity views over admitted claims."""
from __future__ import annotations

from dataclasses import dataclass
import json

from ..contracts import (
    SOURCE_CONTEXTS_MAX_ITEMS,
    ContractError,
    SourceContext,
    TrustedContext,
    bounded_source_context,
    bounded_source_contexts,
    validate_model_request,
    validate_payload,
)
from .aliases import validate_alias_source, validate_alias_target
from .claim_storage import parse_source_ref
from .claims import ClaimVersion, effective_origin, select_effective
from .mutate import evidence_refs
from .visibility import ObjectRef, allowed, release_objects


DEFAULT_MAX_ITEMS = 16
DEFAULT_BUDGET_TOKENS = 4096
CANDIDATE_CAP = 200
_PROFILE_KINDS = frozenset({"fact", "preference", "constraint", "decision", "intention"})
_STATEMENT_KINDS = frozenset({"fact", "preference", "constraint", "decision", "intention"})
_SECTION_FOR_KIND = {
    "fact": "facts",
    "preference": "preferences",
    "constraint": "constraints",
    "decision": "decisions",
}
_SECTION_ORDER = ("facts", "preferences", "constraints", "decisions", "pending_intentions")
_CLOSED_INTENTION = frozenset({"completed", "cancelled", "expired"})
_RELEASE_ERRORS = frozenset({"VERSION_CONFLICT", "SOURCE_MISSING"})
_CURRENT_SUBJECT_ALIASES = frozenset({"user", "current_user", "用户", "我"})


def _verified_current_principal(context: TrustedContext) -> str | None:
    principal = context.source_principal
    if (
        principal is None
        or principal.kind != "human"
        or principal.resolution != "verified"
        or principal.principal_ref is None
    ):
        return None
    return principal.principal_ref


def _profile_subject(context: TrustedContext, requested: str) -> str:
    """Bind only explicit current-person aliases to a verified C1 principal."""

    if requested.casefold() not in _CURRENT_SUBJECT_ALIASES:
        return requested
    principal_ref = _verified_current_principal(context)
    if principal_ref is None:
        raise ContractError("IDENTITY_UNBOUND", "subject")
    return principal_ref


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _budget_bytes(value: object) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _strict_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ContractError("INPUT_INVALID", field)
    return value


def _normalize_read_request(name: str, request, context: TrustedContext, *, entity: bool) -> dict:
    if type(request) is not dict:
        raise ContractError("INPUT_INVALID", "object")
    body = dict(request)
    if "max_items" not in body:
        body["max_items"] = DEFAULT_MAX_ITEMS
    if "budget_tokens" not in body:
        body["budget_tokens"] = DEFAULT_BUDGET_TOKENS
    if entity and "direction" not in body:
        body["direction"] = "outgoing" if body.get("action") == "probe" else "both"
    _strict_int(body.get("max_items"), "max_items")
    _strict_int(body.get("budget_tokens"), "budget_tokens")
    return validate_model_request(name, body, context)


def _empty_sections() -> dict[str, list]:
    return {name: [] for name in _SECTION_ORDER}


def _public_markers(values: list[str], *, limit: int) -> list[str]:
    return list(dict.fromkeys(value for value in values if type(value) is str and value))[:limit]


def _claim_evidence_keys(version: ClaimVersion) -> tuple[str, ...]:
    refs = evidence_refs(version.payload)
    return tuple(dict.fromkeys(refs))[:32]


def _claim_evidence_live(tx, version: ClaimVersion) -> bool:
    if version.suppressed or not allowed(tx, "claim", version.ref, automatic=False):
        return False
    keys = _claim_evidence_keys(version)
    if not keys:
        return False
    for key in keys:
        try:
            source_ref, source_revision = parse_source_ref(key)
        except ContractError:
            return False
        if not allowed(tx, "event", source_ref, automatic=False):
            return False
        try:
            tx.claims.require_live_source(source_ref, source_revision)
        except ContractError:
            return False
        source = tx.source(source_ref, source_revision)
        if source is None or source.suppressed:
            return False
    return True


def _source_contexts_for(tx, version: ClaimVersion) -> list[SourceContext] | None:
    collected: list[SourceContext] = []
    for key in _claim_evidence_keys(version):
        try:
            source_ref, source_revision = parse_source_ref(key)
        except ContractError:
            continue
        source = tx.source(source_ref, source_revision)
        if source is None:
            continue
        context = bounded_source_context(source.event.get("source_context"))
        if context is None or context in collected:
            continue
        collected.append(context)
        if len(collected) >= SOURCE_CONTEXTS_MAX_ITEMS:
            break
    return bounded_source_contexts(collected)


def _intention_state(version: ClaimVersion) -> str | None:
    intention = version.payload.get("intention")
    if not isinstance(intention, dict):
        return None
    state = intention.get("state")
    return state if type(state) is str else None


def _is_current_statement(version: ClaimVersion) -> bool:
    if version.suppressed or version.state not in {"active", "disputed"}:
        return False
    if version.payload["kind"] not in _STATEMENT_KINDS:
        return False
    if version.payload["kind"] == "intention" and _intention_state(version) in _CLOSED_INTENTION:
        return False
    return True


def _temporal_status(version: ClaimVersion) -> str:
    if version.state == "disputed":
        return "disputed"
    if version.state == "active":
        return "current"
    return "unknown"


def _sort_key(version: ClaimVersion) -> tuple[str, str, str, int]:
    payload = version.payload
    return (payload["predicate"], payload["value_text"], version.ref, version.revision)


def _profile_item(version: ClaimVersion, contexts: list[SourceContext] | None) -> dict:
    payload = version.payload
    item = {
        "ref": version.ref,
        "revision": version.revision,
        "kind": payload["kind"],
        "subject": payload["subject"],
        "predicate": payload["predicate"],
        "value_text": payload["value_text"],
        "conditions": list(payload.get("conditions") or []),
        "temporal_status": _temporal_status(version),
        "claim_state": "disputed" if version.state == "disputed" else "active",
        "valid_from": version.valid_from,
        "valid_to": version.valid_to,
        "evidence_refs": list(_claim_evidence_keys(version)),
        "basis": version.basis,
    }
    if contexts:
        item["source_contexts"] = [dict(context) for context in contexts]
    return item


def _statement_item(version: ClaimVersion, *, direction: str, contexts: list[SourceContext] | None) -> dict:
    payload = version.payload
    item = {
        "direction": direction,
        "subject": payload["subject"],
        "predicate": payload["predicate"],
        "value_text": payload["value_text"],
        "ref": version.ref,
        "revision": version.revision,
        "kind": payload["kind"],
        "conditions": list(payload.get("conditions") or []),
        "temporal_status": _temporal_status(version),
        "claim_state": "disputed" if version.state == "disputed" else "active",
        "valid_from": version.valid_from,
        "valid_to": version.valid_to,
        "evidence_refs": list(_claim_evidence_keys(version)),
        "basis": version.basis,
    }
    if contexts:
        item["source_contexts"] = [dict(context) for context in contexts]
    return item


@dataclass(frozen=True)
class _AliasResolution:
    status: str
    subject: str | None
    fence: tuple[ObjectRef, ...]
    scan_capped: bool


def _alias_source_bound(tx, alias: ClaimVersion, target: ClaimVersion) -> bool:
    alias_payload = alias.payload.get("alias")
    if not isinstance(alias_payload, dict) or type(alias_payload.get("name")) is not str:
        return False
    try:
        roots = tx.claims.roots(_claim_evidence_keys(alias))
    except ContractError:
        return False
    for root in roots:
        if effective_origin(root) != "human_direct" or root.capture_state != "complete" or root.capture_gaps:
            continue
        for span in alias.payload.get("evidence_spans") or ():
            if (span.get("source_ref"), span.get("source_revision")) != (root.ref, root.revision):
                continue
            try:
                validate_alias_source(
                    span["quote"],
                    alias_payload["name"],
                    target,
                    source_text=root.content,
                )
                return True
            except ContractError:
                continue
    return False


def _load_effective(tx, ref: str, now: str) -> ClaimVersion | None:
    versions = tx.claims.versions(ref)
    if not versions:
        return None
    return select_effective(versions, now)


def _alias_proof_fence(alias: ClaimVersion, target: ClaimVersion) -> tuple[ObjectRef, ...]:
    fence: list[ObjectRef] = []
    seen: set[tuple[str, str, int]] = set()
    for version in (alias, target):
        key = ("claim", version.ref, version.revision)
        if key in seen:
            continue
        seen.add(key)
        fence.append(ObjectRef("claim", version.ref, version.revision))
    return tuple(fence)


def resolve_subject(tx, queried: str, now: str) -> _AliasResolution:
    """Resolve only admitted project-name aliases; never merge ambiguous names."""

    alias_refs = tx.claims.list_refs(alias_name=queried, limit=CANDIDATE_CAP)
    if len(alias_refs) >= CANDIDATE_CAP:
        return _AliasResolution("ambiguous", None, (), True)
    accepted: list[tuple[str, ClaimVersion, ClaimVersion]] = []
    for ref in alias_refs:
        alias = _load_effective(tx, ref, now)
        if alias is None or alias.suppressed or alias.state != "active" or alias.payload.get("kind") != "alias":
            continue
        if not _claim_evidence_live(tx, alias):
            continue
        alias_payload = alias.payload.get("alias")
        if not isinstance(alias_payload, dict):
            continue
        name = alias_payload.get("name")
        if name != queried and alias.payload.get("value_text") != queried:
            continue
        target = _load_effective(tx, alias_payload.get("target_ref"), now) if type(alias_payload.get("target_ref")) is str else None
        try:
            if target is None:
                raise ContractError("SOURCE_MISSING", "alias_target")
            validate_alias_target(
                target,
                scope_id=alias.scope_id,
                project_id=tx.context.project_id,
                branch_id=tx.context.branch_id,
            )
            if alias.payload["subject"] != target.payload["subject"]:
                raise ContractError("ACCESS_DENIED", "alias_subject_identity")
        except ContractError:
            continue
        if not _claim_evidence_live(tx, target) or not _alias_source_bound(tx, alias, target):
            continue
        accepted.append((target.payload["subject"], alias, target))
    distinct = list(dict.fromkeys(subject for subject, _alias, _target in accepted))
    literal_refs = tx.claims.list_refs(subject=queried, limit=1)
    has_literal = bool(literal_refs)
    if len(distinct) > 1:
        return _AliasResolution("ambiguous", None, (), False)
    if len(distinct) == 1 and has_literal and queried != distinct[0]:
        return _AliasResolution("ambiguous", None, (), False)
    if len(distinct) == 1:
        _subject, alias, target = next(row for row in accepted if row[0] == distinct[0])
        return _AliasResolution("resolved", distinct[0], _alias_proof_fence(alias, target), False)
    return _AliasResolution("literal", queried, (), False)


def _select_versions(tx, refs: tuple[str, ...], now: str) -> tuple[ClaimVersion, ...]:
    selected = []
    for ref in refs:
        version = _load_effective(tx, ref, now)
        if version is None or not _is_current_statement(version):
            continue
        if not _claim_evidence_live(tx, version):
            continue
        selected.append(version)
    return tuple(selected)


def _release_selected(storage, clock, context, versions: tuple[ClaimVersion, ...], fence: tuple[ObjectRef, ...], epoch: int):
    refs = []
    seen: set[tuple[str, str, int]] = set()
    for version in (*versions,):
        key = ("claim", version.ref, version.revision)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ObjectRef("claim", version.ref, version.revision))
    for item in fence:
        key = (item.kind, item.ref, item.revision)
        if key in seen:
            continue
        seen.add(key)
        refs.append(item)
    if not refs:
        with storage.read(context) as tx:
            if tx.status().memory_epoch != epoch:
                raise ContractError("VERSION_CONFLICT", "memory_epoch")
        return ()
    return release_objects(
        storage,
        clock,
        context,
        tuple(refs),
        expected_epoch=epoch,
        automatic=False,
        history=False,
    )


def _recheck_released(tx, released, expected: tuple[ClaimVersion, ...]) -> tuple[ClaimVersion, ...] | None:
    by_ref = {item.ref: item for item in released if isinstance(item, ClaimVersion)}
    checked = []
    for version in expected:
        current = by_ref.get(version.ref)
        if current is None or current.revision != version.revision:
            return None
        if current.suppressed or not _is_current_statement(current) or not _claim_evidence_live(tx, current):
            return None
        checked.append(current)
    return tuple(checked)


def _coverage(*, status: str, truncated: bool, scan_capped: bool, budget_capped: bool) -> str:
    if status in {"no_match", "unavailable"}:
        return "unknown"
    if scan_capped or truncated or budget_capped or status == "partial":
        return "partial"
    if status == "ok":
        return "complete_for_query"
    return "unknown"


def _answerability(*, status: str, alias_status: str, item_count: int, disputed: bool) -> str:
    if status in {"no_match", "unavailable"}:
        return "unknown"
    if alias_status == "ambiguous":
        return "ambiguous"
    if status == "partial" or disputed:
        return "partial"
    if item_count:
        return "supported"
    return "unknown"


def _clip_candidate_refs(*groups: tuple[str, ...], reserved: int = 0) -> tuple[tuple[str, ...], bool]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group:
            if ref in seen:
                continue
            seen.add(ref)
            merged.append(ref)
    room = max(0, CANDIDATE_CAP - reserved)
    overflow = len(merged) > room or any(len(group) >= CANDIDATE_CAP for group in groups)
    return tuple(sorted(merged))[:room], overflow


def _clip_versions_for_view(versions: tuple[ClaimVersion, ...], *, max_items: int) -> tuple[tuple[ClaimVersion, ...], bool]:
    ordered = tuple(sorted(versions, key=_sort_key))
    if len(ordered) <= max_items:
        return ordered, False
    return ordered[:max_items], True


def _mark_gap(result: dict, gap: str, *, unmet: str | None = None) -> None:
    result["gaps"] = _public_markers([*result.get("gaps", []), gap], limit=16)
    if unmet:
        result["unmet_needs"] = _public_markers([*result.get("unmet_needs", []), unmet], limit=8)


def _apply_max_items(result: dict, item_lists: list[list], max_items: int) -> None:
    total = sum(len(items) for items in item_lists)
    if total <= max_items:
        return
    remaining = total - max_items
    for items in reversed(item_lists):
        while remaining and items:
            items.pop()
            remaining -= 1
    result["truncated"] = True
    _mark_gap(result, "max_items_cap", unmet="retry_with_higher_max_items")


def _drop_one_item(item_lists: list[list]) -> bool:
    for items in reversed(item_lists):
        if items:
            items.pop()
            return True
    return False


def _clear_terminal_body(result: dict, item_lists: list[list]) -> None:
    if result["status"] not in {"no_match", "unavailable"}:
        return
    for items in item_lists:
        items.clear()
    if result["status"] == "unavailable":
        result["resolved_subject"] = None
        result["alias_resolution"] = "none"


def _mark_budget_cap(result: dict, budget_tokens: int) -> None:
    result["truncated"] = True
    _mark_gap(result, "budget_token_cap")
    if budget_tokens < DEFAULT_BUDGET_TOKENS:
        _mark_gap(result, "budget_token_cap", unmet="retry_once_with_budget_tokens_4096")


def _converge_final_budget(
    result: dict,
    *,
    max_items: int,
    budget_tokens: int,
    item_lists: list[list],
    alias_status: str,
    scan_capped: bool,
    disputed_from,
) -> dict:
    """Fit the finalized canonical UTF-8 envelope, then reject if even the minimum cannot fit."""

    _apply_max_items(result, item_lists, max_items)
    while True:
        item_count = sum(len(items) for items in item_lists)
        _finalize_status(
            result,
            item_count=item_count,
            alias_status=alias_status,
            scan_capped=scan_capped,
            disputed=bool(disputed_from()),
        )
        _clear_terminal_body(result, item_lists)
        if _budget_bytes(result) <= budget_tokens:
            return result
        if not _drop_one_item(item_lists):
            raise ContractError("INPUT_INVALID", "budget_tokens")
        _mark_budget_cap(result, budget_tokens)


def _profile_item_lists(result: dict) -> list[list]:
    return [result["sections"][name] for name in _SECTION_ORDER] + [result["disputed"]]


def _finish_view(
    result: dict,
    *,
    schema: str,
    max_items: int,
    budget_tokens: int,
    item_lists: list[list],
    alias_status: str,
    scan_capped: bool,
    disputed_from,
) -> dict:
    _converge_final_budget(
        result,
        max_items=max_items,
        budget_tokens=budget_tokens,
        item_lists=item_lists,
        alias_status=alias_status,
        scan_capped=scan_capped,
        disputed_from=disputed_from,
    )
    return validate_payload(schema, result)


def _finalize_status(result: dict, *, item_count: int, alias_status: str, scan_capped: bool, disputed: bool) -> None:
    if result["status"] == "unavailable":
        result["coverage"] = "unknown"
        result["answerability"] = "unknown"
        return
    if alias_status == "ambiguous":
        result["status"] = "partial"
        result["coverage"] = "unknown" if not scan_capped else "partial"
        result["answerability"] = "ambiguous"
        return
    if item_count == 0:
        if result["truncated"] or "budget_token_cap" in result["gaps"] or "max_items_cap" in result["gaps"]:
            result["status"] = "partial"
            result["coverage"] = "partial"
            result["answerability"] = "partial"
            return
        result["status"] = "no_match"
        result["coverage"] = "unknown"
        result["answerability"] = "unknown"
        return
    if result["truncated"] or scan_capped:
        result["status"] = "partial"
    else:
        result["status"] = "ok"
    result["coverage"] = _coverage(
        status=result["status"],
        truncated=result["truncated"],
        scan_capped=scan_capped,
        budget_capped="budget_token_cap" in result["gaps"],
    )
    result["answerability"] = _answerability(
        status=result["status"],
        alias_status=alias_status,
        item_count=item_count,
        disputed=disputed,
    )


def _unavailable(request: dict, *, epoch: int | None, extra_gaps: tuple[str, ...] = (), entity: bool = False) -> dict:
    result = {
        "protocol_version": "1.1",
        "request_id": request["request_id"],
        "status": "unavailable",
        "memory_epoch": epoch,
        "subject": request["subject"],
        "resolved_subject": None,
        "alias_resolution": "none",
        "gaps": _public_markers([*extra_gaps, "memory_epoch_or_authority_changed"], limit=16),
        "coverage": "unknown",
        "answerability": "unknown",
        "truncated": False,
        "scan_capped": False,
        "unmet_needs": ["retry_against_current_epoch"],
    }
    if entity:
        result.update(action=request["action"], direction=request["direction"], statements=[])
        item_lists = [result["statements"]]
    else:
        result.update(sections=_empty_sections(), disputed=[])
        item_lists = _profile_item_lists(result)
    return _finish_view(
        result,
        schema="entity_view" if entity else "profile_view",
        max_items=request["max_items"],
        budget_tokens=request["budget_tokens"],
        item_lists=item_lists,
        alias_status="none",
        scan_capped=False,
        disputed_from=lambda: False,
    )


def _contexts_map(tx, versions: tuple[ClaimVersion, ...]) -> dict[str, list[SourceContext] | None]:
    return {version.ref: _source_contexts_for(tx, version) for version in versions}


def read_profile(storage, clock, context: TrustedContext, request) -> dict:
    payload = _normalize_read_request("profile_request", request, context, entity=False)
    requested_subject = payload["subject"]
    lookup_subject = _profile_subject(context, requested_subject)
    now = clock.utc_now()
    try:
        with storage.read(context) as tx:
            epoch = tx.status().memory_epoch
            resolution = resolve_subject(tx, lookup_subject, now)
            fence = resolution.fence
            if resolution.status == "ambiguous":
                selected: tuple[ClaimVersion, ...] = ()
                scan_capped = resolution.scan_capped
                item_truncated = False
            else:
                subject = resolution.subject or lookup_subject
                refs = tx.claims.list_refs(subject=subject, limit=CANDIDATE_CAP)
                clipped_refs, ref_overflow = _clip_candidate_refs(refs, reserved=len(fence))
                scan_capped = resolution.scan_capped or len(refs) >= CANDIDATE_CAP or ref_overflow
                selected = _select_versions(tx, clipped_refs, now)
                selected, item_truncated = _clip_versions_for_view(selected, max_items=payload["max_items"])
        released = _release_selected(storage, clock, context, selected, fence, epoch)
        with storage.read(context) as tx:
            if tx.status().memory_epoch != epoch:
                return _unavailable(payload, epoch=tx.status().memory_epoch)
            checked = _recheck_released(tx, released, selected)
            if checked is None and selected:
                return _unavailable(payload, epoch=epoch)
            versions = checked or ()
            if resolution.status == "resolved" and resolution.subject:
                alias_ok = all(
                    item.ref in {version.ref for version in released if isinstance(version, ClaimVersion)}
                    for item in fence
                )
                if fence and not alias_ok:
                    return _unavailable(payload, epoch=epoch)
            contexts = _contexts_map(tx, versions)
            result_epoch = tx.status().memory_epoch
            if result_epoch != epoch:
                return _unavailable(payload, epoch=result_epoch)
    except ContractError as exc:
        if exc.code in _RELEASE_ERRORS:
            try:
                with storage.read(context) as tx:
                    current = tx.status().memory_epoch
            except ContractError:
                current = None
            return _unavailable(payload, epoch=current, extra_gaps=(exc.field,))
        raise
    sections = _empty_sections()
    disputed: list[dict] = []
    gaps: list[str] = []
    if resolution.status == "ambiguous":
        gaps.append("alias_ambiguous")
    for version in sorted(versions, key=_sort_key):
        item = _profile_item(version, contexts.get(version.ref))
        if version.state == "disputed":
            disputed.append(item)
            continue
        if version.payload["kind"] == "intention":
            if _intention_state(version) == "pending":
                sections["pending_intentions"].append(item)
            continue
        section = _SECTION_FOR_KIND.get(version.payload["kind"])
        if section is not None:
            sections[section].append(item)
    if disputed:
        gaps.append("disputed_facts_separated")
    if not any(sections[name] for name in _SECTION_ORDER) and not disputed and resolution.status != "ambiguous":
        gaps.append("consolidation_required" if not versions else "no_current_qualified_facts")
    result = {
        "protocol_version": "1.1",
        "request_id": payload["request_id"],
        "status": "ok",
        "memory_epoch": epoch,
        "subject": requested_subject,
        "resolved_subject": None if resolution.status == "ambiguous" else (resolution.subject or lookup_subject),
        "alias_resolution": resolution.status,
        "sections": sections,
        "disputed": disputed,
        "gaps": gaps,
        "coverage": "partial",
        "answerability": "unknown",
        "truncated": False,
        "scan_capped": scan_capped,
        "unmet_needs": [],
    }
    if item_truncated:
        result["truncated"] = True
        result["gaps"].append("max_items_cap")
        result["unmet_needs"].append("retry_with_higher_max_items")
    if scan_capped:
        result["gaps"].append("scan_capped")
        result["unmet_needs"].append("bounded_candidate_enumeration")
    item_lists = _profile_item_lists(result)
    return _finish_view(
        result,
        schema="profile_view",
        max_items=payload["max_items"],
        budget_tokens=payload["budget_tokens"],
        item_lists=item_lists,
        alias_status=resolution.status,
        scan_capped=scan_capped,
        disputed_from=lambda: bool(result["disputed"]),
    )


def read_entity(storage, clock, context: TrustedContext, request) -> dict:
    payload = _normalize_read_request("entity_request", request, context, entity=True)
    requested_subject = payload["subject"]
    lookup_subject = _profile_subject(context, requested_subject)
    now = clock.utc_now()
    predicate = payload.get("predicate")
    want_out = payload["action"] == "probe" or payload["direction"] in {"outgoing", "both"}
    want_in = payload["action"] == "related" and payload["direction"] in {"incoming", "both"}
    if payload["action"] == "probe":
        want_out, want_in = True, False
    try:
        with storage.read(context) as tx:
            epoch = tx.status().memory_epoch
            resolution = resolve_subject(tx, lookup_subject, now)
            outgoing: tuple[ClaimVersion, ...] = ()
            incoming: tuple[ClaimVersion, ...] = ()
            scan_capped = resolution.scan_capped
            item_truncated = False
            fence = resolution.fence
            if resolution.status != "ambiguous":
                subject = resolution.subject or lookup_subject
                outgoing_refs: tuple[str, ...] = ()
                incoming_refs: tuple[str, ...] = ()
                incoming_names: tuple[str, ...] = ()
                scan_hit = False
                if want_out:
                    kind = "fact" if payload["action"] == "probe" else None
                    outgoing_refs = tx.claims.list_refs(subject=subject, predicate=predicate, kind=kind, limit=CANDIDATE_CAP)
                    scan_hit = scan_hit or len(outgoing_refs) >= CANDIDATE_CAP
                if want_in:
                    incoming_names = (
                        (lookup_subject,)
                        if resolution.status == "literal"
                        else tuple(dict.fromkeys((lookup_subject, subject)))
                    )
                    seen_in: list[str] = []
                    seen_set: set[str] = set()
                    for name in incoming_names:
                        refs = tx.claims.list_refs(value_text=name, predicate=predicate, limit=CANDIDATE_CAP)
                        scan_hit = scan_hit or len(refs) >= CANDIDATE_CAP
                        for ref in refs:
                            if ref in seen_set:
                                continue
                            seen_set.add(ref)
                            seen_in.append(ref)
                    incoming_refs = tuple(seen_in)
                clipped_refs, ref_overflow = _clip_candidate_refs(
                    outgoing_refs, incoming_refs, reserved=len(fence),
                )
                scan_capped = scan_capped or scan_hit or ref_overflow
                loaded = _select_versions(tx, clipped_refs, now)
                outgoing = tuple(
                    version for version in loaded
                    if version.ref in outgoing_refs
                    and (payload["action"] != "probe" or version.payload["kind"] == "fact")
                )
                incoming = tuple(
                    version for version in loaded
                    if version.ref in incoming_refs and version.payload.get("value_text") in incoming_names
                )
                selected_by_ref: dict[str, ClaimVersion] = {}
                for version in (*outgoing, *incoming):
                    selected_by_ref.setdefault(version.ref, version)
                selected, item_truncated = _clip_versions_for_view(
                    tuple(selected_by_ref.values()), max_items=payload["max_items"],
                )
                kept = {version.ref for version in selected}
                outgoing = tuple(version for version in outgoing if version.ref in kept)
                incoming = tuple(version for version in incoming if version.ref in kept)
            else:
                selected = ()
        released = _release_selected(storage, clock, context, selected, fence, epoch)
        with storage.read(context) as tx:
            if tx.status().memory_epoch != epoch:
                return _unavailable(payload, epoch=tx.status().memory_epoch, entity=True)
            checked = _recheck_released(tx, released, selected)
            if checked is None and selected:
                return _unavailable(payload, epoch=epoch, entity=True)
            versions = {version.ref: version for version in (checked or ())}
            outgoing = tuple(versions[version.ref] for version in outgoing if version.ref in versions)
            incoming = tuple(versions[version.ref] for version in incoming if version.ref in versions)
            contexts = _contexts_map(tx, tuple(versions.values()))
            if tx.status().memory_epoch != epoch:
                return _unavailable(payload, epoch=tx.status().memory_epoch, entity=True)
    except ContractError as exc:
        if exc.code in _RELEASE_ERRORS:
            try:
                with storage.read(context) as tx:
                    current = tx.status().memory_epoch
            except ContractError:
                current = None
            return _unavailable(payload, epoch=current, extra_gaps=(exc.field,), entity=True)
        raise
    statements: list[dict] = []
    if resolution.status != "ambiguous":
        for version in sorted(outgoing, key=_sort_key):
            statements.append(_statement_item(version, direction="outgoing", contexts=contexts.get(version.ref)))
        for version in sorted(incoming, key=_sort_key):
            statements.append(_statement_item(version, direction="incoming", contexts=contexts.get(version.ref)))
    gaps: list[str] = []
    if resolution.status == "ambiguous":
        gaps.append("alias_ambiguous")
    if not statements and resolution.status != "ambiguous":
        gaps.append("consolidation_required")
    result = {
        "protocol_version": "1.1",
        "request_id": payload["request_id"],
        "status": "ok",
        "memory_epoch": epoch,
        "subject": requested_subject,
        "resolved_subject": None if resolution.status == "ambiguous" else (resolution.subject or lookup_subject),
        "alias_resolution": resolution.status,
        "action": payload["action"],
        "direction": "outgoing" if payload["action"] == "probe" else payload["direction"],
        "statements": statements,
        "gaps": gaps,
        "coverage": "partial",
        "answerability": "unknown",
        "truncated": False,
        "scan_capped": scan_capped,
        "unmet_needs": [],
    }
    if item_truncated:
        result["truncated"] = True
        result["gaps"].append("max_items_cap")
        result["unmet_needs"].append("retry_with_higher_max_items")
    if scan_capped:
        result["gaps"].append("scan_capped")
        result["unmet_needs"].append("bounded_candidate_enumeration")
    return _finish_view(
        result,
        schema="entity_view",
        max_items=payload["max_items"],
        budget_tokens=payload["budget_tokens"],
        item_lists=[result["statements"]],
        alias_status=resolution.status,
        scan_capped=scan_capped,
        disputed_from=lambda: any(item["temporal_status"] == "disputed" for item in result["statements"]),
    )
