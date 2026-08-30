"""Deterministic relation extraction and synchronization for memory graph edges.

Relation writes are companion evidence; contradiction checks and supersession links must not change the source memory text."""

from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from typing import Any, Iterable

try:
    from .graph import lifecycle_visible_sql, metadata_entities
    from .graph_relations import EXTRACTED_RELATION_TYPES
    from .relation_entity_policy import (
        distinctive_relation_entity as _distinctive_entity,
        high_frequency_document_threshold,
        normalize_relation_entity as _normalize_relation_entity,
    )
    from .relation_frequency_index import (
        RelationFrequencyIndexNotReady,
        relation_frequency_scope_failure_status,
        relation_frequency_snapshot,
        relation_frequency_snapshots_by_scope,
    )
    from .relation_containment import (
        complete_relation_focus_work,
        confirm_relation_scope_focus_generation,
        load_relation_focus_work,
        mark_relation_scope_degraded,
        plan_focus_relation_pairs,
        relation_focus_scope_has_debt,
    )
    from .scoring import semantic_similarity
    from .sql_store import now_iso
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from graph import lifecycle_visible_sql, metadata_entities
    from graph_relations import EXTRACTED_RELATION_TYPES
    from relation_entity_policy import (  # type: ignore[no-redef]
        distinctive_relation_entity as _distinctive_entity,
        high_frequency_document_threshold,
        normalize_relation_entity as _normalize_relation_entity,
    )
    from relation_frequency_index import (  # type: ignore[no-redef]
        RelationFrequencyIndexNotReady,
        relation_frequency_scope_failure_status,
        relation_frequency_snapshot,
        relation_frequency_snapshots_by_scope,
    )
    from relation_containment import (  # type: ignore[no-redef]
        complete_relation_focus_work,
        confirm_relation_scope_focus_generation,
        load_relation_focus_work,
        mark_relation_scope_degraded,
        plan_focus_relation_pairs,
        relation_focus_scope_has_debt,
    )
    from scoring import semantic_similarity
    from sql_store import now_iso

_SUPERSEDES_RE = re.compile(r"\b(?:supersedes?|replaces?|replaced)\b|取代|替代")
_OLD_RE = re.compile(r"\b(?:old|legacy|deprecated|previous|v\d+)\b|旧|旧版|过时")
_TYPED_RELATION_TRIGGERS = {
    # `needs`/`需要` are ordinary instruction words, not reliable structural
    # dependency evidence. Keeping them here makes one operational note link
    # to every memory that shares a generic entity.
    "depends_on": (r"depends\s+on", r"requires?", r"依赖(?:于)?", r"取决于"),
    "owned_by": (
        r"owned\s+by",
        r"owner\s+is",
        r"maintained\s+by",
        r"belongs\s+to",
        r"归属",
        r"负责人",
    ),
    "affects": (r"affects", r"impacts", r"changes", r"blocks", r"影响", r"阻塞"),
    "invalidates": (
        r"invalidates?",
        r"makes\s+obsolete",
        r"no\s+longer\s+valid",
        r"失效",
        r"废弃",
    ),
}

_SYNC_MAX_CANDIDATES = 24
_PRESENTATION_CONTEXT_RE = re.compile(
    r"\b(?:brand|branding|logo|icon|color|colour|theme|mockup|marketing|"
    r"screenshot|badge|chart|diagram|label|visual\s+style|"
    r"approval\s+(?:schedule|workflow))\b|"
    r"品牌|标志|图标|配色|主题|样式|营销|截图|图表|标签|审批排期|审核排期",
    flags=re.I,
)
_RESPONSIBILITY_ROLE_RE = re.compile(
    r"\b(?:owns?|owner|maintains?|maintainer|responsible|operates?|"
    r"on[- ]?call|steward)\b|负责|负责人|维护|拥有|归属|值班|管理",
    flags=re.I,
)
_RELATION_TARGET_ELIGIBILITY_RULES: dict[
    str, tuple[tuple[str, re.Pattern[str]], ...]
] = {
    "depends_on": (
        (
            "operational_resource",
            re.compile(
                r"\b(?:service|api|endpoint|database|datastore|cache|queue|cluster|"
                r"node|host|port|credential|secret|network|dns|storage)\b|"
                r"服务|接口|端点|数据库|数据存储|缓存|队列|集群|节点|主机|端口|凭据|网络|存储",
                flags=re.I,
            ),
        ),
        (
            "operational_state",
            re.compile(
                r"\b(?:available|availability|health|healthy|readiness|uptime|"
                r"latency|capacity|connectivity|connection|recovery|backup|runbook|"
                r"failover|ping)\b|可用|健康|就绪|延迟|容量|连接|恢复|备份|运行手册|故障转移",
                flags=re.I,
            ),
        ),
        (
            "runtime_capability",
            re.compile(
                r"\b(?:runs?|listens?|stores?|provides?|serves?|connects?)\b|"
                r"运行|监听|存储|提供|承载|连接",
                flags=re.I,
            ),
        ),
        ("responsible_actor", _RESPONSIBILITY_ROLE_RE),
    ),
    "owned_by": (
        ("responsibility", _RESPONSIBILITY_ROLE_RE),
    ),
    "affects": (
        (
            "observable_state",
            re.compile(
                r"\b(?:metric|metrics|latency|throughput|capacity|availability|"
                r"health|state|status|behavior|behaviour|drain|error|failure|"
                r"traffic|load|performance|workflow|process)\b|"
                r"指标|延迟|吞吐|容量|可用|健康|状态|行为|排空|错误|故障|流量|负载|性能|流程",
                flags=re.I,
            ),
        ),
    ),
    "invalidates": (
        (
            "obsolete_artifact",
            re.compile(
                r"\b(?:old|legacy|deprecated|obsolete|previous|superseded|v\d+|"
                r"version|config|configuration|policy|fact|value|setting|rule|"
                r"assumption)\b|旧|旧版|过时|废弃|失效|版本|配置|策略|事实|值|设置|规则|假设",
                flags=re.I,
            ),
        ),
    ),
}

_RELATION_DENIAL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "depends_on": (
        re.compile(
            r"\b(?:(?:do(?:es)?|did|will|would|can|could|should)\s+not|"
            r"(?:doesn't|don't|didn't|won't|wouldn't|cannot|can't|couldn't|shouldn't))\s+"
            r"(?:depend\s+on|require|rely\s+on|use)\b",
            flags=re.I,
        ),
        re.compile(
            r"\b(?:is|are|was|were)\s+not\s+"
            r"(?:dependent\s+on|required\s+by|used\s+by|part\s+of)\b",
            flags=re.I,
        ),
        re.compile(
            r"\b(?:independent\s+of|unrelated\s+to|not\s+used\s+by|"
            r"provides?\s+no\s+runtime|no\s+runtime\s+(?:role|service|dependency))\b",
            flags=re.I,
        ),
        re.compile(
            r"\bno\s+longer\s+(?:depends?\s+on|requires?|relies?\s+on|uses?)\b",
            flags=re.I,
        ),
        re.compile(r"不(?:再)?依赖|无需|不需要|未依赖|不使用|无运行时依赖"),
    ),
    "owned_by": (
        re.compile(
            r"\b(?:is|are|was|were)\s+not\s+"
            r"(?:owned|maintained|operated)\s+by\b",
            flags=re.I,
        ),
        re.compile(
            r"\b(?:do(?:es)?|did)\s+not\s+belong\s+to\b|"
            r"\bnot\s+(?:responsible|owner|maintainer|on[- ]?call)\b",
            flags=re.I,
        ),
        re.compile(
            r"\bno\s+longer\s+(?:(?:owned|maintained|operated)\s+by|belongs?\s+to)\b",
            flags=re.I,
        ),
        re.compile(r"不(?:再)?(?:归属|属于|负责|维护|管理)|并非.{0,40}负责人"),
    ),
    "affects": (
        re.compile(
            r"\b(?:(?:do(?:es)?|did|will|would|can|could)\s+not|"
            r"(?:doesn't|don't|didn't|won't|wouldn't|cannot|can't|couldn't))\s+"
            r"(?:affect|impact|change|block)\b",
            flags=re.I,
        ),
        re.compile(r"\b(?:unaffected\s+by|no\s+(?:effect|impact)\s+on)\b", flags=re.I),
        re.compile(
            r"\bno\s+longer\s+(?:affects?|impacts?|changes?|blocks?)\b",
            flags=re.I,
        ),
        re.compile(r"不(?:再|会)?(?:影响|阻塞|改变)|未影响|不受.{0,40}影响"),
    ),
    "invalidates": (
        re.compile(
            r"\b(?:(?:do(?:es)?|did|will|would|can|could)\s+not|"
            r"(?:doesn't|don't|didn't|won't|wouldn't|cannot|can't|couldn't))\s+"
            r"(?:invalidate|make\s+.{0,40}\s+obsolete)\b",
            flags=re.I,
        ),
        re.compile(
            r"\b(?:(?:remains?|still)\s+valid|not\s+(?:obsolete|invalid|deprecated))\b",
            flags=re.I,
        ),
        re.compile(
            r"\bno\s+longer\s+(?:invalidates?|makes?\s+.{0,40}\s+obsolete)\b",
            flags=re.I,
        ),
        re.compile(r"不(?:再|会)?(?:使.{0,40}失效|废弃|作废)|仍(?:然)?有效|未失效"),
    ),
}


def _high_frequency_relation_entities(rows: list[dict[str, Any]]) -> set[str]:
    """Return corpus-wide entities too broad to justify pairwise graph edges.

    The absolute floor keeps tiny fixtures and small installations stable. On
    larger stores, document frequency catches new broad labels without waiting
    for an operator to extend a static stoplist after graph fan-out has happened.
    """

    threshold = high_frequency_document_threshold(len(rows))
    if threshold is None:
        return set()
    document_frequency: dict[str, int] = {}
    for row in rows:
        per_memory = {
            _normalize_relation_entity(entity)
            for entity in set(row.get("entities") or set())
            if _distinctive_entity(entity)
        }
        for entity in per_memory:
            document_frequency[entity] = document_frequency.get(entity, 0) + 1
    return {
        entity for entity, count in document_frequency.items() if count >= threshold
    }


def _clean_scope_ids(scope_ids: Iterable[str] | None) -> list[str]:
    return sorted({str(scope_id) for scope_id in (scope_ids or []) if str(scope_id)})


def _load_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_rows(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
) -> list[sqlite3.Row]:
    where = [lifecycle_visible_sql("m")]
    params: list[Any] = []
    scopes = _clean_scope_ids(scope_ids)
    if scopes:
        where.append(f"m.scope_id IN ({','.join('?' for _ in scopes)})")
        params.extend(scopes)
    ids = sorted({str(memory_id) for memory_id in (memory_ids or []) if str(memory_id)})
    if ids:
        where.append(f"m.id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    rows = conn.execute(
        f"""
        SELECT m.id, m.scope_id, m.target, m.content, m.summary, m.created_at, m.updated_at, m.metadata
        FROM memories m
        WHERE {" AND ".join(where)}
        ORDER BY m.updated_at DESC, m.id DESC
        """,
        params,
    ).fetchall()
    return rows


def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _load_metadata(row["metadata"])
    content = str(row["content"] or "")
    return {
        "id": str(row["id"]),
        "scope_id": str(row["scope_id"]),
        "target": str(row["target"]),
        "content": content,
        "summary": str(row["summary"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "metadata": metadata,
        "entities": set(metadata_entities(metadata, content, str(row["target"] or ""))),
    }


def scope_high_frequency_relation_entities_by_scope(
    conn: sqlite3.Connection,
    scope_ids: Iterable[str] | None = None,
) -> dict[str, set[str]]:
    """Read per-scope blocked entities from the incremental frequency index.

    The query is proportional to the returned blocked set, not the number of
    memories in a scope.  Legacy/backlog scopes fail explicitly so callers can
    enqueue bounded maintenance instead of silently performing a truth scan.
    """

    snapshots = relation_frequency_snapshots_by_scope(conn, scope_ids)
    unavailable = sorted(scope for scope, snapshot in snapshots.items() if snapshot is None)
    if unavailable:
        raise RelationFrequencyIndexNotReady(
            "relation frequency index is not current for scopes: "
            + ", ".join(unavailable[:8])
        )
    return {
        scope: set(snapshot["blocked_entities"])
        for scope, snapshot in snapshots.items()
        if snapshot is not None
    }


def scope_high_frequency_relation_entities(
    conn: sqlite3.Connection,
    scope_ids: Iterable[str] | None = None,
) -> set[str]:
    """Return the legacy union view of per-scope blocked entities.

    Queue processing passes exactly one scope. Multi-scope relation scans must
    use :func:`scope_high_frequency_relation_entities_by_scope` so one tenant's
    broad entity cannot suppress another tenant's graph evidence.
    """

    blocked: set[str] = set()
    for scope_blocked in scope_high_frequency_relation_entities_by_scope(
        conn, scope_ids
    ).values():
        blocked.update(scope_blocked)
    return blocked


def _existing_relation_types(
    conn: sqlite3.Connection, memory_ids: Iterable[str]
) -> set[tuple[str, str, str]]:
    ids = sorted({str(memory_id) for memory_id in memory_ids if str(memory_id)})
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""
            SELECT source_memory_id, target_memory_id, relation_type
            FROM memory_relations
            WHERE source_memory_id IN ({placeholders})
               OR target_memory_id IN ({placeholders})
            """,
            [*ids, *ids],
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {
        (
            str(row["source_memory_id"]),
            str(row["target_memory_id"]),
            str(row["relation_type"]).strip().lower(),
        )
        for row in rows
    }


def _pair_has_relation(
    existing: set[tuple[str, str, str]], left_id: str, right_id: str, relation_type: str
) -> bool:
    relation = str(relation_type).strip().lower()
    return (left_id, right_id, relation) in existing or (
        right_id,
        left_id,
        relation,
    ) in existing


def _pair_has_contradiction(
    existing: set[tuple[str, str, str]], left_id: str, right_id: str
) -> bool:
    return _pair_has_relation(existing, left_id, right_id, "contradicts")


def _pair_key(left_id: str, right_id: str) -> tuple[str, str]:
    left = str(left_id)
    right = str(right_id)
    return (left, right) if left <= right else (right, left)


def _delete_generated_relation_edges_for_pairs(
    conn: sqlite3.Connection, pairs: Iterable[tuple[str, str]]
) -> int:
    pair_keys = sorted(
        {
            _pair_key(left_id, right_id)
            for left_id, right_id in pairs
            if str(left_id) and str(right_id)
        }
    )
    if not pair_keys:
        return 0
    before = conn.total_changes
    for left_id, right_id in pair_keys:
        conn.execute(
            """
            DELETE FROM memory_relations
            WHERE (
                    (source_memory_id = ? AND target_memory_id = ?)
                 OR (source_memory_id = ? AND target_memory_id = ?)
            )
              AND LOWER(COALESCE(note, '')) LIKE 'relation-extraction:%'
            """,
            (left_id, right_id, right_id, left_id),
        )
    return conn.total_changes - before


def _same_topic(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    blocked_entities: set[str] | None = None,
) -> tuple[bool, float, str]:
    if left["scope_id"] != right["scope_id"] or left["target"] != right["target"]:
        return False, 0.0, ""
    entity_overlap = {
        entity
        for entity in set(left["entities"]) & set(right["entities"])
        if _distinctive_entity(entity, blocked_entities=blocked_entities)
    }
    similarity = semantic_similarity(str(left["content"]), str(right["content"]))
    if entity_overlap and similarity >= 0.40:
        return (
            True,
            max(0.55, min(0.95, 0.45 + similarity)),
            f"shared_entities={','.join(sorted(entity_overlap)[:4])}; similarity={similarity:.3f}",
        )
    if similarity >= 0.68:
        return True, min(0.9, similarity), f"similarity={similarity:.3f}"
    return False, 0.0, ""


def _supersedes(
    newer: dict[str, Any],
    older: dict[str, Any],
    *,
    blocked_entities: set[str] | None = None,
) -> tuple[bool, float, str]:
    if (
        newer["id"] == older["id"]
        or newer["scope_id"] != older["scope_id"]
        or newer["target"] != older["target"]
    ):
        return False, 0.0, ""
    if newer["updated_at"] < older["updated_at"]:
        return False, 0.0, ""
    text = str(newer["content"] or "").lower()
    older_text = str(older["content"] or "").lower()
    shared_entities = {
        entity
        for entity in set(newer["entities"]) & set(older["entities"])
        if _distinctive_entity(entity, blocked_entities=blocked_entities)
    }
    similarity = semantic_similarity(text, older_text)
    explicit_new = bool(_SUPERSEDES_RE.search(text))
    old_marker = bool(_OLD_RE.search(older_text))
    if shared_entities and explicit_new and (old_marker or similarity >= 0.24):
        confidence = max(0.65, min(0.98, 0.55 + similarity))
        return (
            True,
            confidence,
            f"explicit_supersedes; shared_entities={','.join(sorted(shared_entities)[:4])}; similarity={similarity:.3f}",
        )
    return False, 0.0, ""


def _entity_pattern(entity: str) -> str:
    escaped = re.escape(entity)
    if re.fullmatch(r"[a-z0-9][a-z0-9 .:/#-]*", entity):
        return rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return escaped


def _relation_clause_denies(relation_type: str, clause: str) -> bool:
    """Return whether an entity-bearing clause explicitly denies a typed role."""

    return any(
        pattern.search(str(clause or "")) is not None
        for pattern in _RELATION_DENIAL_PATTERNS.get(str(relation_type), ())
    )


def _entity_explicitly_excluded(clause: str, entity: str) -> bool:
    """Return whether one semantic clause explicitly excludes this entity."""

    entity_re = _entity_pattern(entity)
    patterns = (
        rf"\b(?:but\s+)?not(?!\s+(?:only|just)\b)\s+(?:the\s+)?{entity_re}",
        rf"\b(?:except(?:\s+for)?|excluding|other\s+than|rather\s+than)\s+(?:the\s+)?{entity_re}",
        rf"(?:不包括|排除|而非|不是|但不(?:涉及|影响|依赖|需要|归属|属于)?)\s*{entity_re}",
        rf"{entity_re}\s*(?:除外|不包括在内)",
    )
    return any(re.search(pattern, clause, re.I) for pattern in patterns)


def _trigger_entity_evidence(
    text: str,
    entity: str,
    triggers: tuple[str, ...],
    *,
    allow_obsolete_entity_before_trigger: bool = False,
    relation_type: str = "",
) -> str:
    """Return clause-local positive predicate evidence for one entity."""

    if not _distinctive_entity(entity):
        return ""
    trigger_then_entity, entity_then_trigger = _trigger_entity_patterns(entity, triggers)
    for clause in _entity_relation_clauses(text, entity):
        if _entity_explicitly_excluded(clause, entity):
            continue
        if relation_type and _relation_clause_denies(relation_type, clause):
            continue
        if relation_type and _PRESENTATION_CONTEXT_RE.search(clause) is not None:
            # A predicate-like phrase inside a screenshot, chart, label, or
            # approval narrative describes presentation workflow rather than a
            # durable runtime relation. Separate operational clauses are still
            # evaluated independently by the clause iterator.
            continue
        match = trigger_then_entity.search(clause)
        if match is not None:
            return clause[
                max(0, match.start() - 80) : min(len(clause), match.end() + 120)
            ]
        if not allow_obsolete_entity_before_trigger:
            continue
        before_match = entity_then_trigger.search(clause)
        if before_match is None or _OLD_RE.search(before_match.group(0)) is None:
            continue
        return clause[
            max(0, before_match.start() - 80) : min(
                len(clause), before_match.end() + 120
            )
        ]
    return ""


def _trigger_mentions_entity(
    text: str,
    entity: str,
    triggers: tuple[str, ...],
    *,
    allow_obsolete_entity_before_trigger: bool = False,
    relation_type: str = "",
) -> bool:
    return bool(
        _trigger_entity_evidence(
            text,
            entity,
            triggers,
            allow_obsolete_entity_before_trigger=allow_obsolete_entity_before_trigger,
            relation_type=relation_type,
        )
    )


@lru_cache(maxsize=16384)
def _trigger_entity_patterns(
    entity: str, triggers: tuple[str, ...]
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    entity_re = _entity_pattern(entity)
    trigger_group = "(?:" + "|".join(f"(?:{trigger})" for trigger in triggers) + ")"
    flags = re.I | re.S
    return (
        re.compile(rf"{trigger_group}.{{0,120}}{entity_re}", flags=flags),
        re.compile(rf"{entity_re}.{{0,120}}{trigger_group}", flags=flags),
    )


def _entity_relation_clauses(text: str, entity: str) -> list[str]:
    """Return sentence-sized semantic clauses that actually mention the entity."""

    entity_re = re.compile(_entity_pattern(entity), flags=re.I)
    clauses = re.split(r"(?<=[.!?;。！？；])|\n+", str(text or ""))
    return [
        clause.strip()
        for clause in clauses
        if clause.strip() and entity_re.search(clause) is not None
    ]


def _target_relation_eligibility(
    relation_type: str,
    target_text: str,
    entity: str,
) -> tuple[bool, list[str]]:
    """Require relation-specific evidence in the target entity's semantic slot.

    Shared words with the source are intentionally irrelevant: a branding UI can
    repeat ``availability``, ``deployment``, and an entity name without being an
    operational dependency. Each target clause must instead satisfy the typed
    relation's own role contract. Unclassified clauses fail closed.
    """

    rules = _RELATION_TARGET_ELIGIBILITY_RULES.get(relation_type, ())
    if not rules:
        return False, []
    for clause in _entity_relation_clauses(target_text, entity):
        if _entity_explicitly_excluded(clause, entity):
            continue
        if _relation_clause_denies(relation_type, clause):
            continue
        if _PRESENTATION_CONTEXT_RE.search(clause) is not None:
            continue
        matched_rules = [label for label, pattern in rules if pattern.search(clause)]
        if relation_type == "depends_on" and not (
            set(matched_rules) - {"operational_resource"}
        ):
            # A resource noun identifies the object class but says nothing about
            # its current operational role. Require state, capability, or actor
            # evidence before materializing a durable dependency edge.
            continue
        if matched_rules:
            return True, matched_rules
    return False, []


def _maximal_entity_evidence(
    matches: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Discard entity fragments when a longer matched entity owns the slot."""

    normalized = [
        (entity, evidence, _normalize_relation_entity(entity))
        for entity, evidence in matches
    ]
    output: list[tuple[str, str]] = []
    for entity, evidence, value in normalized:
        if any(
            value != other
            and re.search(
                rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", other
            )
            for _, _, other in normalized
        ):
            continue
        output.append((entity, evidence))
    return output


def _entity_is_same_or_fragment(entity: str, container: str) -> bool:
    """Return whether an entity is the same semantic slot or its token fragment."""

    value = _normalize_relation_entity(entity)
    owner = _normalize_relation_entity(container)
    if not value or not owner:
        return False
    return re.search(
        rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])",
        owner,
    ) is not None


def _typed_relation(
    source: dict[str, Any],
    target: dict[str, Any],
    relation_type: str,
    *,
    blocked_entities: set[str] | None = None,
) -> tuple[bool, float, str]:
    if source["id"] == target["id"] or source["scope_id"] != target["scope_id"]:
        return False, 0.0, ""
    triggers = _TYPED_RELATION_TRIGGERS.get(relation_type)
    if not triggers:
        return False, 0.0, ""
    text = str(source["content"] or "").lower()
    entity_evidence: list[tuple[str, str]] = []
    target_entities = sorted(
        set(target["entities"]), key=lambda value: (-len(str(value)), str(value))
    )
    excluded_source_slots = {
        str(entity)
        for entity in target_entities
        if any(
            _entity_explicitly_excluded(clause, str(entity))
            for clause in _entity_relation_clauses(text, str(entity))
        )
    }
    for entity in target_entities:
        if not _distinctive_entity(entity, blocked_entities=blocked_entities):
            continue
        if any(
            _entity_is_same_or_fragment(str(entity), excluded)
            for excluded in excluded_source_slots
        ):
            continue
        evidence = _trigger_entity_evidence(
            text,
            str(entity),
            triggers,
            allow_obsolete_entity_before_trigger=relation_type == "invalidates",
            relation_type=relation_type,
        )
        if evidence:
            entity_evidence.append((str(entity), evidence))

    eligible_entities: list[str] = []
    eligibility_rules: set[str] = set()
    target_text = str(target["content"] or "").lower()
    for entity, _source_evidence in _maximal_entity_evidence(entity_evidence):
        eligible, matched_rules = _target_relation_eligibility(
            relation_type, target_text, entity
        )
        if not eligible:
            continue
        eligible_entities.append(entity)
        eligibility_rules.update(matched_rules)
    if not eligible_entities:
        return False, 0.0, ""
    confidence = (
        0.72
        if relation_type == "invalidates"
        else 0.78
        if relation_type in {"depends_on", "owned_by"}
        else 0.72
    )
    return (
        True,
        confidence,
        f"triggered_{relation_type}; matched_entities={','.join(eligible_entities[:4])}; "
        f"target_eligibility={','.join(sorted(eligibility_rules)[:6])}",
    )


def _candidate(
    *,
    source_id: str,
    target_id: str,
    relation_type: str,
    confidence: float,
    note: str,
) -> dict[str, Any]:
    if relation_type not in EXTRACTED_RELATION_TYPES:
        raise ValueError(
            f"extractor relation type is absent from central policy: {relation_type}"
        )
    return {
        "source_memory_id": source_id,
        "target_memory_id": target_id,
        "relation_type": relation_type,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "note": note,
    }


def extract_relation_candidates(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
    max_pairs: int = 5000,
) -> list[dict[str, Any]]:
    candidates, _, _ = _relation_candidate_scan(
        conn, scope_ids=scope_ids, memory_ids=memory_ids, max_pairs=max_pairs
    )
    return candidates


def _relation_candidate_scan(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
    focus_memory_ids: Iterable[str] | None = None,
    max_pairs: int = 5000,
    blocked_entities: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]], bool]:
    """Scan memory text for deterministic relation candidates.

    The scanner favors conservative, explainable edges because graph evidence influences recall ranking and conflict review."""
    rows = [
        _row_payload(row)
        for row in _memory_rows(conn, scope_ids=scope_ids, memory_ids=memory_ids)
    ]
    selected_scopes = _clean_scope_ids(scope_ids) or sorted(
        {str(row["scope_id"]) for row in rows}
    )
    high_frequency_entities_by_scope = (
        {scope_id: set(blocked_entities) for scope_id in selected_scopes}
        if blocked_entities is not None
        else scope_high_frequency_relation_entities_by_scope(conn, selected_scopes)
    )
    output: dict[tuple[str, str, str], dict[str, Any]] = {}
    existing_relations = _existing_relation_types(
        conn, [str(row["id"]) for row in rows]
    )
    focus_ids = {
        str(memory_id) for memory_id in (focus_memory_ids or []) if str(memory_id)
    }
    pair_budget = max(1, int(max_pairs or 5000))
    compared = 0
    compared_pairs: set[tuple[str, str]] = set()
    budget_exceeded = False

    def iter_pairs() -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
        if not focus_ids:
            for index, left_row in enumerate(rows):
                for right_row in rows[index + 1 :]:
                    yield left_row, right_row
            return
        seen: set[tuple[int, int]] = set()
        focus_indexes = [
            index for index, row in enumerate(rows) if str(row["id"]) in focus_ids
        ]
        for focus_index in focus_indexes:
            for peer_index in range(len(rows)):
                if peer_index == focus_index:
                    continue
                pair_index = (
                    min(focus_index, peer_index),
                    max(focus_index, peer_index),
                )
                if pair_index in seen:
                    continue
                seen.add(pair_index)
                yield rows[pair_index[0]], rows[pair_index[1]]

    for left, right in iter_pairs():
        if left["scope_id"] != right["scope_id"]:
            continue
        compared += 1
        if compared > pair_budget:
            budget_exceeded = True
            break
        if left["id"] == right["id"]:
            continue
        compared_pairs.add(_pair_key(str(left["id"]), str(right["id"])))
        pair_blocked_entities = high_frequency_entities_by_scope.get(
            str(left["scope_id"]), set()
        )
        if _pair_has_contradiction(
            existing_relations, str(left["id"]), str(right["id"])
        ):
            continue
        same, confidence, note = _same_topic(
            left, right, blocked_entities=pair_blocked_entities
        )
        if same:
            # same_topic is symmetric; store both directed edges so graph
            # evidence works from either result without a second query.
            for source, target in ((left, right), (right, left)):
                key = (source["id"], target["id"], "same_topic")
                output[key] = _candidate(
                    source_id=source["id"],
                    target_id=target["id"],
                    relation_type="same_topic",
                    confidence=confidence,
                    note=note,
                )
        for newer, older in ((left, right), (right, left)):
            supersedes, super_confidence, super_note = _supersedes(
                newer,
                older,
                blocked_entities=pair_blocked_entities,
            )
            if supersedes:
                key = (newer["id"], older["id"], "supersedes")
                output[key] = _candidate(
                    source_id=newer["id"],
                    target_id=older["id"],
                    relation_type="supersedes",
                    confidence=super_confidence,
                    note=super_note,
                )
        for source, target in ((left, right), (right, left)):
            for relation_type in sorted(_TYPED_RELATION_TRIGGERS):
                matched, typed_confidence, typed_note = _typed_relation(
                    source,
                    target,
                    relation_type,
                    blocked_entities=pair_blocked_entities,
                )
                if not matched:
                    continue
                key = (source["id"], target["id"], relation_type)
                output[key] = _candidate(
                    source_id=source["id"],
                    target_id=target["id"],
                    relation_type=relation_type,
                    confidence=typed_confidence,
                    note=typed_note,
                )
    return (
        sorted(
            output.values(),
            key=lambda item: (
                item["relation_type"],
                item["source_memory_id"],
                item["target_memory_id"],
            ),
        ),
        compared_pairs,
        budget_exceeded,
    )


def rebuild_extracted_relations(
    conn: sqlite3.Connection,
    *,
    scope_ids: Iterable[str] | None = None,
    memory_ids: Iterable[str] | None = None,
    dry_run: bool = True,
    batch_id: str = "manual",
    max_pairs: int = 5000,
    focus_memory_ids: Iterable[str] | None = None,
    max_candidates: int = 0,
    commit: bool = True,
    blocked_entities: set[str] | None = None,
    preplanned_cleanup_pairs: Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Rebuild extracted relation companion rows from current SQLite memories.

    The rebuild path is deterministic so graph hygiene can recover from stale companion state without changing truth rows."""
    candidates, compared_pairs, budget_exceeded = _relation_candidate_scan(
        conn,
        scope_ids=scope_ids,
        memory_ids=memory_ids,
        focus_memory_ids=focus_memory_ids,
        max_pairs=max_pairs,
        blocked_entities=blocked_entities,
    )
    compared_pairs.update(
        _pair_key(left_id, right_id)
        for left_id, right_id in (preplanned_cleanup_pairs or [])
        if str(left_id) and str(right_id) and str(left_id) != str(right_id)
    )
    candidate_cap = max(0, int(max_candidates or 0))
    if budget_exceeded:
        # A truncated scan cannot know which generated edges are stale.  Fail
        # before every delete/insert and require a later full-budget rebuild.
        return {
            "ok": False,
            "status": "blocked_budget",
            "dry_run": bool(dry_run),
            "blocked": True,
            "error": f"relation pair budget exceeded: max_pairs={max(1, int(max_pairs or 5000))}",
            "candidate_count": len(candidates),
            "max_pairs": max(1, int(max_pairs or 5000)),
            "inserted": 0,
            "deleted": 0,
            "compared_pair_count": len(compared_pairs),
            "budget_exceeded": True,
            "full_rebuild_required": True,
            "candidates": candidates[:50],
        }
    fanout_blocked = bool(candidate_cap and len(candidates) > candidate_cap)
    if fanout_blocked:
        # Fail before deleting stale generated edges. A noisy update must not
        # replace a reviewed graph with a partial or high-fan-out candidate set.
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "blocked": True,
            "error": f"relation candidate fan-out exceeded cap: {len(candidates)} > {candidate_cap}",
            "candidate_count": len(candidates),
            "max_candidates": candidate_cap,
            "inserted": 0,
            "deleted": 0,
            "compared_pair_count": len(compared_pairs),
            "budget_exceeded": budget_exceeded,
            "candidates": candidates[:50],
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "candidate_count": len(candidates),
            "inserted": 0,
            "deleted": 0,
            "compared_pair_count": len(compared_pairs),
            "budget_exceeded": budget_exceeded,
            "candidates": candidates[:50],
        }
    now = now_iso()
    note_prefix = f"relation-extraction:{batch_id}"
    savepoint = "relation_extraction_apply"
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        deleted = _delete_generated_relation_edges_for_pairs(conn, compared_pairs)
        before_insert = conn.total_changes
        for item in candidates:
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item["source_memory_id"],
                    item["target_memory_id"],
                    item["relation_type"],
                    item["confidence"],
                    f"{note_prefix}; {item['note']}",
                    now,
                ),
            )
        inserted = conn.total_changes - before_insert
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_transaction:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return {
        "ok": True,
        "dry_run": False,
        "candidate_count": len(candidates),
        "inserted": inserted,
        "deleted": deleted,
        "compared_pair_count": len(compared_pairs),
        "budget_exceeded": budget_exceeded,
        "candidates": candidates[:50],
    }


def sync_extracted_relations_for_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    scope_ids: Iterable[str] | None = None,
    batch_id: str = "store",
    max_pairs: int = 1000,
    local_peer_limit: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Refresh a complete cap-bounded focus neighbourhood or fail closed."""

    memory_id = str(memory_id or "")
    if not memory_id:
        return {
            "ok": False,
            "dry_run": False,
            "candidate_count": 0,
            "inserted": 0,
            "error": "missing memory_id",
        }
    del scope_ids  # durable work scopes are derived from indexed truth
    focus_work = load_relation_focus_work(conn, memory_id)
    new_row = conn.execute(
        "SELECT scope_id FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if new_row is None and focus_work is None:
        return {
            "ok": False,
            "dry_run": False,
            "candidate_count": 0,
            "inserted": 0,
            "error": "memory_id not found and no durable focus work exists",
        }
    row_scope_id = str(new_row[0] or "") if new_row is not None else ""
    work_scopes = list(focus_work["scope_ids"]) if focus_work is not None else []
    if row_scope_id:
        work_scopes = sorted(set(work_scopes) | {row_scope_id})
    if not work_scopes:
        return {
            "ok": False,
            "dry_run": False,
            "candidate_count": 0,
            "inserted": 0,
            "error": "focus work has no durable scope",
        }
    # Generated relation policy is scope-owned.  Accessible scopes may include
    # shared pools, but one focus mutation must only compare peers in its own
    # durable scope.
    scopes = [row_scope_id] if row_scope_id else []
    snapshots = {
        scope: relation_frequency_snapshot(conn, scope) for scope in work_scopes
    }
    if any(snapshot is None for snapshot in snapshots.values()):
        failure_states = {
            scope: relation_frequency_scope_failure_status(conn, scope)
            for scope, snapshot in snapshots.items()
            if snapshot is None
        }
        poisoned_scopes = {
            scope
            for scope, failure_state in failure_states.items()
            if failure_state == "dead_letter"
        }
        blocked = bool(poisoned_scopes)
        deferred_reason = (
            "frequency_maintenance_poisoned"
            if blocked
            else "relation frequency index backfill or repair is pending"
        )
        for scope, snapshot in snapshots.items():
            if snapshot is None:
                mark_relation_scope_degraded(
                    conn,
                    scope_id=scope,
                    reason_code=(
                        "frequency_maintenance_poisoned"
                        if scope in poisoned_scopes
                        else "frequency_receipt_stale"
                    ),
                    operator_action_required=scope in poisoned_scopes,
                )
        if commit:
            conn.commit()
        return {
            "ok": True,
            "dry_run": False,
            "blocked": blocked,
            "status": "degraded" if blocked else "synced_deferred",
            "immediate_status": (
                "frequency_maintenance_poisoned" if blocked else "index_pending"
            ),
            "candidate_count": 0,
            "inserted": 0,
            "deleted": 0,
            "deferred": not blocked,
            "deferred_reason": deferred_reason,
            "full_rebuild_required": False,
            "total_peer_count": 0,
            "selected_peer_count": 0,
            "compared_pairs": 0,
        }
    current_snapshot = snapshots.get(row_scope_id) if row_scope_id else None
    blocked_entities = (
        set(current_snapshot["blocked_entities"])
        if current_snapshot is not None
        else set()
    )
    snapshot_revision = (
        int(current_snapshot.get("corpus_revision") or 0)
        if current_snapshot is not None
        else 0
    )
    generations: dict[str, sqlite3.Row | tuple[Any, ...] | None] = {}
    generation_pending = False
    generation_blocked = False
    generation_reason = ""
    for scope, snapshot in snapshots.items():
        assert snapshot is not None
        generation = conn.execute(
            """
            SELECT state, reason_code, active_revision, target_revision
            FROM relation_scope_containment WHERE scope_id=?
            """,
            (scope,),
        ).fetchone()
        generations[scope] = generation
        revision = int(snapshot.get("corpus_revision") or 0)
        ready = bool(
            generation
            and (
                (
                    str(generation[0]) == "ready"
                    and int(generation[2] or 0)
                    == int(generation[3] or 0)
                    == revision
                )
                or (
                    str(generation[0]) == "degraded"
                    and str(generation[1] or "") == "focus_relation_sync_pending"
                    and int(generation[3] or 0) == revision
                )
            )
        )
        if not ready:
            generation_pending = True
            generation_blocked = generation_blocked or bool(
                generation and str(generation[0]) in {"blocked", "disabled"}
            )
            if not generation_reason:
                generation_reason = (
                    str(generation[1] or "relation_generation_pending")
                    if generation
                    else "containment_state_missing"
                )
    if generation_pending:
        if commit:
            conn.commit()
        return {
            "ok": True,
            "dry_run": False,
            "blocked": generation_blocked,
            "status": "synced_deferred",
            "immediate_status": "relation_generation_pending",
            "candidate_count": 0,
            "inserted": 0,
            "deleted": 0,
            "deferred": True,
            "deferred_reason": (
                generation_reason or "relation_generation_pending"
            ),
            "full_rebuild_required": False,
            "total_peer_count": 0,
            "selected_peer_count": 0,
            "compared_pairs": 0,
        }
    worker_budget = max(1, min(int(max_pairs or 1000), 5000))
    bounded_pairs = max(
        1,
        min(
            worker_budget,
            int(local_peer_limit) if local_peer_limit is not None else worker_budget,
        ),
    )
    plan_scope = row_scope_id or work_scopes[0]
    plan = plan_focus_relation_pairs(
        conn,
        scope_id=plan_scope,
        memory_id=memory_id,
        blocked_entities=blocked_entities,
        candidate_cap=bounded_pairs,
        target_revision=snapshot_revision,
    )
    if plan.blocked:
        for scope, snapshot in snapshots.items():
            assert snapshot is not None
            mark_relation_scope_degraded(
                conn,
                scope_id=scope,
                reason_code=plan.reason_code,
                target_revision=int(snapshot.get("corpus_revision") or 0),
                candidate_cap=bounded_pairs,
                affected_count=plan.affected_count,
                operator_action_required=True,
            )
        if commit:
            conn.commit()
        return {
            "ok": True,
            "dry_run": False,
            "blocked": True,
            "status": "degraded",
            "immediate_status": "candidate_cap_exceeded",
            "candidate_count": 0,
            "inserted": 0,
            "deleted": 0,
            "deferred": False,
            "deferred_reason": plan.reason_code,
            "full_rebuild_required": False,
            "total_peer_count": plan.affected_count,
            "selected_peer_count": 0,
            "compared_pairs": 0,
        }
    peer_ids = sorted(
        right if left == memory_id else left for left, right in plan.pairs
    )
    if not conn.in_transaction:
        conn.execute("BEGIN")
    generation_savepoint = "relation_focus_generation_apply"
    conn.execute(f"SAVEPOINT {generation_savepoint}")
    immediate = rebuild_extracted_relations(
        conn,
        scope_ids=scopes,
        memory_ids=[memory_id, *peer_ids],
        dry_run=False,
        batch_id=batch_id,
        max_pairs=max(1, len(peer_ids)),
        focus_memory_ids=[memory_id],
        max_candidates=_SYNC_MAX_CANDIDATES,
        commit=False,
        blocked_entities=blocked_entities,
        preplanned_cleanup_pairs=plan.pairs,
    )
    immediate_ok = bool(immediate.get("ok"))
    generation_cas_miss = False
    if immediate_ok:
        if focus_work is not None:
            immediate_ok = complete_relation_focus_work(
                conn,
                memory_id=memory_id,
                work_generation=int(focus_work["work_generation"]),
            )
        if immediate_ok:
            for scope, snapshot in snapshots.items():
                assert snapshot is not None
                if relation_focus_scope_has_debt(conn, scope):
                    continue
                immediate_ok = confirm_relation_scope_focus_generation(
                    conn,
                    scope_id=scope,
                    revision=int(snapshot.get("corpus_revision") or 0),
                    blocked_entities=set(snapshot["blocked_entities"]),
                    affected_count=len(plan.pairs),
                )
                if not immediate_ok:
                    break
        if not immediate_ok:
            generation_cas_miss = True
            immediate = {
                "ok": False,
                "status": "generation_cas_miss",
                "error": "relation focus generation changed before confirmation",
                "candidate_count": 0,
                "inserted": 0,
                "deleted": 0,
                "compared_pair_count": 0,
            }
    if immediate_ok:
        conn.execute(f"RELEASE SAVEPOINT {generation_savepoint}")
    else:
        conn.execute(f"ROLLBACK TO SAVEPOINT {generation_savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {generation_savepoint}")
    deferred_reason = str(immediate.get("error") or "") if not immediate_ok else ""
    if not immediate_ok and not generation_cas_miss:
        for scope, snapshot in snapshots.items():
            assert snapshot is not None
            mark_relation_scope_degraded(
                conn,
                scope_id=scope,
                reason_code="relation_candidate_cap_exceeded",
                target_revision=int(snapshot.get("corpus_revision") or 0),
                candidate_cap=bounded_pairs,
                affected_count=plan.affected_count,
                operator_action_required=True,
            )
    if commit:
        conn.commit()
    payload = dict(immediate)
    payload.pop("error", None)
    payload.update(
        {
            "ok": True,
            "blocked": not immediate_ok and not generation_cas_miss,
            "status": (
                "synced_deferred"
                if generation_cas_miss
                else ("degraded" if not immediate_ok else "synced")
            ),
            "deferred": generation_cas_miss,
            "deferred_reason": deferred_reason,
            "full_rebuild_required": False,
            "total_peer_count": len(peer_ids),
            "selected_peer_count": len(peer_ids),
            "compared_pairs": int(immediate.get("compared_pair_count") or 0),
        }
    )
    if not immediate_ok:
        payload["immediate_status"] = str(immediate.get("status") or "blocked")
    return payload
