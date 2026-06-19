from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .artifacts import artifact_anchor_block, extract_artifacts
from .capture_filters import should_capture_text
from .config import load_runtime_config
from .gating import clean_text, compact_text, dedup_key
from .governance import is_conflicting, merge_memory_text, normalize_memory_type, semantic_similarity
from .graph import clamp_float, load_metadata, normalize_entity, sync_memory_entities
from .http_utils import chat_completions_endpoint as _shared_chat_completions_endpoint, redact_sensitive as _shared_redact_sensitive
from .models import RuntimeScope
from .scope import accessible_scope_ids, build_scope_id, build_shared_scope_id, canonical_user_id, normalize_scope_identity, writable_scope_ids
from .sql_store import delete_rows, ensure_schema, exact_duplicate_groups, store_row, update_row
from .vector_runtime import setup_vector_layer, upsert_vector_record

ROLE_INCLUDE = {"user", "assistant", "tool"}
TARGETS = {"user", "memory", "project", "ops"}
TASK_HINT_RE = re.compile(
    r"(bug|fix|debug|deploy|release|verify|test|pytest|gateway|sqlite|scope-recall|plugin|"
    r"架构|计划|实现|修复|验证|测试|插件|记忆|工具|任务|问题|报错|配置|部署|重启)",
    re.IGNORECASE,
)
SUCCESS_HINT_RE = re.compile(r"(passed|ok|success|fixed|done|完成|通过|成功|已验证|验证通过)", re.IGNORECASE)
FAIL_HINT_RE = re.compile(r"(blocked|failed|error|失败|阻塞|未完成|报错)", re.IGNORECASE)
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|private[_-]?key)\s*[:=]\s*[^\s,'\"\]}]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
]


@dataclass
class MessageRecord:
    id: int
    session_id: str
    role: str
    content: str
    timestamp: float
    tool_name: str = ""
    tool_calls: str = ""


@dataclass
class SessionBundle:
    id: str
    source: str = ""
    user_id: str = ""
    title: str = ""
    started_at: float = 0.0
    messages: list[MessageRecord] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    command_hints: list[str] = field(default_factory=list)
    is_task: bool = False
    completed: bool = False

    @property
    def message_ids(self) -> list[int]:
        return [message.id for message in self.messages if message.role in {"user", "assistant"}]


@dataclass
class DigestCandidate:
    content: str
    target: str = "memory"
    memory_type: str = "summary"
    importance: float = 0.55
    confidence: float = 0.65
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    session_id: str = ""
    message_ids: list[int] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)


@dataclass
class ScopeProfile:
    scope: RuntimeScope
    scope_id: str
    shared_scope_id: str
    accessible_scope_ids: list[str]
    writable_scope_ids: list[str] = field(default_factory=list)


@dataclass
class DigestOptions:
    hermes_home: Path
    digest_date: date
    timezone_name: str = "Asia/Shanghai"
    dry_run: bool = False
    extractor: str = "llm"
    state_db: Path | None = None
    session_id: str = ""
    limit_sessions: int = 0
    max_session_chars: int = 16000
    chunk_chars: int = 7000
    model: str = ""
    base_url: str = ""
    endpoint: str = ""
    append_v1: bool | None = None
    api_key: str = ""
    timeout: float = 60.0
    max_attempts: int = 2
    retry_delay: float = 1.0
    allow_heuristic_fallback: bool = True
    verbose: bool = False


def _profile_writable_scope_ids(scope: ScopeProfile) -> list[str]:
    configured = [str(scope_id) for scope_id in getattr(scope, "writable_scope_ids", []) if str(scope_id)]
    fallback = [str(scope.scope_id), str(scope.shared_scope_id)]
    output: list[str] = []
    for scope_id in [*configured, *fallback]:
        if scope_id and scope_id not in output:
            output.append(scope_id)
    return output


def _memory_scope_id(conn: sqlite3.Connection, memory_id: str) -> str:
    row = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return str(row["scope_id"] if row is not None else "")


class DigestVectorRuntime:
    """Small provider-shaped adapter for vector sync outside Hermes runtime."""

    name = "scope-recall"

    def __init__(self, *, hermes_home: Path, conn: sqlite3.Connection, scope: ScopeProfile) -> None:
        self._hermes_home = hermes_home
        self._storage_dir = hermes_home / "scope-recall"
        self._db_path = self._storage_dir / "memory.sqlite3"
        self._plugin_dir = Path(__file__).resolve().parent
        self._config = load_runtime_config(self._plugin_dir, self._storage_dir)
        self._retrieval_config = dict(self._config.get("retrieval") or {})
        self._vector_config = dict(self._config.get("vector") or {})
        self._conn = conn
        self._lock = threading.RLock()
        self._scope = scope.scope
        self._scope_id = scope.scope_id
        self._shared_scope_id = scope.shared_scope_id
        self._accessible_scope_ids = list(scope.accessible_scope_ids)
        self._writable_scope_ids = _profile_writable_scope_ids(scope)
        self._embedder = None
        self._vector_store = None
        self._vector_enabled = False
        self._vector_ready = False
        self._vector_status = "disabled"
        self._vector_message = ""
        self._vector_row_count = 0
        self._vector_unique_id_count = 0
        self._vector_duplicate_row_count = 0
        self._vector_backend = "lancedb"
        setup_vector_layer(self)

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn

    def _vector_text(self, summary: str, content: str) -> str:
        return clean_text(f"{summary}\n{content}")

    def close(self) -> None:
        if self._vector_store is not None:
            self._vector_store.close()


def redact_sensitive(text: str) -> str:
    return _shared_redact_sensitive(text)


def _redact_match(match: re.Match[str]) -> str:
    try:
        label = match.group(1)
    except IndexError:
        label = ""
    if label:
        return f"{label}=[REDACTED]"
    return "[REDACTED]"


def parse_date(value: str | None, *, timezone_name: str) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.now(ZoneInfo(timezone_name)).date()


def local_day_bounds(day: date, timezone_name: str) -> tuple[float, float]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, datetime_time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def resolve_session_db(hermes_home: Path, override: Path | None = None) -> Path | None:
    if override is not None:
        return override.expanduser().resolve()
    for path in (hermes_home / "state.db", hermes_home / "lcm.db"):
        if path.exists():
            return path
    return None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _read_session_meta(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if "sessions" not in _table_names(conn):
        return {}
    columns = _column_names(conn, "sessions")
    wanted = ["id", "source", "user_id", "title", "started_at"]
    select_parts = [column if column in columns else f"'' AS {column}" for column in wanted]
    rows = conn.execute(f"SELECT {', '.join(select_parts)} FROM sessions").fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}


def load_session_bundles(
    db_path: Path,
    *,
    digest_date: date,
    timezone_name: str,
    session_id: str = "",
    limit_sessions: int = 0,
) -> list[SessionBundle]:
    if not db_path.exists():
        return []
    start_ts, end_ts = local_day_bounds(digest_date, timezone_name)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if "messages" not in _table_names(conn):
            return []
        columns = _column_names(conn, "messages")
        id_col = "id" if "id" in columns else "store_id"
        source_expr = "source" if "source" in columns else "'' AS source"
        tool_name_expr = "tool_name" if "tool_name" in columns else "'' AS tool_name"
        tool_calls_expr = "tool_calls" if "tool_calls" in columns else "'' AS tool_calls"
        where = "timestamp >= ? AND timestamp < ?"
        params: list[Any] = [start_ts, end_ts]
        if session_id:
            where += " AND session_id = ?"
            params.append(session_id)
        rows = conn.execute(
            f"""
            SELECT {id_col} AS id, session_id, {source_expr}, role, content,
                   {tool_name_expr}, {tool_calls_expr}, timestamp
            FROM messages
            WHERE {where}
            ORDER BY timestamp ASC, {id_col} ASC
            """,
            params,
        ).fetchall()
        session_meta = _read_session_meta(conn)
    finally:
        conn.close()

    grouped: dict[str, SessionBundle] = {}
    for row in rows:
        role = str(row["role"] or "").strip().lower()
        if role not in ROLE_INCLUDE:
            continue
        sid = str(row["session_id"])
        meta = session_meta.get(sid, {})
        bundle = grouped.get(sid)
        if bundle is None:
            bundle = SessionBundle(
                id=sid,
                source=str(meta.get("source") or row["source"] or ""),
                user_id=str(meta.get("user_id") or ""),
                title=str(meta.get("title") or ""),
                started_at=float(meta.get("started_at") or row["timestamp"] or 0.0),
            )
            grouped[sid] = bundle
        message = MessageRecord(
            id=int(row["id"]),
            session_id=sid,
            role=role,
            content=redact_sensitive(clean_text(str(row["content"] or ""))),
            timestamp=float(row["timestamp"] or 0.0),
            tool_name=str(row["tool_name"] or ""),
            tool_calls=str(row["tool_calls"] or ""),
        )
        if message.role == "tool":
            tool_name = message.tool_name.strip()
            if tool_name:
                bundle.tool_names.append(tool_name)
        elif message.tool_calls:
            names, commands = parse_tool_calls(message.tool_calls)
            bundle.tool_names.extend(names)
            bundle.command_hints.extend(commands)
        bundle.messages.append(message)

    bundles = list(grouped.values())
    for bundle in bundles:
        bundle.tool_names = unique_strings(bundle.tool_names, limit=24)
        bundle.command_hints = unique_strings(bundle.command_hints, limit=12)
        session_text = "\n".join([bundle.title, *[m.content for m in bundle.messages if m.role in {"user", "assistant"}]])
        bundle.is_task = bool(bundle.tool_names) or bool(TASK_HINT_RE.search(session_text))
        tail = "\n".join(m.content for m in bundle.messages[-8:] if m.role == "assistant")
        bundle.completed = bool(SUCCESS_HINT_RE.search(tail)) and not bool(FAIL_HINT_RE.search(tail[-1200:]))
    bundles.sort(key=lambda item: item.started_at)
    if limit_sessions > 0:
        bundles = bundles[:limit_sessions]
    return bundles


def parse_tool_calls(raw: str) -> tuple[list[str], list[str]]:
    names: list[str] = []
    commands: list[str] = []
    try:
        parsed = json.loads(raw)
    except Exception:
        return names, commands
    calls = parsed if isinstance(parsed, list) else [parsed]
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or call.get("tool_name") or "").strip()
        if name:
            names.append(name)
        args_raw = function.get("arguments") or call.get("arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        command = str(args.get("command") or args.get("cmd") or "").strip() if isinstance(args, dict) else ""
        if command:
            commands.append(summarize_command(command))
    return names, commands


def summarize_command(command: str) -> str:
    command = redact_sensitive(command)
    command = re.sub(r"\s+", " ", command).strip()
    if not command:
        return ""
    safe_patterns = [
        r"\bpytest\b[^;&|]*",
        r"\bpython(?:3)?\b[^;&|]*(?:pytest|compileall|check\.release|nightly-digest)[^;&|]*",
        r"\bgit\s+(?:status|diff|show|log)\b[^;&|]*",
        r"\bhermes\s+(?:memory|gateway)\b[^;&|]*",
        r"\bsqlite3\b\s+\S+",
        r"\brg\b[^;&|]*",
    ]
    for pattern in safe_patterns:
        match = re.search(pattern, command)
        if match:
            return compact_text(match.group(0), 160)
    return compact_text(command.split()[0], 80)


def unique_strings(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= limit:
            break
    return output


def session_chunks(bundle: SessionBundle, *, chunk_chars: int, max_session_chars: int) -> list[str]:
    lines = [f"Session: {bundle.id}", f"Title: {bundle.title or '(untitled)'}", f"Type: {'task' if bundle.is_task else 'normal'}"]
    if bundle.tool_names:
        lines.append("Tools used: " + ", ".join(bundle.tool_names[:16]))
    if bundle.command_hints:
        lines.append("Command hints: " + "; ".join(bundle.command_hints[:8]))
    header = "\n".join(lines) + "\n"
    body_lines: list[str] = []
    for message in bundle.messages:
        if message.role not in {"user", "assistant"}:
            continue
        if not message.content or not should_capture_text(message.content).allowed:
            continue
        body_lines.append(f"{message.role}: {compact_text(message.content, 1800)}")
    text = header + "\n".join(body_lines)
    if len(text) > max_session_chars:
        text = text[:max_session_chars]
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    current = header
    for line in body_lines:
        if len(current) + len(line) + 1 > chunk_chars and current.strip() != header.strip():
            chunks.append(current.rstrip())
            current = header
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def bundle_artifact_anchor_block(bundle: SessionBundle) -> str:
    text = "\n".join(message.content for message in bundle.messages if message.role in {"user", "assistant"})
    return artifact_anchor_block(extract_artifacts(text))


def heuristic_candidates(bundle: SessionBundle) -> list[DigestCandidate]:
    candidates: list[DigestCandidate] = []
    artifact_block = bundle_artifact_anchor_block(bundle)
    user_texts = [message.content for message in bundle.messages if message.role == "user" and message.content]
    assistant_tail = [message.content for message in bundle.messages if message.role == "assistant" and message.content][-3:]
    if bundle.is_task and bundle.tool_names:
        title = bundle.title or compact_text(user_texts[0] if user_texts else bundle.id, 80)
        result = compact_text(" ".join(assistant_tail), 260)
        tools = ", ".join(bundle.tool_names[:10])
        commands = "; ".join(bundle.command_hints[:6])
        parts = [f"{title} 的可复用任务流程：使用工具链 {tools}。"]
        if commands:
            parts.append(f"关键命令/检查包括 {commands}。")
        if result:
            parts.append(f"结果摘要：{result}")
        if artifact_block:
            parts.append(artifact_block)
        candidates.append(
            DigestCandidate(
                content=compact_text(" ".join(parts), 900),
                target="ops",
                memory_type="workflow",
                importance=0.82 if bundle.completed else 0.68,
                confidence=0.72,
                entities=[bundle.title, *bundle.tool_names],
                tags=["nightly-digest", "workflow", "tool-chain"],
                reason="task session used tools and produced reusable workflow evidence",
                session_id=bundle.id,
                message_ids=bundle.message_ids[:80],
                tools_used=bundle.tool_names[:16],
                commands=bundle.command_hints[:10],
                verification=extract_verification_hints(assistant_tail),
            )
        )
    elif user_texts:
        summary = compact_text(" / ".join(user_texts[-3:]), 700)
        if len(summary) >= 60 and TASK_HINT_RE.search(summary):
            content = f"对话重点摘要：{summary}"
            if artifact_block:
                content = f"{content}\n\n{artifact_block}"
            candidates.append(
                DigestCandidate(
                    content=content,
                    target="memory",
                    memory_type="summary",
                    importance=0.5,
                    confidence=0.6,
                    entities=[],
                    tags=["nightly-digest", "summary"],
                    reason="normal conversation contained reusable topic context",
                    session_id=bundle.id,
                    message_ids=bundle.message_ids[:80],
                )
            )
    return candidates


def extract_verification_hints(texts: list[str]) -> list[str]:
    hints: list[str] = []
    for text in texts:
        for pattern in (r"\d+\s+passed(?:,\s*\d+\s+warning)?", r"release gate ok", r"验证通过", r"未发现 bug"):
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                hints.append(match.group(0))
    return unique_strings(hints, limit=8)


def build_prompt(bundle: SessionBundle, chunk: str, existing_context: list[str]) -> str:
    existing = "\n".join(f"- {item}" for item in existing_context[:40]) or "- (none)"
    mode = "任务型对话" if bundle.is_task else "普通对话"
    return (
        "你是 scope-recall 的夜间记忆整理器。阅读当天对话片段，提取值得长期保存的记忆。\n"
        "硬规则：不要保存 system/tool 原文、不要保存 token/API key/password/cookie/private key、不要保存流水账。\n"
        "关键外部工件必须保留可检索锚点：repo/name、issue/PR/release/commit 编号、标题、URL、记录时状态/日期/作者/下一步（能从片段得出才写；当前状态需 live check）。\n"
        "任务型对话要额外提取可复用 workflow/tool-chain：用过哪些工具类别、关键检查、验证方式、踩坑。只写脱敏摘要。\n"
        "如果已有记忆已经完整覆盖，请输出 action=skip；如果已有记忆不够详细，请输出 action=update 并给 existing_hint。\n"
        "输出只能是 JSON 数组，每项字段：action, content, target, memory_type, importance, confidence, entities, tags, reason, existing_hint。\n"
        "target 只能是 user/memory/project/ops；memory_type 可为 preference/factual/project/procedure/workflow/summary/pitfall/decision/resource/constraint。\n"
        f"\n会话类型：{mode}\n"
        f"已有相关记忆摘要：\n{existing}\n\n"
        f"对话片段：\n---\n{chunk}\n---\n"
    )


def parse_llm_candidates(raw: str, *, bundle: SessionBundle) -> list[DigestCandidate]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    candidates: list[DigestCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("action") or "").strip().lower() == "skip":
            continue
        content = redact_sensitive(clean_text(str(item.get("content") or "")))
        target = str(item.get("target") or "memory").strip().lower()
        if target not in TARGETS:
            target = "memory"
        entities_raw = item.get("entities") if isinstance(item.get("entities"), list) else []
        tags_raw = item.get("tags") if isinstance(item.get("tags"), list) else []
        candidate = DigestCandidate(
            content=content,
            target=target,
            memory_type=normalize_memory_type(item.get("memory_type"), "summary"),
            importance=clamp_float(item.get("importance"), default=0.55),
            confidence=clamp_float(item.get("confidence"), default=0.65),
            entities=[entity for entity in (normalize_entity(value) for value in entities_raw) if entity],
            tags=unique_strings([str(value).strip().lower() for value in tags_raw], limit=12),
            reason=compact_text(str(item.get("reason") or ""), 240),
            session_id=bundle.id,
            message_ids=bundle.message_ids[:80],
            tools_used=bundle.tool_names[:16],
            commands=bundle.command_hints[:10],
            verification=extract_verification_hints([content]),
        )
        if candidate_is_allowed(candidate):
            candidates.append(candidate)
    return candidates


def candidate_is_allowed(candidate: DigestCandidate) -> bool:
    if candidate.target not in TARGETS:
        return False
    if len(candidate.content) < 40:
        return False
    if not should_capture_text(candidate.content).allowed:
        return False
    return True


def _config_bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_digest_api_mode(value: Any, *, provider: str = "", base_url: str = "") -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": "",
        "openai": "chat_completions",
        "openai_compatible": "chat_completions",
        "chat": "chat_completions",
        "chat_completion": "chat_completions",
        "chat_completions": "chat_completions",
        "codex": "codex_responses",
        "codex_responses": "codex_responses",
        "responses": "codex_responses",
        "openai_responses": "codex_responses",
    }
    normalized = aliases.get(raw, raw)
    if normalized:
        return normalized
    provider_l = str(provider or "").strip().lower()
    base_l = str(base_url or "").strip().lower()
    if provider_l == "openai-codex" or ("chatgpt.com" in base_l and "/backend-api/codex" in base_l):
        return "codex_responses"
    return "chat_completions"


def resolve_llm_config(hermes_home: Path, options: DigestOptions) -> dict[str, Any]:
    config_path = hermes_home / "config.yaml"
    env = load_dotenv(hermes_home / ".env")
    env.update(os.environ)
    cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            cfg = loaded if isinstance(loaded, dict) else {}
        except Exception:
            cfg = {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    providers_cfg = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    nightly_cfg = cfg.get("scope_recall_nightly_digest") if isinstance(cfg.get("scope_recall_nightly_digest"), dict) else {}

    provider = str(nightly_cfg.get("provider") or model_cfg.get("provider") or "").strip()
    provider_cfg = providers_cfg.get(provider) if isinstance(providers_cfg.get(provider), dict) else {}
    model = options.model or str(
        nightly_cfg.get("model")
        or nightly_cfg.get("default_model")
        or provider_cfg.get("default_model")
        or model_cfg.get("model")
        or model_cfg.get("default")
        or model_cfg.get("default_model")
        or "gpt-4o-mini"
    )
    base_url = options.base_url or str(
        nightly_cfg.get("base_url")
        or provider_cfg.get("base_url")
        or model_cfg.get("base_url")
        or "https://api.openai.com"
    )
    endpoint = options.endpoint or str(
        nightly_cfg.get("endpoint")
        or nightly_cfg.get("chat_endpoint")
        or provider_cfg.get("endpoint")
        or provider_cfg.get("chat_endpoint")
        or model_cfg.get("endpoint")
        or ""
    )
    append_v1_raw = options.append_v1
    if append_v1_raw is None:
        append_v1_raw = nightly_cfg.get("append_v1", provider_cfg.get("append_v1", model_cfg.get("append_v1", True)))
    append_v1 = _config_bool_value(append_v1_raw, True)
    api_key = options.api_key or resolve_api_key(
        nightly_cfg.get("api_key")
        or nightly_cfg.get("api_key_env")
        or nightly_cfg.get("key_env")
        or provider_cfg.get("api_key")
        or provider_cfg.get("api_key_env")
        or provider_cfg.get("key_env")
        or model_cfg.get("api_key"),
        provider,
        env,
    )
    api_mode = _normalize_digest_api_mode(
        nightly_cfg.get("api_mode") or provider_cfg.get("api_mode") or model_cfg.get("api_mode"),
        provider=provider,
        base_url=str(base_url or ""),
    )
    return {
        "provider": provider,
        "model": str(model or "gpt-4o-mini"),
        "base_url": str(base_url or "https://api.openai.com").rstrip("/"),
        "endpoint": str(endpoint or "").rstrip("/"),
        "append_v1": append_v1,
        "api_key": api_key,
        "api_mode": api_mode,
    }


def load_dotenv(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    if not path.exists():
        return output
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip().strip("'\"")
        output[key.strip()] = value
    return output


def resolve_api_key(raw_value: Any, provider: str, env: dict[str, str]) -> str:
    candidates = ["SCOPE_RECALL_DIGEST_API_KEY"]
    raw = str(raw_value or "").strip()
    env_match = re.fullmatch(r"\$\{([^}]+)\}", raw)
    if env_match:
        candidates.append(env_match.group(1))
    elif raw and re.fullmatch(r"[A-Z][A-Z0-9_]*", raw):
        candidates.append(raw)
    elif raw:
        return raw
    if provider:
        candidates.append(f"{provider.upper().replace('-', '_')}_API_KEY")
    candidates.extend(["DEEPSEEK_API_KEY", "OPENAI_API_KEY"])
    for key in candidates:
        value = env.get(key)
        if value:
            return value
    return ""


def _codex_cloudflare_headers(access_token: str) -> dict[str, str]:
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Scope Recall)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


def _responses_endpoint(base_url: str) -> str:
    endpoint = str(base_url or "").strip().rstrip("/")
    if not endpoint:
        endpoint = "https://api.openai.com/v1"
    if endpoint.endswith("/responses"):
        return endpoint
    return endpoint + "/responses"


def _response_item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    value = getattr(item, key, default)
    return value if value is not None else default


def _extract_responses_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    parts: list[str] = []
    for item in data.get("output") or []:
        if _response_item_get(item, "type") != "message":
            continue
        for content_part in _response_item_get(item, "content", []) or []:
            part_type = _response_item_get(content_part, "type")
            if part_type in {"output_text", "text"}:
                text = _response_item_get(content_part, "text", "")
                if text:
                    parts.append(str(text))
    if parts:
        return "".join(parts)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content") or "")


def _extract_responses_sse_text(body: str) -> str:
    delta_parts: list[str] = []
    item_parts: list[str] = []
    completed_payload: dict[str, Any] | None = None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "")
        if event_type == "error":
            message = event.get("message") or event.get("error") or raw
            raise RuntimeError(f"LLM stream error: {redact_sensitive(str(message))}")
        if "output_text.delta" in event_type:
            delta = event.get("delta")
            if isinstance(delta, str):
                delta_parts.append(delta)
            continue
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                text = _extract_responses_text({"output": [item]})
                if text:
                    item_parts.append(text)
            continue
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if isinstance(response, dict):
                completed_payload = response
            if event_type == "response.failed":
                failure_payload = event.get("response") or raw
                raise RuntimeError(f"LLM stream failed: {redact_sensitive(str(failure_payload))}")
    if delta_parts:
        return "".join(delta_parts)
    if item_parts:
        return "".join(item_parts)
    if completed_payload:
        return _extract_responses_text(completed_payload)
    return ""


def _decode_responses_body(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _extract_responses_sse_text(body)
    if not isinstance(data, dict):
        return ""
    return _extract_responses_text(data)


def _chat_completions_endpoint(base_url: str, *, endpoint: str = "", append_v1: bool = True) -> str:
    return _shared_chat_completions_endpoint(base_url, endpoint=endpoint, append_v1=append_v1)


def _call_chat_completions_llm(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    endpoint: str = "",
    append_v1: bool = True,
) -> str:
    endpoint_url = _chat_completions_endpoint(base_url, endpoint=endpoint, append_v1=append_v1)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You extract durable memory as strict JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 1800,
    }
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"LLM HTTP {exc.code} at {endpoint_url}: {body}") from exc
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content") or "")


def _call_codex_responses_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float) -> str:
    payload = {
        "model": model,
        "instructions": "You extract durable memory as strict JSON.",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "store": False,
        "stream": True,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        **_codex_cloudflare_headers(api_key),
    }
    endpoint_url = _responses_endpoint(base_url)
    request = urllib.request.Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = redact_sensitive(exc.read().decode("utf-8", errors="replace")[:500])
        raise RuntimeError(f"LLM HTTP {exc.code} at {endpoint_url}: {body}") from exc
    return _decode_responses_body(body)


def call_llm(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    api_mode: str = "chat_completions",
    endpoint: str = "",
    append_v1: bool = True,
) -> str:
    if not api_key:
        raise RuntimeError("API key not found for nightly digest")
    mode = _normalize_digest_api_mode(api_mode, provider="", base_url=base_url)
    if mode == "codex_responses":
        return _call_codex_responses_llm(prompt, model=model, base_url=base_url, api_key=api_key, timeout=timeout)
    if mode != "chat_completions":
        raise RuntimeError(f"Unsupported digest api_mode: {api_mode}")
    return _call_chat_completions_llm(
        prompt,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        endpoint=endpoint,
        append_v1=append_v1,
    )


def _classify_llm_error(exc: Exception) -> tuple[str, bool]:
    message = str(exc or "").lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return "timeout", True
    if "429" in message or "rate limit" in message or "too many requests" in message:
        return "rate_limit", True
    if any(token in message for token in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable", "gateway timeout")):
        return "server", True
    if any(token in message for token in ("connection", "network", "temporarily", "reset by peer", "remote end closed")):
        return "network", True
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "permission")):
        return "auth", False
    if any(token in message for token in ("402", "quota", "billing", "insufficient_quota")):
        return "quota", False
    if any(token in message for token in ("json", "parse", "decode")):
        return "parse", False
    return "unknown", True


def _call_llm_with_retries(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    api_mode: str,
    endpoint: str = "",
    append_v1: bool = True,
    max_attempts: int = 1,
    retry_delay: float = 0.0,
) -> str:
    last_error: Exception | None = None
    last_kind = "unknown"
    last_retryable = True
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            return call_llm(
                prompt,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                api_mode=api_mode,
                endpoint=endpoint,
                append_v1=append_v1,
            )
        except Exception as exc:
            last_error = exc
            last_kind, last_retryable = _classify_llm_error(exc)
            if (not last_retryable) or attempt >= attempts:
                break
            if retry_delay > 0:
                time.sleep(max(0.0, float(retry_delay)))
    assert last_error is not None
    raise RuntimeError(
        f"{last_kind} after {attempts} attempt(s): {type(last_error).__name__}: {redact_sensitive(str(last_error)[:400])}"
    ) from last_error


def _fallback_event(*, bundle: SessionBundle, exc: Exception, attempts: int) -> dict[str, Any]:
    kind, retryable = _classify_llm_error(exc)
    return {
        "session_id": bundle.id,
        "kind": kind,
        "retryable": retryable,
        "attempts": max(1, int(attempts or 1)),
        "message": redact_sensitive(f"{type(exc).__name__}: {str(exc)[:240]}"),
    }


def infer_scope(
    conn: sqlite3.Connection,
    *,
    fallback_platform: str = "cli",
    fallback_user_id: str = "",
    runtime_config: dict[str, Any] | None = None,
) -> ScopeProfile:
    row = conn.execute(
        """
        SELECT platform, user_id, chat_id, thread_id, gateway_session_key,
               agent_identity, agent_workspace
        FROM memories
        WHERE target IN ('user','memory','project','ops')
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT platform, user_id, chat_id, thread_id, gateway_session_key,
                   agent_identity, agent_workspace
            FROM memories
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    scope = RuntimeScope(
        platform=str(row["platform"] if row else fallback_platform) or fallback_platform or "cli",
        user_id=str(row["user_id"] if row else fallback_user_id) or fallback_user_id or "local",
        chat_id=str(row["chat_id"] if row else ""),
        thread_id=str(row["thread_id"] if row else ""),
        gateway_session_key="",
        agent_identity=str(row["agent_identity"] if row else "default") or "default",
        agent_workspace=str(row["agent_workspace"] if row else "hermes") or "hermes",
        agent_context="primary",
    )
    scope = normalize_scope_identity(scope, runtime_config)
    return ScopeProfile(
        scope=scope,
        scope_id=build_scope_id(scope, runtime_config),
        shared_scope_id=build_shared_scope_id(scope, runtime_config),
        accessible_scope_ids=accessible_scope_ids(scope, runtime_config),
        writable_scope_ids=writable_scope_ids(scope, runtime_config),
    )


def ensure_digest_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nightly_digest_runs (
            id TEXT PRIMARY KEY,
            digest_date TEXT NOT NULL,
            source_db TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            extractor TEXT NOT NULL,
            model TEXT,
            dry_run INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_digest_date
            ON nightly_digest_runs(digest_date, started_at DESC);
        CREATE TABLE IF NOT EXISTS memory_digest_sources (
            memory_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            message_ids TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, run_id, session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_scope_recall_digest_memory
            ON memory_digest_sources(memory_id, created_at DESC);
        """
    )
    conn.commit()


def existing_memory_context(conn: sqlite3.Connection, scope: ScopeProfile, *, limit: int = 80) -> list[str]:
    placeholders = ",".join("?" for _ in scope.accessible_scope_ids)
    rows = conn.execute(
        f"""
        SELECT target, summary, content
        FROM memories
        WHERE scope_id IN ({placeholders}) AND target IN ('user','memory','project','ops')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        [*scope.accessible_scope_ids, limit],
    ).fetchall()
    return [f"[{row['target']}] {compact_text(str(row['summary'] or row['content']), 180)}" for row in rows]


def candidate_metadata(candidate: DigestCandidate, run_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "memory_type": candidate.memory_type,
        "importance": candidate.importance,
        "confidence": candidate.confidence,
        "entities": candidate.entities,
        "tags": unique_strings([*candidate.tags, "nightly-digest"], limit=20),
        "digest_run_id": run_id,
        "digest_session_id": candidate.session_id,
        "digest_reason": candidate.reason,
    }
    if candidate.tools_used:
        metadata["tools_used"] = candidate.tools_used
    if candidate.commands:
        metadata["commands"] = candidate.commands
    if candidate.verification:
        metadata["verification"] = candidate.verification
    return metadata


def find_match(conn: sqlite3.Connection, scope: ScopeProfile, candidate: DigestCandidate) -> tuple[str, str, float]:
    placeholders = ",".join("?" for _ in scope.accessible_scope_ids)
    rows = conn.execute(
        f"""
        SELECT id, content
        FROM memories
        WHERE scope_id IN ({placeholders}) AND target = ?
        ORDER BY updated_at DESC
        LIMIT 250
        """,
        [*scope.accessible_scope_ids, candidate.target],
    ).fetchall()
    best_id = ""
    best_content = ""
    best_score = 0.0
    candidate_key = dedup_key(candidate.content)
    for row in rows:
        content = str(row["content"])
        if dedup_key(content) == candidate_key:
            return str(row["id"]), content, 1.0
        score = semantic_similarity(content, candidate.content)
        if score > best_score:
            best_id = str(row["id"])
            best_content = content
            best_score = score
    return best_id, best_content, best_score


def record_digest_source(conn: sqlite3.Connection, *, memory_id: str, run_id: str, candidate: DigestCandidate) -> None:
    source_hash = hashlib.sha1(candidate.content.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_digest_sources(memory_id, run_id, session_id, message_ids, source_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            run_id,
            candidate.session_id,
            json.dumps(candidate.message_ids[:120], ensure_ascii=False),
            source_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def merge_candidate_metadata(conn: sqlite3.Connection, *, memory_id: str, candidate: DigestCandidate, run_id: str) -> None:
    row = conn.execute("SELECT content, target, metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        return
    existing = load_metadata(row["metadata"])
    incoming = candidate_metadata(candidate, run_id)
    for key in ("entities", "tags", "tools_used", "commands", "verification"):
        existing_values = existing.get(key) if isinstance(existing.get(key), list) else []
        incoming_values = incoming.get(key) if isinstance(incoming.get(key), list) else []
        merged = unique_strings([*map(str, existing_values), *map(str, incoming_values)], limit=24)
        if merged:
            existing[key] = merged
    for key in ("digest_run_id", "digest_session_id", "digest_reason", "memory_type"):
        if incoming.get(key):
            existing[key] = incoming[key]
    existing["importance"] = max(clamp_float(existing.get("importance"), default=0.5), candidate.importance)
    existing["confidence"] = max(clamp_float(existing.get("confidence"), default=0.5), candidate.confidence)
    metadata_json = json.dumps(existing, ensure_ascii=False, sort_keys=True)
    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (metadata_json, memory_id))
    sync_memory_entities(conn, memory_id=memory_id, content=str(row["content"]), target=str(row["target"]), metadata=existing)


def _cross_platform_metadata(scope: RuntimeScope, config: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = canonical_user_id(scope, config)
    metadata = {"raw_platform": scope.platform or "cli", "raw_user_id": scope.user_id or "local"}
    if canonical:
        metadata["canonical_user"] = canonical
        metadata["scope_identity_mode"] = "canonical"
    return metadata


def apply_candidates(
    conn: sqlite3.Connection,
    vector_runtime: DigestVectorRuntime | None,
    scope: ScopeProfile,
    *,
    run_id: str,
    candidates: list[DigestCandidate],
    dry_run: bool,
    runtime_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seen_candidate_keys: set[str] = set()
    actions: list[dict[str, Any]] = []
    counts = Counter()
    for candidate in candidates:
        if not candidate_is_allowed(candidate):
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "candidate filtered", "content": candidate.content[:160]})
            continue
        key = f"{candidate.target}:{dedup_key(candidate.content)}"
        if key in seen_candidate_keys:
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "duplicate candidate", "content": candidate.content[:160]})
            continue
        seen_candidate_keys.add(key)

        match_id, match_content, score = find_match(conn, scope, candidate)
        match_scope_id = _memory_scope_id(conn, match_id) if match_id else ""
        match_is_writable = bool(match_scope_id and match_scope_id in set(_profile_writable_scope_ids(scope)))
        if match_id and score >= 0.88:
            counts["skipped"] += 1
            actions.append({"action": "skip", "reason": "existing memory covers candidate", "id": match_id, "score": round(score, 4)})
            continue
        if match_id and match_is_writable and score >= 0.55 and not is_conflicting(match_content, candidate.content):
            merged = merge_memory_text(match_content, candidate.content)
            if merged == match_content:
                counts["skipped"] += 1
                actions.append({"action": "skip", "reason": "merge produced no change", "id": match_id, "score": round(score, 4)})
                continue
            counts["updated"] += 1
            actions.append({"action": "update", "id": match_id, "score": round(score, 4), "target": candidate.target})
            if not dry_run:
                updated, summary, updated_at = update_row(
                    conn,
                    memory_id=match_id,
                    content=merged,
                    target=candidate.target,
                    scope_ids=_profile_writable_scope_ids(scope),
                )
                if updated:
                    merge_candidate_metadata(conn, memory_id=match_id, candidate=candidate, run_id=run_id)
                    record_digest_source(conn, memory_id=match_id, run_id=run_id, candidate=candidate)
                    conn.commit()
                    if vector_runtime is not None:
                        row_scope = conn.execute("SELECT scope_id FROM memories WHERE id = ?", (match_id,)).fetchone()
                        upsert_vector_record(
                            vector_runtime,
                            id=match_id,
                            source="nightly-digest",
                            target=candidate.target,
                            content=merged,
                            summary=summary,
                            updated_at=updated_at,
                            scope_id=str(row_scope["scope_id"] if row_scope is not None else scope.shared_scope_id),
                        )
            continue

        counts["inserted"] += 1
        memory_id = uuid.uuid4().hex
        actions.append({"action": "insert", "id": memory_id, "target": candidate.target})
        if not dry_run:
            stored_id, summary, updated_at, inserted = store_row(
                conn,
                memory_id=memory_id,
                scope_id=scope.shared_scope_id,
                platform=scope.scope.platform,
                user_id=scope.scope.user_id,
                chat_id=scope.scope.chat_id,
                thread_id=scope.scope.thread_id,
                gateway_session_key="",
                agent_identity=scope.scope.agent_identity,
                agent_workspace=scope.scope.agent_workspace,
                session_id=candidate.session_id,
                source="nightly-digest",
                target=candidate.target,
                content=candidate.content,
                metadata=json.dumps({**_cross_platform_metadata(scope.scope, runtime_config), **candidate_metadata(candidate, run_id)}, ensure_ascii=False, sort_keys=True),
            )
            if inserted:
                record_digest_source(conn, memory_id=stored_id, run_id=run_id, candidate=candidate)
                conn.commit()
                if vector_runtime is not None:
                    upsert_vector_record(
                        vector_runtime,
                        id=stored_id,
                        source="nightly-digest",
                        target=candidate.target,
                        content=candidate.content,
                        summary=summary,
                        updated_at=updated_at,
                        scope_id=scope.shared_scope_id,
                    )
            else:
                counts["inserted"] -= 1
                counts["skipped"] += 1
    deleted = 0 if dry_run else cleanup_exact_duplicates(conn, scope, vector_runtime)
    counts["deleted"] += deleted
    return {"counts": dict(counts), "actions": actions}


def cleanup_exact_duplicates(conn: sqlite3.Connection, scope: ScopeProfile, vector_runtime: DigestVectorRuntime | None) -> int:
    writable_scopes = _profile_writable_scope_ids(scope)
    groups = exact_duplicate_groups(conn, scope_ids=writable_scopes)
    delete_ids = [memory_id for group in groups for memory_id in group["delete_ids"]]
    if not delete_ids:
        return 0
    deleted = delete_rows(conn, delete_ids, scope_ids=writable_scopes)
    if vector_runtime is not None and vector_runtime._vector_store is not None:
        try:
            vector_runtime._vector_store.delete_by_ids(delete_ids)
        except Exception:
            pass
    return deleted


def collect_candidates(
    bundles: list[SessionBundle],
    *,
    options: DigestOptions,
    llm_config: dict[str, Any],
    existing_context: list[str],
    fallback_events: list[dict[str, Any]] | None = None,
) -> list[DigestCandidate]:
    candidates: list[DigestCandidate] = []
    fallback_events = fallback_events if fallback_events is not None else []
    for bundle in bundles:
        if options.extractor == "heuristic":
            candidates.extend(heuristic_candidates(bundle))
            continue
        bundle_candidates: list[DigestCandidate] = []
        llm_failed = False
        for chunk in session_chunks(bundle, chunk_chars=options.chunk_chars, max_session_chars=options.max_session_chars):
            prompt = build_prompt(bundle, chunk, existing_context)
            try:
                raw = _call_llm_with_retries(
                    prompt,
                    model=llm_config["model"],
                    base_url=llm_config["base_url"],
                    api_key=llm_config["api_key"],
                    timeout=options.timeout,
                    api_mode=llm_config.get("api_mode", "chat_completions"),
                    endpoint=str(llm_config.get("endpoint") or ""),
                    append_v1=bool(llm_config.get("append_v1", True)),
                    max_attempts=options.max_attempts,
                    retry_delay=options.retry_delay,
                )
            except Exception as exc:
                event = _fallback_event(bundle=bundle, exc=exc, attempts=options.max_attempts)
                if (not options.allow_heuristic_fallback) or not event["retryable"]:
                    raise
                fallback_events.append(event)
                bundle_candidates.extend(heuristic_candidates(bundle))
                llm_failed = True
                break
            parsed = parse_llm_candidates(raw, bundle=bundle)
            if parsed:
                bundle_candidates.extend(parsed)
        if llm_failed:
            candidates.extend(bundle_candidates)
            continue
        if not bundle_candidates:
            bundle_candidates.extend(heuristic_candidates(bundle))
        candidates.extend(bundle_candidates)
    return candidates


def run_digest(options: DigestOptions) -> dict[str, Any]:
    hermes_home = options.hermes_home.expanduser().resolve()
    db_path = resolve_session_db(hermes_home, options.state_db)
    if db_path is None or not db_path.exists():
        return {"ok": True, "status": "no_session_db", "digest_date": str(options.digest_date), "sessions": 0}
    bundles = load_session_bundles(
        db_path,
        digest_date=options.digest_date,
        timezone_name=options.timezone_name,
        session_id=options.session_id,
        limit_sessions=options.limit_sessions,
    )
    if not bundles:
        return {"ok": True, "status": "no_messages", "digest_date": str(options.digest_date), "source_db": str(db_path), "sessions": 0}

    storage_dir = hermes_home / "scope-recall"
    if not options.dry_run:
        storage_dir.mkdir(parents=True, exist_ok=True)
    memory_db = storage_dir / "memory.sqlite3"
    if options.dry_run and memory_db.exists():
        conn = sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True, timeout=30)
    elif options.dry_run:
        conn = sqlite3.connect(":memory:")
    else:
        storage_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(memory_db, timeout=30)
    conn.row_factory = sqlite3.Row
    if not options.dry_run:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        ensure_schema(conn)
        ensure_digest_schema(conn)
    elif not memory_db.exists():
        ensure_schema(conn)

    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    llm_config = resolve_llm_config(hermes_home, options)
    runtime_config = load_runtime_config(Path(__file__).resolve().parent, storage_dir)
    fallback_platform = next((bundle.source for bundle in bundles if bundle.source), "cli")
    fallback_user_id = next((bundle.user_id for bundle in bundles if bundle.user_id), "")
    scope = infer_scope(conn, fallback_platform=fallback_platform, fallback_user_id=fallback_user_id, runtime_config=runtime_config)
    vector_runtime: DigestVectorRuntime | None = None
    try:
        vector_runtime = None if options.dry_run else DigestVectorRuntime(hermes_home=hermes_home, conn=conn, scope=scope)
        existing = existing_memory_context(conn, scope)
        fallback_events: list[dict[str, Any]] = []
        candidates = collect_candidates(bundles, options=options, llm_config=llm_config, existing_context=existing, fallback_events=fallback_events)
        applied = apply_candidates(conn, vector_runtime, scope, run_id=run_id, candidates=candidates, dry_run=options.dry_run, runtime_config=runtime_config)
        counts = Counter(applied["counts"])
        extractor_used = "heuristic" if options.extractor == "heuristic" else ("heuristic-fallback" if fallback_events else "llm")
        status = "dry_run" if options.dry_run else ("ok_with_fallback" if fallback_events else "ok")
        result = {
            "ok": True,
            "status": status,
            "run_id": run_id,
            "digest_date": str(options.digest_date),
            "source_db": str(db_path),
            "sessions": len(bundles),
            "task_sessions": sum(1 for bundle in bundles if bundle.is_task),
            "candidates": len(candidates),
            "inserted": counts.get("inserted", 0),
            "updated": counts.get("updated", 0),
            "skipped": counts.get("skipped", 0),
            "deleted": counts.get("deleted", 0),
            "extractor": options.extractor,
            "extractor_used": extractor_used,
            "extractor_fallbacks": fallback_events[:20],
            "model": llm_config.get("model", ""),
            "actions": applied["actions"][:50],
        }
        if not options.dry_run:
            conn.execute(
                """
                INSERT INTO nightly_digest_runs(
                    id, digest_date, source_db, started_at, finished_at, extractor, model, dry_run,
                    status, inserted, updated, skipped, deleted, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(options.digest_date),
                    str(db_path),
                    started_at,
                    datetime.now(timezone.utc).isoformat(),
                    options.extractor,
                    llm_config.get("model", ""),
                    0,
                    status,
                    result["inserted"],
                    result["updated"],
                    result["skipped"],
                    result["deleted"],
                    json.dumps(
                        {
                            "sessions": len(bundles),
                            "task_sessions": result["task_sessions"],
                            "extractor_used": extractor_used,
                            "extractor_fallbacks": fallback_events[:20],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
        return result
    except Exception as exc:
        if not options.dry_run:
            conn.execute(
                """
                INSERT OR REPLACE INTO nightly_digest_runs(
                    id, digest_date, source_db, started_at, finished_at, extractor, model, dry_run,
                    status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(options.digest_date),
                    str(db_path),
                    started_at,
                    datetime.now(timezone.utc).isoformat(),
                    options.extractor,
                    llm_config.get("model", ""),
                    0,
                    "error",
                    redact_sensitive(str(exc)[:1000]),
                ),
            )
            conn.commit()
        raise
    finally:
        if vector_runtime is not None:
            vector_runtime.close()
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract durable memories from Hermes daily conversations")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", "~/.hermes"), help="Hermes home/profile path")
    parser.add_argument("--date", help="Local digest date, YYYY-MM-DD. Defaults to today in --timezone.")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="Local timezone for day boundaries")
    parser.add_argument("--dry-run", action="store_true", help="Plan changes without writing memories")
    parser.add_argument("--extractor", choices=["llm", "heuristic"], default="llm", help="Extraction backend")
    parser.add_argument("--state-db", help="Override Hermes state/lcm database path")
    parser.add_argument("--session-id", default="", help="Restrict digest to one session")
    parser.add_argument("--limit-sessions", type=int, default=0, help="Limit sessions for smoke tests")
    parser.add_argument("--model", default="", help="Override chat completion model")
    parser.add_argument("--base-url", default="", help="Override OpenAI-compatible base URL")
    parser.add_argument("--endpoint", default="", help="Override full chat completions endpoint")
    parser.add_argument("--append-v1", action=argparse.BooleanOptionalAction, default=None, help="Append /v1 before /chat/completions for base URLs")
    parser.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=60.0, help="LLM request timeout seconds")
    parser.add_argument("--llm-max-attempts", type=int, default=2, help="LLM retry attempts before falling back or failing")
    parser.add_argument("--llm-retry-delay", type=float, default=1.0, help="Seconds to wait between retryable LLM failures")
    parser.add_argument("--no-heuristic-fallback", action="store_true", help="Fail instead of using heuristic extraction after retryable LLM failures")
    parser.add_argument("--verbose", action="store_true", help="Print detailed JSON")
    return parser


def options_from_args(args: argparse.Namespace) -> DigestOptions:
    return DigestOptions(
        hermes_home=Path(args.hermes_home),
        digest_date=parse_date(args.date, timezone_name=args.timezone),
        timezone_name=args.timezone,
        dry_run=bool(args.dry_run),
        extractor=str(args.extractor),
        state_db=Path(args.state_db).expanduser() if args.state_db else None,
        session_id=str(args.session_id or ""),
        limit_sessions=max(0, int(args.limit_sessions or 0)),
        model=str(args.model or ""),
        base_url=str(args.base_url or ""),
        endpoint=str(args.endpoint or ""),
        append_v1=args.append_v1,
        api_key=str(args.api_key or ""),
        timeout=float(args.timeout or 60.0),
        max_attempts=max(1, int(args.llm_max_attempts or 1)),
        retry_delay=max(0.0, float(args.llm_retry_delay or 0.0)),
        allow_heuristic_fallback=not bool(args.no_heuristic_fallback),
        verbose=bool(args.verbose),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    started = time.time()
    try:
        result = run_digest(options_from_args(args))
        result["elapsed_seconds"] = round(time.time() - started, 3)
        if args.verbose or args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            compact = {key: result.get(key) for key in ("ok", "status", "digest_date", "sessions", "candidates", "inserted", "updated", "skipped", "deleted")}
            print(json.dumps(compact, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
