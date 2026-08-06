"""Tests for nightly digest collection, candidate application, fallback, and run status.

They make scheduled summarization observable rather than an opaque cron side effect."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scope_recall.nightly_digest as nightly_digest
from scope_recall.digest_quality import score_digest_candidate
from scope_recall.nightly_digest import (
    DigestCandidate,
    DigestOptions,
    MessageRecord,
    ScopeProfile,
    SessionBundle,
    _parse_llm_candidates_with_status,
    apply_candidates,
    call_llm,
    candidate_is_allowed,
    candidate_metadata,
    cleanup_exact_duplicates,
    existing_memory_context,
    heuristic_candidates,
    load_session_bundles,
    redact_sensitive,
    resolve_llm_config,
    run_digest,
    safe_command_hints,
    session_chunks,
)
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import delete_rows, ensure_schema, store_row
from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation


def _ts(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def _write_config(hermes_home: Path) -> None:
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")


def _parse_test_bundle() -> SessionBundle:
    return SessionBundle(
        id="parse-test",
        source="test",
        title="parse test",
        messages=[MessageRecord(id=1, session_id="parse-test", role="user", content="remember stable fact", timestamp=0.0)],
        is_task=True,
        completed=True,
    )


def test_llm_candidate_parser_treats_empty_response_as_empty_not_parse_error():
    candidates, status = _parse_llm_candidates_with_status("", bundle=_parse_test_bundle())

    assert candidates == []
    assert status == "empty"


def test_safe_command_hints_keeps_generic_safe_tool_categories_without_raw_commands():
    hints = safe_command_hints(
        [
            "python3 -m pytest tests/test_release.py -q",
            "uv run custom-health-check --json",
            "./scripts/custom-check.sh --path /home/a/private/file",
            "npm run test",
        ]
    )

    assert hints == ["pytest", "uv", "custom-script", "npm"]
    assert all("/home/a" not in hint and "custom-check" not in hint for hint in hints)


def test_llm_candidate_parser_extracts_json_array_from_fenced_text_with_preamble():
    raw = """Here is the JSON:\n```json\n[{\"action\": \"create\", \"evidence_message_ids\": [1], \"content\": \"Stable reusable workflow: when release rollback fails, validate backup before deleting current plugin and verify rollback receipt paths.\", \"target\": \"ops\", \"memory_type\": \"workflow\", \"importance\": \"high\", \"confidence\": \"high\", \"entities\": [\"rollback\"], \"tags\": [\"workflow\"], \"reason\": \"Reusable rollback safety workflow\"}]\n```\n"""

    candidates, status = _parse_llm_candidates_with_status(raw, bundle=_parse_test_bundle())

    assert status == "parsed"
    assert len(candidates) == 1
    assert candidates[0].target == "ops"
    assert candidates[0].memory_type == "workflow"
    assert candidates[0].importance >= 0.55
    assert candidates[0].confidence >= 0.65


def test_llm_candidate_parser_extracts_unfenced_json_array_with_preamble_and_epilogue():
    raw = """可以，下面是提取结果：\n[{\"action\": \"create\", \"evidence_message_ids\": [1], \"content\": \"Stable reusable workflow: before replaying journal dead letters, classify auth, quota, timeout, parse, and low-value failures separately with audit evidence.\", \"target\": \"ops\", \"memory_type\": \"workflow\", \"importance\": 0.72, \"confidence\": 0.81, \"entities\": [\"journal recovery\"], \"tags\": [\"journal-digest\"], \"reason\": \"Reusable recovery workflow\"}]\n以上。"""

    candidates, status = _parse_llm_candidates_with_status(raw, bundle=_parse_test_bundle())

    assert status == "parsed"
    assert len(candidates) == 1
    assert candidates[0].target == "ops"
    assert candidates[0].memory_type == "workflow"


def _provenance_bundle() -> SessionBundle:
    return SessionBundle(
        id="provenance-session",
        source="test",
        title="chunk provenance",
        messages=[
            MessageRecord(
                id=734829,
                session_id="provenance-session",
                role="user",
                content="Scope Recall uses chunk-scoped provenance for each stable fact candidate.",
                timestamp=0.0,
            ),
            MessageRecord(
                id=734830,
                session_id="provenance-session",
                role="user",
                content="A different fact belongs to a later chunk and must remain independently pending.",
                timestamp=1.0,
            ),
        ],
        is_task=True,
        completed=True,
    )


def _provenance_response(message_id: int) -> str:
    return json.dumps(
        [
            {
                "action": "ADD",
                "content": (
                    "Stable extraction rule: every durable candidate keeps only the exact "
                    "message identifiers that the model actually cited in its visible chunk."
                ),
                "claim": {
                    "subject": "Scope Recall",
                    "predicate": "uses",
                    "value": "chunk-scoped provenance",
                },
                "evidence_message_ids": [message_id],
                "target": "ops",
                "memory_type": "workflow",
                "importance": 0.8,
                "confidence": 0.9,
                "reason": "exact provenance regression",
            }
        ]
    )


def test_session_chunks_render_real_ids_and_reject_cross_chunk_citations() -> None:
    bundle = _provenance_bundle()
    chunks = session_chunks(bundle, chunk_chars=190, max_session_chars=1000)

    assert len(chunks) >= 2
    first = chunks[0]
    assert "[message_id=734829 role=user]" in first.text
    assert first.message_ids == (734829,)
    assert "734830" not in first.text

    forged, forged_status = _parse_llm_candidates_with_status(
        _provenance_response(734830),
        bundle=bundle,
        allowed_message_ids=set(first.message_ids),
    )
    valid, valid_status = _parse_llm_candidates_with_status(
        _provenance_response(734829),
        bundle=bundle,
        allowed_message_ids=set(first.message_ids),
    )

    assert forged == []
    assert forged_status == "filtered"
    assert valid_status == "parsed"
    assert valid[0].message_ids == [734829]


def test_candidate_provenance_is_exact_not_the_whole_session() -> None:
    bundle = _provenance_bundle()

    candidates, status = _parse_llm_candidates_with_status(
        _provenance_response(734829),
        bundle=bundle,
        allowed_message_ids={734829, 734830},
    )

    assert status == "parsed"
    assert candidates[0].message_ids == [734829]


def test_candidate_can_bind_an_exposed_id_beyond_the_old_eighty_message_cap() -> None:
    bundle = SessionBundle(
        id="long-provenance-session",
        messages=[
            MessageRecord(
                id=800000 + index,
                session_id="long-provenance-session",
                role="user",
                content="Scope Recall uses chunk-scoped provenance for every durable candidate.",
                timestamp=float(index),
            )
            for index in range(100)
        ],
    )
    tail_id = 800099

    candidates, status = _parse_llm_candidates_with_status(
        _provenance_response(tail_id),
        bundle=bundle,
        allowed_message_ids={tail_id},
    )

    assert status == "parsed"
    assert candidates[0].message_ids == [tail_id]


@pytest.mark.parametrize(
    ("chunk_chars", "max_session_chars", "contents"),
    [
        (1000, 300, ["A bounded fact message " + "x" * 800]),
        (240, 1000, [f"Stable message {index} " + "y" * 180 for index in range(10)]),
        (220, 500, ["玉衡验证 Unicode 全局预算 🌟" * 120]),
        (120, 5000, [" ".join(f"bounded-token-{index}" for index in range(200))]),
    ],
)
def test_session_chunks_never_exceed_global_or_per_call_budget(
    chunk_chars: int,
    max_session_chars: int,
    contents: list[str],
) -> None:
    bundle = SessionBundle(
        id="budget-session",
        title="global budget",
        messages=[
            MessageRecord(
                id=900000 + index,
                session_id="budget-session",
                role="user",
                content=content,
                timestamp=float(index),
            )
            for index, content in enumerate(contents)
        ],
    )

    chunks = session_chunks(
        bundle,
        chunk_chars=chunk_chars,
        max_session_chars=max_session_chars,
    )

    assert chunks
    assert sum(chunk.exposed_chars for chunk in chunks) <= max_session_chars
    assert all(chunk.exposed_chars == len(chunk.text) for chunk in chunks)
    assert all(0 < chunk.exposed_chars <= chunk_chars for chunk in chunks)
    assert all(chunk.input_chars == chunks[0].input_chars for chunk in chunks)
    assert all(chunk.message_ids for chunk in chunks)
    assert all(
        any(f"message_id={message_id}" in chunk.text for message_id in chunk.message_ids)
        for chunk in chunks
    )
    assert all(chunk.truncated for chunk in chunks)


def test_original_long_session_counterexample_is_globally_bounded() -> None:
    bundle = SessionBundle(
        id="audit-long-session",
        title="RB-3 replay",
        messages=[
            MessageRecord(
                id=910000 + index,
                session_id="audit-long-session",
                role="user",
                content=f"Valid durable preference message {index}: " + "q" * 820,
                timestamp=float(index),
            )
            for index in range(20)
        ],
    )

    chunks = session_chunks(bundle, chunk_chars=1000, max_session_chars=5000)

    assert sum(len(chunk.text) for chunk in chunks) <= 5000
    assert chunks[0].input_chars > 5000
    assert chunks[-1].truncated is True


def test_session_chunk_exact_boundary_is_not_reported_as_truncated() -> None:
    bundle = SessionBundle(
        id="exact-budget-session",
        messages=[
            MessageRecord(
                id=920001,
                session_id="exact-budget-session",
                role="user",
                content="A short stable preference with an exact bounded exposure.",
                timestamp=0.0,
            )
        ],
    )
    unbounded = session_chunks(bundle, chunk_chars=10000, max_session_chars=10000)
    exact_limit = len(unbounded[0].text)

    chunks = session_chunks(
        bundle,
        chunk_chars=exact_limit,
        max_session_chars=exact_limit,
    )

    assert len(chunks) == 1
    assert chunks[0].exposed_chars == exact_limit
    assert chunks[0].truncated is False


def test_tiny_budget_that_cannot_show_a_message_id_returns_no_chunk() -> None:
    bundle = _provenance_bundle()

    assert session_chunks(bundle, chunk_chars=20, max_session_chars=20) == []


def _create_state_db(path: Path, day: date, *, content_suffix: str = "") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                title TEXT,
                started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions(id, source, user_id, model, title, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-task", "telegram", "9000000001", "deepseek-v4-pro", "scope-recall live validation", _ts(day, 9)),
        )
        tool_calls = [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "python -m pytest -q && python scripts/check.release.py"}),
                },
            },
            {"type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]
        messages = [
            ("user", f"帮我验证 scope-recall 插件并修复记忆能力。API_KEY=secret1234567890 {content_suffix}", "", ""),
            ("assistant", "我会先读代码，再跑测试，最后做玉衡实机 smoke。", json.dumps(tool_calls), ""),
            ("tool", "{\"output\":\"117 passed, release gate ok, token=abcdef1234567890\"}", "", "terminal"),
            ("assistant", "完成：pytest 117 passed，release gate ok，玉衡 live smoke 验证通过。", "", ""),
        ]
        for role, content, calls, tool_name in messages:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                ("session-task", role, content, calls, tool_name, _ts(day, 10)),
            )
        conn.commit()
    finally:
        conn.close()


def _create_progress_only_state_db(path: Path, day: date) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                title TEXT,
                started_at REAL NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions(id, source, user_id, model, title, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-progress", "telegram", "9000000001", "gpt-5.5", "停服维护进度更新 #10", _ts(day, 9)),
        )
        messages = [
            ("user", "进度如何了", "", ""),
            ("assistant", "当前进度：用了 terminal/read_file；还没验证，继续处理中。", "", ""),
            ("tool", '{"output":"still running"}', "", "terminal"),
        ]
        for role, content, calls, tool_name in messages:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_calls, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                ("session-progress", role, content, calls, tool_name, _ts(day, 10)),
            )
        conn.commit()
    finally:
        conn.close()


def test_digest_quality_rejects_transient_tool_progress_candidate():
    candidate = DigestCandidate(
        content="停服维护进度更新 #10 的可复用任务流程：使用工具链 terminal/read_file。结果摘要：当前进度只是用了哪些工具，还没验证完成。",
        target="ops",
        memory_type="workflow",
        confidence=0.8,
        reason="progress update",
    )

    quality = score_digest_candidate(candidate)

    assert quality.transient_progress is True
    assert quality.recommended_action == "reject"
    assert candidate_is_allowed(candidate) is False


def test_heuristic_digest_sanitizes_private_command_hints_before_metadata():
    private_posix = "/".join(["", "home", "a", ".hermes-yuheng", "plugins", "scope-recall"])
    private_windows = "C:" + "/".join(["", "Users", "Administrator", "AppData", "Local", "Temp", "codex-abc", "result.json"])
    bundle = SessionBundle(
        id="private-command-hints",
        source="test",
        title="Scope Recall release check",
        messages=[
            MessageRecord(id=1, session_id="private-command-hints", role="user", content="请检查 scope-recall 发布。", timestamp=0.0),
            MessageRecord(id=2, session_id="private-command-hints", role="assistant", content="完成：验证通过。", timestamp=0.0),
        ],
        tool_names=["terminal"],
        command_hints=[
            f"git -C {private_posix} status --short",
            f"Get-Content {private_windows}",
        ],
        is_task=True,
        completed=True,
    )

    candidates = heuristic_candidates(bundle)

    assert len(candidates) == 1
    candidate = candidates[0]
    metadata = candidate_metadata(candidate, "run-private-command-hints")
    persisted = json.dumps(metadata, ensure_ascii=False) + "\n" + candidate.content
    assert candidate_is_allowed(candidate) is True
    assert "/home/" not in persisted
    assert "C:/Users" not in persisted
    assert "AppData" not in persisted
    assert "git -C" not in persisted
    assert "Get-Content" not in persisted
    assert "Command hints" not in persisted


def test_digest_candidate_rejects_raw_private_command_paths():
    private_posix = "/".join(["", "home", "a", ".hermes-yuheng", "plugins", "scope-recall"])
    candidate = DigestCandidate(
        content=f"Scope Recall 发布流程：步骤是运行 git -C {private_posix} status 后再验证 release gate。",
        target="ops",
        memory_type="workflow",
        confidence=0.82,
        commands=[f"git -C {private_posix} status --short"],
        verification=["release gate ok"],
        reason="raw private command path regression",
    )

    quality = score_digest_candidate(candidate)

    assert quality.contains_raw_tool_trace is True
    assert quality.recommended_action == "reject"
    assert candidate_is_allowed(candidate) is False


def test_digest_quality_allows_verified_reusable_workflow_and_records_metadata():
    candidate = DigestCandidate(
        content="Scope Recall 发布收口流程：触发条件是准备发布插件；步骤是先跑 pytest，再跑 release gate，最后回读 release/PyPI；验证：512 passed，release gate ok。",
        target="ops",
        memory_type="workflow",
        confidence=0.82,
        commands=["python3 -m pytest -q", "python3 scripts/check.release.py"],
        verification=["512 passed", "release gate ok"],
        reason="reusable release workflow",
    )

    quality = score_digest_candidate(candidate)
    metadata = candidate_metadata(candidate, "run-quality")

    assert quality.reusable is True
    assert quality.recommended_action == "promote"
    assert candidate_is_allowed(candidate) is True
    assert metadata["digest_quality"]["recommended_action"] == "promote"
    assert metadata["digest_quality"]["has_verification"] is True


def test_digest_candidate_quality_action_is_stored_as_review_candidate_lifecycle():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    scope = ScopeProfile(
        scope=RuntimeScope(platform="telegram", user_id="joy", chat_id="dm", agent_identity="yuheng", agent_workspace="hermes"),
        scope_id="scope-local",
        shared_scope_id="scope-shared",
        accessible_scope_ids=["scope-local", "scope-shared"],
        writable_scope_ids=["scope-local", "scope-shared"],
    )
    candidate = DigestCandidate(
        content="Scope Recall 状态摘要：这条记忆需要人工复审后再晋升，包含上下文但缺少完整验证闭环。",
        target="ops",
        memory_type="summary",
        confidence=0.5,
        reason="low confidence summary candidate",
    )

    result = apply_candidates(conn, None, scope, run_id="run-candidate", candidates=[candidate], dry_run=False, runtime_config={})
    row = conn.execute("SELECT metadata FROM memories").fetchone()
    metadata = json.loads(row["metadata"])

    assert result["quality_counts"] == {"candidate": 1}
    assert result["counts"]["inserted"] == 1
    assert metadata["digest_quality"]["recommended_action"] == "candidate"
    assert metadata["lifecycle"] == "candidate"
    assert metadata["candidate_status"] == "needs_review"


def test_nightly_digest_hides_provisional_context_but_uses_it_for_deduplication():
    for lifecycle in ("archived", "candidate", "in_progress"):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        scope = ScopeProfile(
            scope=RuntimeScope(platform="telegram", user_id="joy", chat_id="dm", agent_identity="yuheng", agent_workspace="hermes"),
            scope_id="scope-local",
            shared_scope_id="scope-shared",
            accessible_scope_ids=["scope-local", "scope-shared"],
            writable_scope_ids=["scope-local", "scope-shared"],
        )
        content = (
            "Nightly extraction must hide provisional rows from ordinary context while still using them "
            "to prevent duplicate provisional candidates."
        )
        store_row(
            conn,
            memory_id=f"hidden-{lifecycle}",
            scope_id="scope-shared",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="hidden-match",
            source="nightly-digest",
            target="memory",
            content=content,
        )
        conn.execute(
            "UPDATE memories SET metadata = json_set(metadata, '$.lifecycle', ?) WHERE id = ?",
            (lifecycle, f"hidden-{lifecycle}"),
        )
        conn.commit()

        assert all(content not in item for item in existing_memory_context(conn, scope))
        result = apply_candidates(
            conn,
            None,
            scope,
            run_id=f"run-{lifecycle}",
            candidates=[
                DigestCandidate(
                    content=content,
                    target="memory",
                    memory_type="workflow",
                    confidence=0.9,
                    reason="verified release workflow",
                    verification=["release gate passed"],
                )
            ],
            dry_run=False,
            runtime_config={},
        )
        if lifecycle == "archived":
            assert result["counts"]["inserted"] == 1
            assert result["counts"].get("skipped", 0) == 0
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
            inserted = conn.execute(
                "SELECT metadata FROM memories WHERE id != ?",
                (f"hidden-{lifecycle}",),
            ).fetchone()
            assert json.loads(inserted["metadata"])["lifecycle"] == "candidate"
        else:
            assert result["counts"]["skipped"] == 1
            assert result["counts"].get("inserted", 0) == 0
            assert result["actions"][0]["reason"] == "existing memory covers candidate"
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        hidden = conn.execute(
            "SELECT metadata FROM memories WHERE id = ?", (f"hidden-{lifecycle}",)
        ).fetchone()
        assert json.loads(hidden["metadata"])["lifecycle"] == lifecycle
        conn.close()


def test_nightly_similar_candidate_is_sanitized_without_rewriting_active_memory():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    scope = ScopeProfile(
        scope=RuntimeScope(platform="cli", user_id="joy", agent_identity="yuheng", agent_workspace="hermes"),
        scope_id="scope-a",
        shared_scope_id="scope-a",
        accessible_scope_ids=["scope-a"],
        writable_scope_ids=["scope-a"],
    )
    existing = (
        "When release review finds stale vector companions, run vector repair dry-run then apply only after "
        "truth hash verification passes."
    )
    candidate_content = (
        "When release review finds stale vector companions, run vector repair dry run and apply after the "
        "truth hash check passes."
    )
    store_row(
        conn,
        memory_id="active-1",
        scope_id="scope-a",
        platform="cli",
        user_id="joy",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="yuheng",
        agent_workspace="hermes",
        session_id="semantic-update",
        source="nightly-digest",
        target="memory",
        content=existing,
    )
    marker = "S" * 24
    private_path = "/home/synthetic/.ssh/id_rsa"
    result = apply_candidates(
        conn,
        None,
        scope,
        run_id="secret-metadata-update",
        candidates=[
            DigestCandidate(
                content=candidate_content,
                target="memory",
                memory_type="workflow",
                importance=0.9,
                confidence=0.95,
                reason="verified release workflow",
                tags=["api_key=sk-" + marker],
                entities=[private_path],
                verification=["release gate passed"],
            )
        ],
        dry_run=False,
        runtime_config={},
    )

    assert result["counts"]["inserted"] == 1
    active = conn.execute(
        "SELECT content, metadata FROM memories WHERE id='active-1'"
    ).fetchone()
    candidate = conn.execute(
        "SELECT id, content, metadata FROM memories WHERE id <> 'active-1'"
    ).fetchone()
    assert active["content"] == existing
    assert candidate is not None
    assert candidate["content"] == candidate_content
    rendered = candidate["metadata"]
    parsed = json.loads(rendered)
    entities = "\n".join(
        str(row[0])
        for row in conn.execute(
            "SELECT entity FROM memory_entities WHERE memory_id=?", (candidate["id"],)
        ).fetchall()
    )
    assert parsed["lifecycle"] == "candidate"
    assert parsed["automatic_admission"]["route"] == "experience_review"
    assert marker not in rendered and marker.lower() not in rendered
    assert "api_key=" not in rendered
    assert private_path not in rendered
    assert private_path not in entities
    assert "[REDACTED_" in rendered or "[redacted_" in rendered
    conn.close()


def test_heuristic_digest_rejects_progress_toolchain_only_summary(tmp_path):
    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_progress_only_state_db(hermes_home / "state.db", day)

    result = run_digest(DigestOptions(hermes_home=hermes_home, digest_date=day, extractor="heuristic"))

    assert result["ok"] is True
    assert result["inserted"] == 0
    assert result["skipped"] == 1
    assert result["quality_counts"] == {"reject": 1}
    assert result["actions"][0]["reason"] == "quality rejected"
    assert result["actions"][0]["quality"]["recommended_action"] == "reject"
    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        conn.close()


def test_digest_llm_config_can_use_dedicated_provider_without_inheriting_codex_endpoint(tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("DEEPSEEK_API_KEY=deepseek-test-key\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        """
model:
  provider: openai-codex
  default: gpt-5.5
  base_url: https://chatgpt.com/backend-api/codex
providers:
  deepseek:
    base_url: https://api.deepseek.com
    default_model: deepseek-v4-pro
    key_env: DEEPSEEK_API_KEY
scope_recall_nightly_digest:
  provider: deepseek
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = resolve_llm_config(hermes_home, DigestOptions(hermes_home=hermes_home, digest_date=date(2026, 6, 13)))

    assert config["model"] == "deepseek-v4-pro"
    assert config["base_url"] == "https://api.deepseek.com"
    assert config["api_key"] == "deepseek-test-key"
    assert config["api_mode"] == "chat_completions"


def test_digest_llm_config_detects_codex_responses_mode(tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("CODEX_API_KEY=codex-test-token\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        """
model:
  provider: openai-codex
  default: gpt-5.5
  base_url: https://chatgpt.com/backend-api/codex
providers:
  openai-codex:
    base_url: https://chatgpt.com/backend-api/codex
    key_env: CODEX_API_KEY
scope_recall_nightly_digest:
  provider: openai-codex
  model: gpt-5.5
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = resolve_llm_config(hermes_home, DigestOptions(hermes_home=hermes_home, digest_date=date(2026, 6, 13)))

    assert config["model"] == "gpt-5.5"
    assert config["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert config["api_key"] == "codex-test-token"
    assert config["provider"] == "openai-codex"
    assert config["api_mode"] == "codex_responses"


def test_call_llm_codex_responses_uses_responses_endpoint_and_extracts_text(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return (
                'data: {"type":"response.output_text.delta","delta":"[{\\"content\\":\\"codex digest memory\\"}]"}\n\n'
                'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"[{\\"content\\":\\"codex digest memory\\"}]"}]}}\n\n'
                'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout, allow_insecure=False):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()


    monkeypatch.setattr("scope_recall.nightly_llm.safe_urlopen", fake_urlopen)
    fake_codex_token = "token" + "-without" + "-jwt" + "-claims"

    raw = call_llm(
        "extract this",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key=fake_codex_token,
        timeout=12,
        api_mode="codex_responses",
    )

    assert raw == '[{"content":"codex digest memory"}]'
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["body"]["model"] == "gpt-5.5"
    assert captured["body"]["instructions"] == "You extract durable memory as strict JSON."
    assert captured["body"]["store"] is False
    assert captured["body"]["stream"] is True
    assert "messages" not in captured["body"]
    assert captured["headers"]["Authorization"] == f"Bearer {fake_codex_token}"
    assert captured["headers"]["Originator"] == "codex_cli_rs"
    assert captured["timeout"] == 12


def test_call_llm_openai_compatible_uses_chat_completions_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode("utf-8")

    def fake_urlopen(request, *, timeout, allow_insecure=False):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()


    monkeypatch.setattr("scope_recall.nightly_llm.safe_urlopen", fake_urlopen)

    raw = call_llm(
        "extract this",
        model="gpt-4o-mini",
        base_url="https://api.openai.com",
        api_key="openai-key",
        timeout=12,
        api_mode="chat_completions",
    )

    assert raw == "[]"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["body"]["messages"][0]["role"] == "system"
    assert captured["body"]["messages"][1]["content"] == "extract this"
    assert captured["headers"]["Authorization"] == "Bearer openai-key"


def test_call_llm_chat_completions_respects_explicit_endpoint_without_appending_v1(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode("utf-8")

    def fake_urlopen(request, *, timeout, allow_insecure=False):
        captured["url"] = request.full_url
        return FakeResponse()


    monkeypatch.setattr("scope_recall.nightly_llm.safe_urlopen", fake_urlopen)

    raw = call_llm(
        "extract this",
        model="ark-code-latest",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        endpoint="https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
        api_key="ark-key",
        timeout=12,
        api_mode="chat_completions",
    )

    assert raw == "[]"
    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"


def test_call_llm_chat_completions_append_v1_false_uses_provider_specific_root(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode("utf-8")

    def fake_urlopen(request, *, timeout, allow_insecure=False):
        captured["url"] = request.full_url
        return FakeResponse()


    monkeypatch.setattr("scope_recall.nightly_llm.safe_urlopen", fake_urlopen)

    call_llm(
        "extract this",
        model="ark-code-latest",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        api_key="ark-key",
        timeout=12,
        api_mode="chat_completions",
        append_v1=False,
    )

    assert captured["url"] == "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"


def test_call_llm_http_error_mentions_public_endpoint_without_secret_or_private_path(
    monkeypatch,
):
    import io
    from email.message import Message

    fake_secret = "sk-" + "a" * 28

    def fake_urlopen(request, *, timeout, allow_insecure=False):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            hdrs=Message(),
            fp=io.BytesIO(f"provider error api_key={fake_secret}".encode("utf-8")),
        )


    monkeypatch.setattr("scope_recall.nightly_llm.safe_urlopen", fake_urlopen)

    try:
        call_llm(
            "extract this",
            model="ark-code-latest",
            base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
            api_key="ark-key",
            timeout=12,
            api_mode="chat_completions",
            append_v1=False,
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "https://ark.cn-beijing.volces.com/chat/completions" in message
    assert "/api/coding/v3/" not in message
    assert fake_secret not in message
    assert "[REDACTED]" in message


def test_digest_llm_config_exposes_append_v1_false_for_provider_specific_roots(tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("ARK_API_KEY=ark-test-key\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        """
model:
  provider: ark
  default: ark-code-latest
providers:
  ark:
    base_url: https://ark.cn-beijing.volces.com/api/coding/v3
    key_env: ARK_API_KEY
scope_recall_nightly_digest:
  provider: ark
  append_v1: false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = resolve_llm_config(hermes_home, DigestOptions(hermes_home=hermes_home, digest_date=date(2026, 6, 13)))

    assert config["model"] == "ark-code-latest"
    assert config["base_url"] == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert config["api_key"] == "ark-test-key"
    assert config["api_mode"] == "chat_completions"
    assert config["append_v1"] is False


def test_redact_sensitive_handles_assignment_and_bearer_without_leaking_secret():
    fake_bearer = "abcd" + "efgh" + "ijkl" + "mnopqrstuvwxyz"
    text = redact_sensitive("api_key=sk-secretsecretsecret bearer " + fake_bearer)
    assert "sk-secret" not in text
    assert fake_bearer not in text
    assert "[REDACTED]" in text


def test_load_session_bundles_keeps_tool_summary_but_not_raw_tool_content(tmp_path):
    day = date(2026, 6, 1)
    db_path = tmp_path / "state.db"
    _create_state_db(db_path, day)

    bundles = load_session_bundles(db_path, digest_date=day, timezone_name="Asia/Shanghai")

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.is_task is True
    assert "terminal" in bundle.tool_names
    assert "read_file" in bundle.tool_names
    assert any("pytest" in command for command in bundle.command_hints)
    assert not any(message.role == "tool" and "secret1234567890" in message.content for message in bundle.messages)


def test_heuristic_digest_writes_workflow_memory_and_ledger_then_skips_duplicate(tmp_path):
    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    options = DigestOptions(hermes_home=hermes_home, digest_date=day, extractor="heuristic")
    first = run_digest(options)

    assert first["ok"] is True
    assert first["inserted"] == 1
    assert first["quality_counts"] == {"promote": 1}
    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, target, content, metadata FROM memories").fetchall()
        assert len(rows) == 1
        assert rows[0]["target"] == "ops"
        assert "工具链" in rows[0]["content"]
        assert "117 passed" not in rows[0]["content"]
        assert "结果摘要" not in rows[0]["content"]
        assert "secret1234567890" not in rows[0]["content"]
        metadata = json.loads(rows[0]["metadata"])
        assert metadata["memory_type"] == "workflow"
        assert metadata["verification"] == ["verification-evidence"]
        assert metadata["digest_quality"]["recommended_action"] == "promote"
        assert metadata["digest_quality"]["has_verification"] is True
        assert metadata["lifecycle"] == "candidate"
        assert metadata["automatic_admission"]["route"] == "experience_review"
        assert "terminal" in metadata["tools_used"]
        assert conn.execute("SELECT COUNT(*) FROM nightly_digest_runs").fetchone()[0] == 1
        run_metadata = json.loads(conn.execute("SELECT metadata FROM nightly_digest_runs").fetchone()[0])
        assert run_metadata["quality_counts"] == {"promote": 1}
        assert conn.execute("SELECT COUNT(*) FROM memory_digest_sources").fetchone()[0] == 1
    finally:
        conn.close()

    second = run_digest(options)
    assert second["inserted"] == 0
    assert second["skipped"] >= 1
    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        memory_id = conn.execute("SELECT id FROM memories").fetchone()[0]
        assert delete_rows(conn, [memory_id]) == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_digest_sources WHERE memory_id = ?", (memory_id,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_dry_run_does_not_write_digest_rows(tmp_path):
    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    result = run_digest(DigestOptions(hermes_home=hermes_home, digest_date=day, extractor="heuristic", dry_run=True))

    assert result["ok"] is True
    assert result["status"] == "dry_run"
    assert not (hermes_home / "scope-recall" / "memory.sqlite3").exists()


def test_llm_digest_timeout_falls_back_to_heuristic_and_records_degraded_ok(tmp_path, monkeypatch):

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    def fake_urlopen(request, *, timeout, allow_insecure=False):  # noqa: ARG001
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("scope_recall.nightly_llm.safe_urlopen", fake_urlopen)
    fake_digest_key = "fake" + "-digest-key"

    result = run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=day,
            extractor="llm",
            api_key=fake_digest_key,
            max_attempts=1,
            retry_delay=0,
        )
    )

    assert result["ok"] is True
    assert result["status"] == "ok_with_fallback"
    assert result["inserted"] == 1
    assert result["extractor_used"] == "heuristic-fallback"
    assert result["extractor_fallbacks"][0]["kind"] == "timeout"

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "ok_with_fallback"
        assert run["error"] is None
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_fallbacks"][0]["kind"] == "timeout"
    finally:
        conn.close()


def test_llm_empty_array_skips_heuristic_template_and_records_error(tmp_path, monkeypatch):
    import scope_recall.nightly_digest as nightly_digest

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    def fake_call_llm_with_retries(*args, **kwargs):  # noqa: ARG001
        return "[]"

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call_llm_with_retries)

    result = run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=day,
            extractor="llm",
            api_key="fake-digest-key",
            max_attempts=1,
            retry_delay=0,
        )
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"]
    assert result["inserted"] == 0
    assert result["extractor_used"] == "llm-degraded"
    assert result["extractor_fallbacks"][0]["kind"] == "llm_empty_skipped"

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "error"
        assert run["error"]
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_used"] == "llm-degraded"
        assert metadata["extractor_fallbacks"][0]["kind"] == "llm_empty_skipped"
    finally:
        conn.close()


def test_llm_bad_json_skips_heuristic_template_and_records_error(tmp_path, monkeypatch):
    import scope_recall.nightly_digest as nightly_digest

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    def fake_call_llm_with_retries(*args, **kwargs):  # noqa: ARG001
        return "not json at all"

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call_llm_with_retries)

    result = run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=day,
            extractor="llm",
            api_key="fake-digest-key",
            max_attempts=1,
            retry_delay=0,
        )
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"]
    assert result["inserted"] == 0
    assert result["extractor_used"] == "llm-degraded"
    assert result["extractor_fallbacks"][0]["kind"] == "llm_parse_skipped"

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "error"
        assert run["error"]
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_used"] == "llm-degraded"
        assert metadata["extractor_fallbacks"][0]["kind"] == "llm_parse_skipped"
    finally:
        conn.close()


def test_heuristic_digest_preserves_external_artifact_anchors(tmp_path):
    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(
        hermes_home / "state.db",
        day,
        content_suffix="上游申请 https://github.com/NousResearch/hermes-agent/issues/42864 标题 [Show & Tell/RFC] scope-recall standalone memory provider。",
    )

    result = run_digest(DigestOptions(hermes_home=hermes_home, digest_date=day, extractor="heuristic"))

    assert result["ok"] is True
    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT content, metadata FROM memories").fetchone()
        assert row is not None
        assert "Artifact anchors:" in row["content"]
        assert "NousResearch/hermes-agent#42864" in row["content"]
        metadata = json.loads(row["metadata"])
        assert metadata["artifacts"][0]["kind"] == "github_issue"
        assert metadata["artifacts"][0]["number"] == 42864
    finally:
        conn.close()

def test_llm_explicit_skip_after_candidate_keeps_previous_chunk_candidate(tmp_path, monkeypatch):
    import scope_recall.nightly_digest as nightly_digest

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day, content_suffix=" " + "补充材料 " * 80)

    calls = {"count": 0}

    def fake_call_llm_with_retries(*args, **kwargs):  # noqa: ARG001
        calls["count"] += 1
        visible_ids = [int(value) for value in re.findall(r"message_id=(\d+)", str(args[0]))]
        if calls["count"] == 1:
            assert visible_ids
            return json.dumps(
                [
                    {
                        "action": "ADD",
                        "evidence_message_ids": [visible_ids[0]],
                        "content": "scope-recall 多 chunk 审计流程：先保留第一段有效候选，后续 explicit skip 不应丢弃已有候选。",
                        "claim": {
                            "subject": "scope-recall 多 chunk 审计流程",
                            "predicate": "候选保留规则",
                            "value": "后续 explicit skip 不丢弃前一段有效候选",
                            "cardinality": "single",
                        },
                        "target": "ops",
                        "memory_type": "workflow",
                        "importance": 0.7,
                        "confidence": 0.8,
                        "entities": ["scope-recall"],
                        "tags": ["nightly-digest"],
                        "reason": "regression",
                    }
                ],
                ensure_ascii=False,
            )
        return json.dumps([{"action": "skip", "reason": "covered"}])

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call_llm_with_retries)

    result = run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=day,
            extractor="llm",
            api_key="fake-digest-key",
            max_attempts=1,
            retry_delay=0,
            chunk_chars=80,
            max_session_chars=4000,
        )
    )

    assert calls["count"] >= 2
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["inserted"] == 1
    assert result["extractor_fallbacks"] == []


def test_llm_explicit_skip_before_candidate_continues_to_next_chunk(tmp_path, monkeypatch):
    import scope_recall.nightly_digest as nightly_digest

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day, content_suffix=" " + "后续材料 " * 80)

    calls = {"count": 0}

    def fake_call_llm_with_retries(*args, **kwargs):  # noqa: ARG001
        calls["count"] += 1
        visible_ids = [int(value) for value in re.findall(r"message_id=(\d+)", str(args[0]))]
        if calls["count"] == 1:
            return json.dumps([{"action": "skip", "reason": "first chunk has no reusable content"}])
        if calls["count"] > 2:
            return json.dumps([{"action": "skip", "reason": "remaining continuation has no new candidate"}])
        assert visible_ids
        return json.dumps(
            [
                {
                    "action": "ADD",
                    "evidence_message_ids": [visible_ids[0]],
                    "content": "scope-recall 多 chunk 审计流程：第一个 chunk explicit skip 后，后续 chunk 的有效候选仍必须被解析写入。",
                    "claim": {
                        "subject": "scope-recall 多 chunk 审计流程",
                        "predicate": "候选保留规则",
                        "value": "前一段 explicit skip 不阻止后一段有效候选",
                        "cardinality": "single",
                    },
                    "target": "ops",
                    "memory_type": "workflow",
                    "importance": 0.7,
                    "confidence": 0.8,
                    "entities": ["scope-recall"],
                    "tags": ["nightly-digest"],
                    "reason": "regression",
                }
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call_llm_with_retries)

    result = run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=day,
            extractor="llm",
            api_key="fake-digest-key",
            max_attempts=1,
            retry_delay=0,
            chunk_chars=80,
            max_session_chars=4000,
        )
    )

    assert calls["count"] >= 2
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["inserted"] == 1
    assert result["candidates"] == 1
    assert result["extractor_used"] == "llm"
    assert result["extractor_fallbacks"] == []


def test_llm_empty_and_empty_heuristic_records_degraded_skip(tmp_path, monkeypatch):
    import scope_recall.nightly_digest as nightly_digest

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    def fake_call_llm_with_retries(*args, **kwargs):  # noqa: ARG001
        return "[]"

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call_llm_with_retries)
    monkeypatch.setattr(nightly_digest, "heuristic_candidates", lambda bundle: [])

    result = run_digest(
        DigestOptions(
            hermes_home=hermes_home,
            digest_date=day,
            extractor="llm",
            api_key="fake-digest-key",
            max_attempts=1,
            retry_delay=0,
        )
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"]
    assert result["candidates"] == 0
    assert result["inserted"] == 0
    assert result["extractor_used"] == "llm-degraded"
    assert result["extractor_fallbacks"][0]["kind"] == "llm_empty_skipped"

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "error"
        assert run["error"]
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_used"] == "llm-degraded"
        assert metadata["extractor_fallbacks"][0]["kind"] == "llm_empty_skipped"
    finally:
        conn.close()


class _FailingDigestVectorStore:
    def __init__(self) -> None:
        self.deleted_ids: list[list[str]] = []

    def delete_by_ids(self, ids: list[str]) -> None:
        self.deleted_ids.append(list(ids))
        raise RuntimeError("nightly vector delete failed token=secret123456789 /tmp/hermes-nightly")


class _FakeDigestVectorRuntime:
    def __init__(self) -> None:
        self._vector_store = _FailingDigestVectorStore()
        self._vector_ready = True
        self._vector_status = "ready"
        self._vector_message = ""


class _NeverInitializedDigestVectorRuntime:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._vector_store = None
        self._vector_enabled = True
        self._vector_ready = False
        self._vector_status = "degraded"
        self._vector_generation_id = ""
        self._vector_message = "embedder unavailable before companion initialization"

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn


def _duplicate_cleanup_scope() -> ScopeProfile:
    scope = RuntimeScope(platform="cli", user_id="local", agent_identity="default", agent_workspace="hermes")
    return ScopeProfile(
        scope=scope,
        scope_id="local-scope",
        shared_scope_id="shared-scope",
        accessible_scope_ids=["local-scope", "shared-scope"],
        writable_scope_ids=["shared-scope"],
    )


def _store_duplicate_cleanup_row(conn: sqlite3.Connection, *, memory_id: str) -> None:
    store_row(
        conn,
        memory_id=memory_id,
        scope_id="shared-scope",
        platform="cli",
        user_id="local",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        session_id="nightly-duplicate-cleanup",
        source="nightly-digest",
        target="memory",
        content="Nightly exact duplicate cleanup must fail closed when vector delete fails.",
        allow_duplicate=True,
        enqueue_vector_intent=False,
    )


def test_nightly_duplicate_cleanup_commits_hard_delete_audit():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_duplicate_cleanup_row(conn, memory_id="dupe-new")
    _store_duplicate_cleanup_row(conn, memory_id="dupe-old")

    deleted = cleanup_exact_duplicates(conn, _duplicate_cleanup_scope(), None)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id IN ('dupe-new', 'dupe-old')").fetchone()[0] == 1
    audit = conn.execute(
        "SELECT event_type, action, target_id FROM governance_audit_events WHERE event_type = 'nightly_duplicate_hard_delete'"
    ).fetchone()
    assert audit is not None
    assert audit["action"] == "hard_delete"
    assert audit["target_id"] in {"dupe-new", "dupe-old"}


def test_nightly_duplicate_cleanup_allows_degraded_never_initialized_vector_runtime():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_duplicate_cleanup_row(conn, memory_id="dupe-new")
    _store_duplicate_cleanup_row(conn, memory_id="dupe-old")
    vector_runtime = _NeverInitializedDigestVectorRuntime(conn)

    deleted = cleanup_exact_duplicates(conn, _duplicate_cleanup_scope(), vector_runtime)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id IN ('dupe-new', 'dupe-old')").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox").fetchone()[0] == 0


def test_nightly_duplicate_cleanup_uses_shared_replay_without_direct_vector_delete(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    bootstrap_legacy_generation(
        conn,
        identity=GenerationIdentity(
            backend="lancedb", provider="local-hash", model="hash-v1", dimensions=16
        ),
        row_count=0,
    )
    conn.commit()
    _store_duplicate_cleanup_row(conn, memory_id="dupe-new")
    _store_duplicate_cleanup_row(conn, memory_id="dupe-old")
    vector_runtime = _FakeDigestVectorRuntime()
    replay_calls: list[tuple[object, int]] = []

    def replay(runtime, *, limit):
        replay_calls.append((runtime, limit))
        return {"claimed": 1, "completed": 0, "failed": 1}

    monkeypatch.setattr(nightly_digest, "replay_vector_outbox", replay)

    deleted = cleanup_exact_duplicates(conn, _duplicate_cleanup_scope(), vector_runtime)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id IN ('dupe-new', 'dupe-old')").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM vector_outbox WHERE status = 'pending'").fetchone()[0] == 1
    assert replay_calls == [(vector_runtime, 1)]
    assert vector_runtime._vector_store.deleted_ids == []
    assert vector_runtime._vector_status == "needs_repair"
    assert vector_runtime._vector_message == "nightly duplicate vector outbox replay failed"
    assert "secret123456789" not in vector_runtime._vector_message
    assert "/tmp/hermes" not in vector_runtime._vector_message


@pytest.mark.parametrize(
    ("raw_insecure_opt_in", "expected_insecure_opt_in"),
    ((True, True), ("true", False)),
)
def test_collect_candidates_propagates_endpoint_opt_in_and_explicit_booleans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_insecure_opt_in: object,
    expected_insecure_opt_in: bool,
) -> None:
    captured: dict[str, object] = {}

    def fake_call(_prompt: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return '[{"action":"skip"}]'

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", fake_call)

    nightly_digest.collect_candidates(
        [_parse_test_bundle()],
        options=DigestOptions(
            hermes_home=tmp_path,
            digest_date=date(2026, 8, 5),
            extractor="llm",
            allow_heuristic_fallback=False,
        ),
        llm_config={
            "model": "test-model",
            "base_url": "http://model.internal:1234/v1",
            "api_key": "placeholder-only",
            "api_mode": "chat_completions",
            "append_v1": "false",
            "allow_insecure_endpoint": raw_insecure_opt_in,
        },
        existing_context=[],
    )

    assert captured["allow_insecure_endpoint"] is expected_insecure_opt_in
    assert captured["append_v1"] is False


def test_collect_candidates_endpoint_policy_error_never_uses_heuristic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scope_recall.nightly_llm as nightly_llm

    calls = {"count": 0}
    fallback_events: list[dict[str, object]] = []

    def blocked_retry(*args: object, **kwargs: object) -> str:  # noqa: ARG001
        calls["count"] += 1
        raise nightly_llm.NightlyDigestLLMError(
            "endpoint_policy after 1 attempt(s): blocked by endpoint policy",
            attempts=1,
            error_kind="endpoint_policy",
            retryable=False,
        )

    monkeypatch.setattr(nightly_digest, "_call_llm_with_retries", blocked_retry)

    with pytest.raises(RuntimeError):
        nightly_digest.collect_candidates(
            [_parse_test_bundle()],
            options=DigestOptions(
                hermes_home=tmp_path,
                digest_date=date(2026, 8, 5),
                extractor="llm",
                max_attempts=3,
                allow_heuristic_fallback=True,
            ),
            llm_config={
                "model": "test-model",
                "base_url": "https://api.example.invalid/v1",
                "api_key": "placeholder-only",
                "api_mode": "chat_completions",
            },
            existing_context=[],
            fallback_events=fallback_events,
        )

    assert calls["count"] == 1
    assert fallback_events == []
