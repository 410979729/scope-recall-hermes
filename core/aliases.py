"""Conservative validation for durable project-name aliases."""
from __future__ import annotations

import re

from ..contracts import ContractError
from .source_qualification import AUTHORITY_QUESTION, CLAUSE_BREAK, preserves_qualifiers


_NEGATED = re.compile(r"(?:不是|并非|没有|未曾|不叫|不要|别把|不应|不能)|\b(?:not|never|do not|don't)\b", re.I)
_HYPOTHETICAL = re.compile(r"假设|假如|如果|设想|虚构|假定|\b(?:suppose|hypothetical|fictional|what if)\b", re.I)


# Hyphens, path separators, and dots can be part of stable identifiers.  They
# must not be treated as a boundary when the model starts a quote in the
# middle of a longer identifier.
_NAME_CHAR = r"\w\u3400-\u9fff./\\-"


def _rename_pattern(anchor: str, name: str) -> re.Pattern[str]:
    """Match a complete old identity and complete new name in one clause."""

    return re.compile(
        rf"(?<![{_NAME_CHAR}]){re.escape(anchor)}[ \t]*"
        rf"(?:项目|project)?[ \t]*"
        rf"(?:以后|之后|现在|正式|已|已经)?[ \t]*"
        rf"(?:改名为|更名为|改称|改叫|renamed[ \t]+to|is[ \t]+now[ \t]+called)[ \t]*"
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


def validate_alias_target(target, *, scope_id: str, project_id: str | None, branch_id: str | None) -> None:
    """Bind an alias to one live claim in the caller's exact identity scope."""
    if target is None or target.scope_id != scope_id:
        raise ContractError("ACCESS_DENIED", "alias_target_scope")
    if target.project_id != project_id or target.branch_id != branch_id:
        raise ContractError("ACCESS_DENIED", "alias_target_project")
    if target.state != "active" or target.suppressed:
        raise ContractError("ACCESS_DENIED", "alias_target_not_live")
    if target.payload["kind"] != "fact" or target.payload["predicate"].casefold() not in {
        "项目名称", "项目名", "名称", "project_name", "project name", "name"
    }:
        raise ContractError("ACCESS_DENIED", "alias_target_not_name_identity")
