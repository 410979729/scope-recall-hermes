from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .gating import compact_text, query_tokens

_ENTITY_WORD_RE = re.compile(r"`([^`\n]{2,80})`|([A-Za-z][A-Za-z0-9_.:/#-]{1,63})|([\u4e00-\u9fff]{2,12})")
_COMMON_ENTITY_WORDS = {
    "the",
    "this",
    "that",
    "what",
    "which",
    "when",
    "where",
    "who",
    "why",
    "how",
    "does",
    "should",
    "could",
    "would",
    "user",
    "assistant",
    "and",
    "after",
    "with",
    "for",
    "from",
    "into",
    "owns",
    "uses",
    "used",
    "called",
    "returned",
    "then",
    "were",
    "was",
    "services",
    "service",
    "model",
    "models",
    "changes",
    "app",
    "architecture",
    "scope",
    "recall",
    "deploy",
    "deployment",
    "production",
    "command",
    "context",
    "tool",
    "tools",
    "execution",
    "summary",
    "output",
    "preview",
    "result",
    "results",
    "status",
    "success",
    "error",
    "path",
    "file",
    "files",
    "line",
    "lines",
    "json",
    "terminal",
    "patch",
    "todo",
    "browser",
    "session",
    "memory",
    "operator",
    "fact",
    "durable",
    "entity",
    "graph",
    "lookup",
    "profile",
    "current",
    "decision",
    "decisions",
    "appear",
    "appears",
    "through",
    "visible",
    "喜欢",
    "偏好",
    "希望",
    "临时",
    "配置",
    "使用",
    "路线图",
    "包含",
    "中文",
    "实体",
    "增强",
}

_HIDDEN_LIFECYCLE_VALUES = ("archived", "superseded", "obsolete", "rejected")
_HIDDEN_LIFECYCLE_SET = set(_HIDDEN_LIFECYCLE_VALUES)


def lifecycle_value(metadata: dict[str, Any] | str | None) -> str:
    parsed = load_metadata(metadata or {})
    return str(parsed.get("lifecycle") or "").strip().lower()


def lifecycle_is_hidden(metadata: dict[str, Any] | str | None) -> bool:
    return lifecycle_value(metadata) in _HIDDEN_LIFECYCLE_SET


def lifecycle_visible_sql(alias: str = "m") -> str:
    lifecycle_expr = f"LOWER(COALESCE(CASE WHEN json_valid({alias}.metadata) THEN json_extract({alias}.metadata, '$.lifecycle') ELSE '' END, ''))"
    hidden_values = ",".join(f"'{value}'" for value in _HIDDEN_LIFECYCLE_VALUES)
    return f"{lifecycle_expr} NOT IN ({hidden_values})"


_TOOL_TRACE_ENTITY_WORDS = {
    "read_file",
    "search_files",
    "execute_code",
    "skill_view",
    "skills_list",
    "session_search",
    "browser_console",
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_press",
    "browser_vision",
    "terminal",
    "patch",
    "write_file",
    "todo",
    "memory",
    "scope_recall_search",
    "scope_recall_context",
    "scope_recall_profile",
    "scope_recall_memory",
    "scope_recall_entity",
    "scope_recall_store",
    "scope_recall_inspect",
    "scope_recall_explain",
    "scope_recall_probe",
    "scope_recall_related",
}

_TOOL_TRACE_ENTITY_PREFIXES = (
    "browser_",
    "scope_recall_",
    "mcp_",
)

_TOOL_TRACE_ENTITY_SUFFIXES = (
    "_tool",
    "_tools",
    "_result",
    "_results",
    "_output",
    "_preview",
    "_cache",
    "_path",
    "_paths",
)


_CJK_HINT_TERMS = {
    "自然码",
    "双拼",
}


def _hinted_cjk_entities(text: str) -> list[str]:
    if not re.search(r"[\u4e00-\u9fff]", text or ""):
        return []
    return [term for term in sorted(_CJK_HINT_TERMS, key=len, reverse=True) if term in text]


def _jieba_entities(text: str) -> list[str]:
    if not re.search(r"[\u4e00-\u9fff]", text or ""):
        return []
    try:
        import jieba.posseg as pseg  # type: ignore[import-not-found]
    except Exception:
        return []
    values: list[str] = []
    for word, flag in pseg.cut(text or ""):
        clean = str(word or "").strip()
        if len(clean) < 2:
            continue
        if not re.search(r"[\u4e00-\u9fff]", clean):
            continue
        if str(flag or "").startswith(("n", "v")) or len(clean) >= 3:
            values.append(clean)
    return values



def clamp_float(value: Any, *, default: float = 0.5, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _is_tool_trace_entity(lowered: str) -> bool:
    if lowered in _TOOL_TRACE_ENTITY_WORDS:
        return True
    if any(lowered.startswith(prefix) for prefix in _TOOL_TRACE_ENTITY_PREFIXES):
        return True
    if any(lowered.endswith(suffix) for suffix in _TOOL_TRACE_ENTITY_SUFFIXES):
        return True
    # Most snake_case tokens from tool logs are implementation/procedure noise,
    # not durable world entities. Stored legacy metadata can still contain such
    # rows, so read surfaces also call normalize_entity as a defensive filter.
    if "_" in lowered:
        return True
    if lowered.startswith(("/tmp/", "/home/", "file://")):
        return True
    return False


def normalize_entity(value: Any) -> str:
    text = str(value or "").strip().strip("`\"'.,:;()[]{}<>")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    lowered = text.lower()
    if lowered in _COMMON_ENTITY_WORDS or len(lowered) < 2:
        return ""
    if _is_tool_trace_entity(lowered):
        return ""
    return lowered[:96]


def _unique(values: list[str], *, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = normalize_entity(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
        if len(output) >= limit:
            break
    return output


def extract_entities(text: str, *, target: str = "") -> list[str]:
    """Extract a small, deterministic entity set for local graph recall.

    This is intentionally conservative. It indexes obvious proper nouns,
    backtick-delimited names, code-ish identifiers, and compact CJK names
    without calling an LLM or adding another runtime dependency.
    """

    candidates: list[str] = []
    for match in _ENTITY_WORD_RE.finditer(text or ""):
        value = next((group for group in match.groups() if group), "")
        if value:
            candidates.append(value)
    candidates.extend(_hinted_cjk_entities(text or ""))
    candidates.extend(_jieba_entities(text or ""))
    if str(target or "").lower() == "user":
        for token in query_tokens(text or ""):
            if token in {"joy", "eri"}:
                candidates.append(token)
    return _unique(candidates)


def metadata_entities(metadata: dict[str, Any], content: str = "", target: str = "") -> list[str]:
    raw_entities = metadata.get("entities")
    values: list[str] = []
    if isinstance(raw_entities, list):
        values.extend(str(item) for item in raw_entities)
    elif raw_entities:
        values.append(str(raw_entities))
    values.extend(extract_entities(content, target=target))
    return _unique(values)


def load_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ensure_graph_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_entities (
            memory_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'metadata',
            PRIMARY KEY(memory_id, entity)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_entity_lookup
            ON memory_entities(entity, memory_id);

        CREATE TABLE IF NOT EXISTS memory_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_feedback_memory
            ON memory_feedback(memory_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS memory_relations (
            source_memory_id TEXT NOT NULL,
            target_memory_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            note TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(source_memory_id, target_memory_id, relation_type)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_relations_source
            ON memory_relations(source_memory_id, relation_type);
        CREATE INDEX IF NOT EXISTS idx_scope_recall_relations_target
            ON memory_relations(target_memory_id, relation_type);
        """
    )


def sync_memory_entities(conn: sqlite3.Connection, *, memory_id: str, content: str, target: str, metadata: dict[str, Any] | str) -> None:
    parsed = load_metadata(metadata)
    conn.execute("DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,))
    if lifecycle_is_hidden(parsed):
        return
    entities = metadata_entities(parsed, content, target)
    conn.executemany(
        "INSERT OR REPLACE INTO memory_entities(memory_id, entity, weight, source) VALUES (?, ?, ?, ?)",
        [(memory_id, entity, 1.0, "metadata") for entity in entities],
    )


def backfill_memory_entities(conn: sqlite3.Connection) -> None:
    memory_count = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    if memory_count <= 0:
        return
    entity_count = int(conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0])
    if entity_count > 0:
        return
    rows = conn.execute(f"SELECT id, content, target, metadata FROM memories m WHERE {lifecycle_visible_sql('m')}").fetchall()
    for row in rows:
        sync_memory_entities(
            conn,
            memory_id=str(row["id"]),
            content=str(row["content"]),
            target=str(row["target"]),
            metadata=str(row["metadata"] or "{}"),
        )


def query_entities(query: str) -> list[str]:
    entities = extract_entities(query or "")
    for token in query_tokens(query or ""):
        if len(token) >= 3:
            entities.append(token)
    return _unique(entities, limit=8)


def entity_overlap_bonus(query: str, metadata: dict[str, Any], *, weight: float) -> float:
    if weight <= 0.0:
        return 0.0
    query_set = set(query_entities(query))
    memory_set = set(metadata_entities(metadata))
    if not query_set or not memory_set:
        return 0.0
    overlap = len(query_set & memory_set) / max(len(query_set), 1)
    return min(weight, weight * overlap)


def entity_distance_scores(
    query_entities: list[str],
    memory_entities: dict[str, list[str]],
    relations: dict[str, list[str]],
    *,
    max_depth: int = 2,
) -> dict[str, float]:
    """Score memories by graph distance from query entities.

    This is the local/SQLite analogue of Zep-style focal-node reranking: direct
    entity overlap scores highest, one-hop related entities score lower, and
    unrelated memories receive no graph-distance score.
    """

    frontier = {normalize_entity(entity) for entity in query_entities if normalize_entity(entity)}
    if not frontier:
        return {}
    distance: dict[str, int] = {entity: 0 for entity in frontier}
    current = set(frontier)
    normalized_relations = {
        normalize_entity(key): [normalize_entity(value) for value in values if normalize_entity(value)]
        for key, values in (relations or {}).items()
    }
    for depth in range(1, max(1, int(max_depth)) + 1):
        next_frontier: set[str] = set()
        for entity in current:
            for neighbor in normalized_relations.get(entity, []):
                if neighbor and neighbor not in distance:
                    distance[neighbor] = depth
                    next_frontier.add(neighbor)
        current = next_frontier
        if not current:
            break

    scores: dict[str, float] = {}
    for memory_id, entities in (memory_entities or {}).items():
        best: float = 0.0
        for entity in entities:
            normalized = normalize_entity(entity)
            if normalized not in distance:
                continue
            # depth 0 => 1.0, depth 1 => 0.5, depth 2 => 0.333...
            best = max(best, 1.0 / (distance[normalized] + 1.0))
        if best > 0.0:
            scores[str(memory_id)] = best
    return scores


def apply_quality_weight(score: float, metadata: dict[str, Any], *, weight: float) -> float:
    if weight <= 0.0:
        return score
    keys = [key for key in ("confidence", "trust", "importance") if key in metadata]
    if not keys:
        return score
    quality = sum(clamp_float(metadata.get(key), default=0.5) for key in keys) / len(keys)
    return max(0.0, min(1.0, score + (quality - 0.5) * weight))


def compact_context_lines(items: list[dict[str, Any]], *, max_chars: int) -> str:
    lines: list[str] = []
    used = 0
    for item in items:
        target = str(item.get("target") or "memory")
        summary = compact_text(str(item.get("summary") or item.get("content") or ""), 180)
        line = f"- [{target}] {summary}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
