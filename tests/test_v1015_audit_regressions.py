from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import threading
import urllib.error
from pathlib import Path

import pytest

from scope_recall import capture_llm, journal, nightly_digest
from scope_recall.journal import JournalDigestCandidate, JournalEntry, apply_journal_candidates, ensure_journal_schema, heuristic_journal_candidates
from scope_recall.memory_ops import delete_memories
from scope_recall.models import RuntimeScope
from scope_recall.nightly_digest import DigestCandidate, ScopeProfile, apply_candidates, ensure_digest_schema, infer_scope
from scope_recall.scope import accessible_scope_ids, build_scope_id, build_shared_scope_id, normalize_scope_identity
from scope_recall.sql_store import ensure_schema, store_row


def _json_response(content: str = "[]"):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self) -> bytes:
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    return Response()


def test_capture_llm_uses_explicit_endpoint_without_appending_v1(monkeypatch):
    seen_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        seen_urls.append(request.full_url)
        return _json_response("[]")

    monkeypatch.setattr(capture_llm.urllib.request, "urlopen", fake_urlopen)

    capture_llm.extract_capture_candidates(
        "user asks something durable",
        "assistant answers something useful",
        {
            "capture_llm": {
                "enabled": True,
                "api_key": "test-key",
                "model": "capture-model",
                "base_url": "https://wrong.example/root",
                "endpoint": "https://ark.example/api/coding/v3/chat/completions",
            }
        },
    )

    assert seen_urls == ["https://ark.example/api/coding/v3/chat/completions"]


def test_capture_llm_respects_append_v1_false_for_provider_roots(monkeypatch):
    seen_urls: list[str] = []

    def fake_urlopen(request, timeout=None):
        seen_urls.append(request.full_url)
        return _json_response("[]")

    monkeypatch.setattr(capture_llm.urllib.request, "urlopen", fake_urlopen)

    capture_llm.extract_capture_candidates(
        "user asks something durable",
        "assistant answers something useful",
        {
            "capture_llm": {
                "enabled": True,
                "api_key": "test-key",
                "model": "capture-model",
                "base_url": "https://ark.example/api/coding/v3",
                "append_v1": False,
            }
        },
    )

    assert seen_urls == ["https://ark.example/api/coding/v3/chat/completions"]


def test_codex_responses_http_errors_are_redacted(monkeypatch):
    leaked_token = "super" + "secretvalue1234567890"
    leaked_bearer = "abcdef" + "ghijklmnopqrstuvwxyz"
    api_key = "sk-" + "abc" + "c123"

    def fake_urlopen(request, timeout=None):
        payload = {"error": f"token={leaked_token} Authorization: Bearer {leaked_bearer}"}
        body = json.dumps(payload).encode("utf-8")
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(body))

    monkeypatch.setattr(nightly_digest.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc_info:
        nightly_digest._call_codex_responses_llm(
            "prompt",
            model="model",
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            timeout=1,
        )

    message = str(exc_info.value)
    assert leaked_token not in message
    assert leaked_bearer not in message
    assert api_key not in message
    assert "[REDACTED]" in message


def test_codex_responses_sse_errors_are_redacted():
    leaked_password = "raw" + "password123456789"
    leaked_token = "token" + "secret123456789"
    body = "data: " + json.dumps({"type": "error", "message": f"password={leaked_password} token={leaked_token}"}) + "\n\n"

    with pytest.raises(RuntimeError) as exc_info:
        nightly_digest._decode_responses_body(body)

    message = str(exc_info.value)
    assert leaked_password not in message
    assert leaked_token not in message
    assert "[REDACTED]" in message


def test_heuristic_journal_digest_does_not_promote_tool_content():
    entries = [
        JournalEntry(
            1,
            "local-scope",
            "shared-scope",
            "session-tool-only",
            1,
            "tool",
            "Tool output includes should-not-become-memory-marker and command stdout",
            "2026-06-13T00:00:00+00:00",
        )
    ]

    assert heuristic_journal_candidates(entries) == []


def test_nightly_infer_scope_accepts_explicit_fallback_platform_for_empty_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    profile = infer_scope(
        conn,
        fallback_platform="cli",
        fallback_user_id="local",
        runtime_config={
            "identity": {
                "cross_platform_shared_scope": True,
                "cli_user_id_fallback": "local",
                "user_aliases": {"cli:local": "joy"},
            }
        },
    )

    assert profile.scope.platform == "cli"
    assert profile.scope.user_id == "local"
    assert "canonical_user:3:joy" in profile.shared_scope_id


class _FakeProvider:
    def __init__(self, conn: sqlite3.Connection, *, accessible: list[str], writable: list[str]) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._accessible_scope_ids = accessible
        self._writable_scope_ids = writable
        self._vector_store = None

    def _require_conn(self) -> sqlite3.Connection:
        return self._conn


def test_legacy_shared_scope_alias_is_not_writable_for_deletes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    config = {
        "identity": {
            "cross_platform_shared_scope": True,
            "cli_user_id_fallback": "local",
            "user_aliases": {"telegram:8176453077": "joy", "cli:local": "joy"},
        }
    }
    cli_scope = normalize_scope_identity(
        RuntimeScope(platform="cli", user_id="", agent_identity="default", agent_workspace="hermes"),
        config,
    )
    legacy_telegram_scope = RuntimeScope(platform="telegram", user_id="8176453077", agent_identity="default", agent_workspace="hermes")
    legacy_shared = build_shared_scope_id(legacy_telegram_scope)
    canonical_local = build_scope_id(cli_scope, config)
    canonical_shared = build_shared_scope_id(cli_scope, config)
    store_row(
        conn,
        memory_id="legacy-row",
        scope_id=legacy_shared,
        platform="telegram",
        user_id="8176453077",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        session_id="legacy-session",
        source="manual",
        target="memory",
        content="Legacy durable memory that should remain read-only through alias.",
        metadata="{}",
    )
    provider = _FakeProvider(
        conn,
        accessible=accessible_scope_ids(cli_scope, config),
        writable=[canonical_local, canonical_shared],
    )

    deleted = delete_memories(provider, ["legacy-row"])

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id = 'legacy-row'").fetchone()[0] == 1


def test_missing_writable_scope_list_fails_closed_for_deletes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="accessible-only-row",
        scope_id="legacy-readable-scope",
        platform="telegram",
        user_id="8176453077",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        session_id="legacy-session",
        source="manual",
        target="memory",
        content="Accessible-only row must not be deleted when writable scope state is missing.",
        metadata="{}",
    )

    class ProviderWithoutWritableScopes:
        _vector_store = None

        def __init__(self) -> None:
            self._conn = conn
            self._lock = threading.RLock()
            self._accessible_scope_ids = ["legacy-readable-scope"]

        def _require_conn(self) -> sqlite3.Connection:
            return self._conn

    deleted = delete_memories(ProviderWithoutWritableScopes(), ["accessible-only-row"])

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id = 'accessible-only-row'").fetchone()[0] == 1


def test_nightly_digest_does_not_update_read_only_legacy_alias_rows(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_digest_schema(conn)
    legacy_scope_id = build_shared_scope_id(RuntimeScope(platform="telegram", user_id="8176453077", agent_identity="default", agent_workspace="hermes"))
    canonical_scope = RuntimeScope(platform="cli", user_id="local", agent_identity="default", agent_workspace="hermes")
    scope_profile = ScopeProfile(
        scope=canonical_scope,
        scope_id=build_scope_id(canonical_scope),
        shared_scope_id="canonical-shared-scope",
        accessible_scope_ids=[build_scope_id(canonical_scope), "canonical-shared-scope", legacy_scope_id],
        writable_scope_ids=[build_scope_id(canonical_scope), "canonical-shared-scope"],
    )
    legacy_content = "Atlas pipeline deployment workflow uses Rust workers and release evidence."
    store_row(
        conn,
        memory_id="legacy-update-row",
        scope_id=legacy_scope_id,
        platform="telegram",
        user_id="8176453077",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        session_id="legacy-session",
        source="nightly-digest",
        target="memory",
        content=legacy_content,
        metadata="{}",
    )
    captured: list[dict[str, str]] = []

    monkeypatch.setattr(
        nightly_digest,
        "find_match",
        lambda _conn, _scope, _candidate: (
            "legacy-update-row",
            legacy_content,
            0.60,
        ),
    )
    monkeypatch.setattr(nightly_digest, "upsert_vector_record", lambda _runtime, **kwargs: captured.append(kwargs))

    result = apply_candidates(
        conn,
        object(),
        scope_profile,
        run_id="run-vector-scope",
        candidates=[
            DigestCandidate(
                content="Atlas pipeline deployment workflow uses Rust workers, release evidence, and rollback notes.",
                target="memory",
                memory_type="workflow",
                session_id="session-new",
                message_ids=[1, 2],
            )
        ],
        dry_run=False,
        runtime_config={},
    )

    assert result["counts"].get("inserted") == 1
    assert result["counts"].get("updated", 0) == 0
    legacy_row = conn.execute("SELECT content FROM memories WHERE id = 'legacy-update-row'").fetchone()
    assert legacy_row["content"] == legacy_content
    inserted_row = conn.execute("SELECT scope_id FROM memories WHERE id != 'legacy-update-row'").fetchone()
    assert inserted_row["scope_id"] == "canonical-shared-scope"
    assert captured and captured[0]["scope_id"] == "canonical-shared-scope"


def test_journal_digest_does_not_update_read_only_legacy_alias_rows(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    config = {
        "identity": {
            "cross_platform_shared_scope": True,
            "cli_user_id_fallback": "local",
            "user_aliases": {"telegram:8176453077": "joy", "cli:local": "joy"},
        }
    }
    cli_scope = normalize_scope_identity(
        RuntimeScope(platform="cli", user_id="", agent_identity="default", agent_workspace="hermes"),
        config,
    )
    legacy_scope_id = build_shared_scope_id(RuntimeScope(platform="telegram", user_id="8176453077", agent_identity="default", agent_workspace="hermes"))
    legacy_content = "Journal digest workflow stores durable notes with release evidence and rollback checks."
    store_row(
        conn,
        memory_id="legacy-journal-row",
        scope_id=legacy_scope_id,
        platform="telegram",
        user_id="8176453077",
        chat_id="",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        session_id="legacy-session",
        source="journal-digest",
        target="memory",
        content=legacy_content,
        metadata="{}",
    )
    captured: list[dict[str, str]] = []
    monkeypatch.setattr(journal, "_find_match", lambda _conn, _scope_ids, _candidate: ("legacy-journal-row", legacy_content, 0.60))
    monkeypatch.setattr(journal, "upsert_vector_record", lambda _runtime, **kwargs: captured.append(kwargs))

    result = apply_journal_candidates(
        conn,
        object(),
        cli_scope,
        run_id="journal-readonly-alias",
        candidates=[
            JournalDigestCandidate(
                content="Journal digest workflow stores durable notes with release evidence, rollback checks, and audit gates.",
                target="memory",
                entry_ids=[1],
                session_ids=["session-new"],
            )
        ],
        dry_run=False,
        runtime_config=config,
    )

    assert result["counts"].get("inserted") == 1
    assert result["counts"].get("updated", 0) == 0
    legacy_row = conn.execute("SELECT content FROM memories WHERE id = 'legacy-journal-row'").fetchone()
    assert legacy_row["content"] == legacy_content
    inserted_row = conn.execute("SELECT scope_id FROM memories WHERE id != 'legacy-journal-row'").fetchone()
    assert "canonical_user:3:joy" in inserted_row["scope_id"]
    assert captured and "canonical_user:3:joy" in captured[0]["scope_id"]


def test_release_secret_scan_reports_locations_without_secret_text(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "check_release_for_test",
        Path(__file__).resolve().parents[1] / "scripts" / "check.release.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    leaked_value = "super" + "secretvalue1234567890"
    (tmp_path / "leaky.py").write_text(f'token="{leaked_value}"\n', encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)

    findings = module.scan_tree()

    rendered = "\n".join(findings["secrets"])
    assert "leaky.py" in rendered
    assert "token" in rendered
    assert leaked_value not in rendered
