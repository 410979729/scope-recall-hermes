"""Conservative validation for durable project-name aliases."""

from __future__ import annotations

import re

from ..contracts import ContractError
from .language_resources import language_rules
from .source_qualification import AUTHORITY_QUESTION, CLAUSE_BREAK, CONDITIONAL, preserves_qualifiers

ENTITY_KINDS = ("fact", "preference", "constraint", "decision", "intention")
_RULES = language_rules("aliases")
_NEGATED = re.compile(_RULES["negated"], re.I)
_HYPOTHETICAL = re.compile(CONDITIONAL.pattern + _RULES["hypothetical"], re.I)
_NAME_CHAR = r"\w\u3400-\u9fff./\\-"


def _rename_pattern(anchor: str, name: str) -> re.Pattern[str]:
    """Match a complete old identity and complete new name in one clause."""

    return re.compile(
        rf"(?<![{_NAME_CHAR}]){re.escape(anchor)}[ \t]*"
        rf"{_RULES['project']}[ \t]*"
        rf"{_RULES['aspect']}[ \t]*"
        rf"{_RULES['rename']}[ \t]*"
        rf"[“\"'「]?{re.escape(name)}"
        rf"(?=$|[”\"'」\s，,。.;；!！?？])",
        re.I,
    )


def _relation_clause(text: str, match: re.Match[str]) -> str:
    """Return only the punctuation-delimited clause containing a relation."""

    before = list(CLAUSE_BREAK.finditer(text, 0, match.start()))
    left = before[-1].end() if before else 0
    after = CLAUSE_BREAK.search(text, match.end())
    right = after.start() if after else len(text)
    return text[left:right]


def validate_alias_source(
    text: str,
    name: str,
    target,
    *,
    source_text: str,
) -> None:
    """Require an asserted, human-readable name relation.

    The model may propose a shape, but only the captured source can authorize
    the alias.  Keep this check local to alias handling because ordinary claim
    negation rules deliberately do not apply to every claim kind.
    """
    quote = text
    content = source_text
    if (
        not isinstance(quote, str)
        or not isinstance(content, str)
        or not name
        or name not in content
        or not quote
        or quote not in content
    ):
        raise ContractError("ACCESS_DENIED", "alias_name_not_bound")
    # A name appearing beside an arbitrary valid ref does not establish identity.
    # The full source must connect the target's actual stable subject or its
    # previously sourced name to the complete new name within one clause. The
    # quote must contain that complete relation; it cannot drop a leading name
    # or a qualifier and then authorize a different assertion.
    anchors = {target.payload["subject"], target.payload["value_text"]}
    for anchor in anchors:
        if not isinstance(anchor, str) or not anchor:
            continue
        for match in _rename_pattern(anchor, name).finditer(content):
            clause = _relation_clause(content, match)
            if (
                AUTHORITY_QUESTION.search(clause)
                or _NEGATED.search(clause)
                or _HYPOTHETICAL.search(clause)
                or not preserves_qualifiers(content, quote)
                or match.group(0) not in quote
            ):
                continue
            return
    raise ContractError("ACCESS_DENIED", "alias_relation_not_bound")


def validate_alias_target(
    target, *, scope_id: str, project_id: str | None, branch_id: str | None, entity: bool = False
) -> None:
    """Bind an alias to one live claim in the caller's exact identity scope."""
    if target is None or target.scope_id != scope_id:
        raise ContractError("ACCESS_DENIED", "alias_target_scope")
    if target.project_id != project_id or target.branch_id != branch_id:
        raise ContractError("ACCESS_DENIED", "alias_target_project")
    if target.state != "active" or target.suppressed:
        raise ContractError("ACCESS_DENIED", "alias_target_not_live")
    if entity:
        if target.payload["kind"] not in ENTITY_KINDS:
            raise ContractError("ACCESS_DENIED", "alias_target_not_entity")
    elif target.payload["kind"] != "fact" or target.payload["predicate"].casefold() not in _RULES["name_predicates"]:
        raise ContractError("ACCESS_DENIED", "alias_target_not_name_identity")


def entity_alias_proof(tx, alias, target, now: str) -> bool:
    """Accept only a stored judgement whose two live fact versions still match the alias.

    Retraction, a changed endpoint, a deleted source or a different audience invalidates
    the relation without changing either fact. No model is called while reading.
    """
    from .claims import evidence_refs, valid_at
    from .visibility import allowed

    row = (
        tx._check()
        .execute(
            "SELECT source_ref,source_revision,target_ref,target_revision FROM entity_alias_proofs "
            "WHERE alias_ref=? AND alias_revision=?",
            (alias.ref, alias.revision),
        )
        .fetchone()
    )
    if row is None or (target.ref, target.revision) != (row[2], row[3]):
        return False
    source = tx.claims.version(row[0], row[1])
    if source is None:
        return False
    names = alias.payload.get("alias", {})
    source_names = {source.payload.get("subject"), *source.payload.get("entity_names", [])}
    if names.get("name") not in source_names or target.payload.get("subject") != alias.payload["subject"]:
        return False
    audience = (alias.scope_id, alias.project_id, alias.branch_id)
    for version in (source, target):
        if (version.scope_id, version.project_id, version.branch_id) != audience:
            return False
        if version.current_revision != version.revision or version.state != "active" or version.suppressed:
            return False
        if not valid_at(version, now, historical=False) or not allowed(tx, "claim", version.ref, automatic=False):
            return False
        try:
            tx.claims.live_sources(evidence_refs(version.payload), version.scope_id)
        except ContractError:
            return False
    return True
