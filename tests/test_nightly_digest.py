"""Tests for nightly digest collection, candidate application, fallback, and run status.

They make scheduled summarization observable rather than an opaque cron side effect."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
    load_session_bundles,
    redact_sensitive,
    resolve_llm_config,
    run_digest,
)
from scope_recall.models import RuntimeScope
from scope_recall.sql_store import delete_rows, ensure_schema, store_row


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


def test_llm_candidate_parser_extracts_json_array_from_fenced_text_with_preamble():
    raw = """Here is the JSON:\n```json\n[{\"action\": \"create\", \"content\": \"Stable reusable workflow: when release rollback fails, validate backup before deleting current plugin and verify rollback receipt paths.\", \"target\": \"ops\", \"memory_type\": \"workflow\", \"importance\": \"high\", \"confidence\": \"high\", \"entities\": [\"rollback\"], \"tags\": [\"workflow\"], \"reason\": \"Reusable rollback safety workflow\"}]\n```\n"""

    candidates, status = _parse_llm_candidates_with_status(raw, bundle=_parse_test_bundle())

    assert status == "parsed"
    assert len(candidates) == 1
    assert candidates[0].target == "ops"
    assert candidates[0].memory_type == "workflow"
    assert candidates[0].importance >= 0.55
    assert candidates[0].confidence >= 0.65


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
            ("session-task", "telegram", "8176453077", "deepseek-v4-pro", "scope-recall live validation", _ts(day, 9)),
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
            ("session-progress", "telegram", "8176453077", "gpt-5.5", "停服维护进度更新 #10", _ts(day, 9)),
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

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    import scope_recall.nightly_digest as nightly_digest

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)
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

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    import scope_recall.nightly_digest as nightly_digest

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)

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

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    import scope_recall.nightly_digest as nightly_digest

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)

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

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    import scope_recall.nightly_digest as nightly_digest

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)

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


def test_call_llm_chat_completions_http_error_mentions_endpoint_without_secret(monkeypatch):
    import io
    from email.message import Message

    fake_secret = "sk-" + "a" * 28

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            hdrs=Message(),
            fp=io.BytesIO(f"provider error api_key={fake_secret}".encode("utf-8")),
        )

    import scope_recall.nightly_digest as nightly_digest

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)

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

    assert "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions" in message
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
        assert "secret1234567890" not in rows[0]["content"]
        metadata = json.loads(rows[0]["metadata"])
        assert metadata["memory_type"] == "workflow"
        assert metadata["digest_quality"]["recommended_action"] == "promote"
        assert metadata["digest_quality"]["has_verification"] is True
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
    import scope_recall.nightly_digest as nightly_digest

    day = date(2026, 6, 1)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _write_config(hermes_home)
    _create_state_db(hermes_home / "state.db", day)

    def fake_urlopen(request, timeout):  # noqa: ARG001
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)
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


def test_llm_empty_array_falls_back_and_records_degraded_ok(tmp_path, monkeypatch):
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

    assert result["ok"] is True
    assert result["status"] == "ok_with_fallback"
    assert result["inserted"] == 1
    assert result["extractor_used"] == "heuristic-fallback"
    assert result["extractor_fallbacks"][0]["kind"] == "llm_empty"

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "ok_with_fallback"
        assert run["error"] is None
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_used"] == "heuristic-fallback"
        assert metadata["extractor_fallbacks"][0]["kind"] == "llm_empty"
    finally:
        conn.close()


def test_llm_bad_json_falls_back_and_records_degraded_ok(tmp_path, monkeypatch):
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

    assert result["ok"] is True
    assert result["status"] == "ok_with_fallback"
    assert result["inserted"] == 1
    assert result["extractor_used"] == "heuristic-fallback"
    assert result["extractor_fallbacks"][0]["kind"] == "llm_parse"

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "ok_with_fallback"
        assert run["error"] is None
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_used"] == "heuristic-fallback"
        assert metadata["extractor_fallbacks"][0]["kind"] == "llm_parse"
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
        if calls["count"] == 1:
            return json.dumps(
                [
                    {
                        "action": "insert",
                        "content": "scope-recall 多 chunk 审计流程：先保留第一段有效候选，后续 explicit skip 不应丢弃已有候选。",
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
        if calls["count"] == 1:
            return json.dumps([{"action": "skip", "reason": "first chunk has no reusable content"}])
        return json.dumps(
            [
                {
                    "action": "insert",
                    "content": "scope-recall 多 chunk 审计流程：第一个 chunk explicit skip 后，后续 chunk 的有效候选仍必须被解析写入。",
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


def test_llm_empty_and_empty_heuristic_records_error(tmp_path, monkeypatch):
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
    assert result["candidates"] == 0
    assert result["extractor_fallbacks"][0]["kind"] == "llm_empty_no_candidates"
    assert "no candidates" in result["error"]

    conn = sqlite3.connect(hermes_home / "scope-recall" / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT status, error, metadata FROM nightly_digest_runs").fetchone()
        assert run["status"] == "error"
        assert "no candidates" in run["error"]
        metadata = json.loads(run["metadata"])
        assert metadata["extractor_fallbacks"][0]["kind"] == "llm_empty_no_candidates"
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
    )


def test_nightly_duplicate_cleanup_keeps_sql_truth_when_vector_delete_fails():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    _store_duplicate_cleanup_row(conn, memory_id="dupe-new")
    _store_duplicate_cleanup_row(conn, memory_id="dupe-old")
    vector_runtime = _FakeDigestVectorRuntime()

    deleted = cleanup_exact_duplicates(conn, _duplicate_cleanup_scope(), vector_runtime)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id IN ('dupe-new', 'dupe-old')").fetchone()[0] == 2
    assert vector_runtime._vector_status == "needs_repair"
    assert "[REDACTED_SECRET]" in vector_runtime._vector_message
    assert "[REDACTED_PATH]" in vector_runtime._vector_message
    assert "secret123456789" not in vector_runtime._vector_message
    assert "/tmp/hermes" not in vector_runtime._vector_message
