from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from scope_recall.models import RuntimeScope
import scope_recall.journal as journal_module
from scope_recall.scope import build_scope_id, build_shared_scope_id, accessible_scope_ids
from scope_recall.sql_store import delete_rows, ensure_schema, store_row
from scope_recall.journal import (
    JournalDigestCandidate,
    JournalEntry,
    append_journal_entry,
    apply_journal_candidates,
    ensure_journal_schema,
    heuristic_journal_candidates,
    load_unprocessed_journal_entries,
    run_journal_digest,
    _insert_journal_entry,
)


def _scope() -> RuntimeScope:
    return RuntimeScope(
        platform="telegram",
        user_id="8176453077",
        chat_id="dm",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _open_memory_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_journal_schema(conn)
    return conn


def test_journal_entries_are_provenance_not_durable_memory(tmp_path):
    conn = _open_memory_db(tmp_path / "memory.sqlite3")
    scope = _scope()

    user_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="session-a",
        turn_number=1,
        role="user",
        content="Joy 希望 scope-recall 不要逐消息写长期记忆，而是先写临时日记。",
    )
    assistant_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="session-a",
        turn_number=1,
        role="assistant",
        content="我会改成 journal-first，并在后台 digest 后写入高质量记忆。",
    )

    assert user_id
    assert assistant_id
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    entries = load_unprocessed_journal_entries(conn, scope_ids=accessible_scope_ids(scope), limit=10)
    assert [entry.role for entry in entries] == ["user", "assistant"]
    assert all(entry.processed_run_id == "" for entry in entries)


def test_journal_entry_preserves_long_turns_as_chunks(tmp_path):
    conn = _open_memory_db(tmp_path / "memory.sqlite3")
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)
    marker = "TAIL-MARKER-DO-NOT-LOSE"
    long_text = "长任务说明：" + ("需要保留隐性经验、失败路径和验收证据。" * 260) + marker

    first_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        session_id="long-session",
        turn_number=1,
        role="user",
        content=long_text,
    )

    rows = conn.execute("SELECT id, content, metadata FROM journal_entries ORDER BY id").fetchall()
    assert first_id == rows[0]["id"]
    assert len(rows) >= 2
    reconstructed = "".join(row["content"] for row in rows)
    assert marker in reconstructed
    metadata = [json.loads(row["metadata"]) for row in rows]
    assert {item["original_content_hash"] for item in metadata} == {hashlib.sha256(long_text.encode("utf-8")).hexdigest()}
    assert [item["chunk_index"] for item in metadata] == list(range(1, len(rows) + 1))


def test_heuristic_digest_splits_unrelated_topics_inside_one_session():
    entries = [
        JournalEntry(1, "s", "shared", "same-session", 1, "user", "修 scope-recall release gate：wheel manifest、check.release.py、版本号一致。", "2026-06-12T00:00:01+00:00"),
        JournalEntry(2, "s", "shared", "same-session", 2, "assistant", "release gate 已验证，pytest 通过。", "2026-06-12T00:00:02+00:00"),
        JournalEntry(3, "s", "shared", "same-session", 3, "user", "另外处理客户 Tailscale 远程机器授权边界，网络改动必须先保证不会断联。", "2026-06-12T00:10:01+00:00"),
        JournalEntry(4, "s", "shared", "same-session", 4, "assistant", "远程客户机只读盘点优先，防火墙/路由变更需要 Joy 授权和回滚。", "2026-06-12T00:10:02+00:00"),
    ]

    candidates = heuristic_journal_candidates(entries)

    assert len(candidates) >= 2
    assert any("check.release.py" in candidate.content or "release gate" in candidate.content for candidate in candidates)
    assert any("Tailscale" in candidate.content or "远程" in candidate.content for candidate in candidates)
    assert all(set(candidate.entry_ids) != {1, 2, 3, 4} for candidate in candidates)


def test_journal_digest_groups_related_turns_and_writes_evidence_links(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    append_journal_entry(
        conn,
        scope=scope,
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        session_id="session-memory-design",
        turn_number=1,
        role="user",
        content="不要每句话都写 SQL，scope-recall 要使用临时日记，然后周期性高质量提取。",
    )
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=scope_id,
        shared_scope_id=shared_scope_id,
        session_id="session-memory-design",
        turn_number=2,
        role="assistant",
        content="方案确定：journal-first、background digest、merge/upsert、向量库只索引高质量记忆。",
    )

    result = run_journal_digest(
        hermes_home=hermes_home,
        extractor="heuristic",
        scope=scope,
        interval_label="test",
        limit_entries=50,
        dry_run=False,
    )

    assert result["ok"] is True
    assert result["processed_entries"] == 2
    assert result["inserted"] == 1
    assert result["updated"] == 0

    memory = conn.execute("SELECT id, source, target, content, metadata FROM memories").fetchone()
    assert memory is not None
    assert memory["source"] == "journal-digest"
    assert memory["target"] in {"memory", "project", "ops"}
    assert "journal-first" in memory["content"] or "临时日记" in memory["content"]
    metadata = json.loads(memory["metadata"])
    assert metadata["memory_type"] in {"decision", "workflow", "summary"}
    assert metadata["journal_run_id"] == result["run_id"]

    evidence_count = conn.execute("SELECT COUNT(*) FROM memory_journal_sources WHERE memory_id = ?", (memory["id"],)).fetchone()[0]
    assert evidence_count == 2
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id = ?", (result["run_id"],)).fetchone()[0] == 2
    assert delete_rows(conn, [memory["id"]]) == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_journal_sources WHERE memory_id = ?", (memory["id"],)).fetchone()[0] == 0


def test_journal_digest_merge_upserts_same_topic_instead_of_scattering_rows(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="s1", turn_number=1, role="user", content="scope-recall 要 journal-first，不要逐条消息入库。")
    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="s1", turn_number=2, role="assistant", content="已确定 journal-first 和后台 digest。")
    first = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50)

    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="s1", turn_number=3, role="user", content="同一个任务还要加 merge/upsert，别把同任务拆成很多条记忆。")
    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="s1", turn_number=4, role="assistant", content="同一主题会更新已有 journal-digest 记忆，并追加 evidence。")
    second = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50)

    assert first["inserted"] == 1
    assert second["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    content = conn.execute("SELECT content FROM memories").fetchone()[0]
    assert "journal-first" in content
    assert "merge" in content.lower() or "合并" in content or "upsert" in content.lower()
    assert conn.execute("SELECT COUNT(*) FROM memory_journal_sources").fetchone()[0] == 4


def test_journal_digest_does_not_overmerge_same_session_unrelated_followup(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="same-long-session", turn_number=1, role="user", content="scope-recall release gate 要修 wheel manifest、check.release.py 和版本一致。")
    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="same-long-session", turn_number=2, role="assistant", content="release gate 已验证 wheel 文件清单和 scripts/journal-digest.py。")
    first = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50)

    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="same-long-session", turn_number=3, role="user", content="另一个主题：召回排序要加入 RRF、BM25 和 entity distance，避免单一向量信号把旧主题顶上来。")
    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="same-long-session", turn_number=4, role="assistant", content="retrieval fusion 已验证 rrf_min_signals、BM25 归一化、实体距离 rerank。")
    second = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50)

    assert first["inserted"] == 1
    assert second["inserted"] == 1
    assert second["updated"] == 0
    contents = [row["content"] for row in conn.execute("SELECT content FROM memories ORDER BY created_at").fetchall()]
    assert len(contents) == 2
    assert any("check.release.py" in content for content in contents)
    assert any("RRF" in content or "BM25" in content for content in contents)


def test_journal_digest_dry_run_does_not_mutate_existing_database(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="dry-run-session",
        turn_number=1,
        role="user",
        content="scope-recall dry-run 必须不推进 journal watermark。",
    )
    before = conn.execute("SELECT processed_run_id FROM journal_entries").fetchone()[0]

    result = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50, dry_run=True)

    after = conn.execute("SELECT processed_run_id FROM journal_entries").fetchone()[0]
    assert result["status"] == "dry_run"
    assert before == after == ""
    assert conn.execute("SELECT COUNT(*) FROM journal_digest_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_journal_digest_does_not_promote_assistant_only_legacy_rows(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    _insert_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="assistant-only-legacy",
        turn_number=1,
        role="assistant",
        text="I will remember this deployment procedure for next time.",
    )

    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("assistant-only journal rows must not be sent to LLM digest")

    monkeypatch.setattr(journal_module, "call_llm", fail_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, extractor="llm", scope=scope, interval_label="test", limit_entries=50)

    assert result["ok"] is True
    assert result["processed_entries"] == 1
    assert result["candidates"] == 0
    assert result["inserted"] == 0
    assert result["skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id != ''").fetchone()[0] == 1


def test_journal_digest_without_scope_processes_each_shared_scope(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope_a = _scope()
    scope_b = RuntimeScope(
        platform="telegram",
        user_id="joy-b",
        chat_id="dm-b",
        thread_id="",
        gateway_session_key="",
        agent_identity="default",
        agent_workspace="hermes",
        agent_context="primary",
    )
    append_journal_entry(conn, scope=scope_a, scope_id=build_scope_id(scope_a), shared_scope_id=build_shared_scope_id(scope_a), session_id="s-a", turn_number=1, role="user", content="scope-recall user A journal-first digest workflow。")
    append_journal_entry(conn, scope=scope_b, scope_id=build_scope_id(scope_b), shared_scope_id=build_shared_scope_id(scope_b), session_id="s-b", turn_number=1, role="user", content="scope-recall user B journal-first digest workflow。")

    result = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=None, interval_label="test", limit_entries=50)

    assert result["ok"] is True
    assert result["processed_entries"] == 2
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id != ''").fetchone()[0] == 2


def test_journal_digest_does_not_overmerge_distinct_sessions(tmp_path):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    scope_id = build_scope_id(scope)
    shared_scope_id = build_shared_scope_id(scope)

    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="release-ci", turn_number=1, role="user", content="scope-recall 1.0.12 发布前要修复 CI：check.release.py、wheel 文件清单、README 版本号必须一致。")
    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="release-ci", turn_number=2, role="assistant", content="已完成发布门禁调整：scripts/check.release.py 会验证 journal.py 和 scripts/journal-digest.py 被打入 wheel。")
    first = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50)

    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="retrieval-quality", turn_number=1, role="user", content="scope-recall 召回排序要加入 RRF、BM25 和 entity distance，避免单一向量信号把旧主题顶上来。")
    append_journal_entry(conn, scope=scope, scope_id=scope_id, shared_scope_id=shared_scope_id, session_id="retrieval-quality", turn_number=2, role="assistant", content="已实现 retrieval fusion：rrf_min_signals=2，BM25 分数归一化，实体距离只作为低权重补充。")
    second = run_journal_digest(hermes_home=hermes_home, extractor="heuristic", scope=scope, interval_label="test", limit_entries=50)

    assert first["inserted"] == 1
    assert second["inserted"] == 1
    assert second["updated"] == 0
    contents = [row["content"] for row in conn.execute("SELECT content FROM memories ORDER BY created_at").fetchall()]
    assert len(contents) == 2
    assert any("check.release.py" in content for content in contents)
    assert any("RRF" in content or "BM25" in content for content in contents)


def test_journal_digest_records_rejections_without_advancing_watermark_for_filtered_candidates(tmp_path):
    conn = _open_memory_db(tmp_path / "memory.sqlite3")
    scope = _scope()
    candidate = JournalDigestCandidate(
        content="This rejected candidate is long enough to audit but uses an unsupported journal target.",
        target="unsupported",
        entry_ids=[123],
        session_ids=["s-reject"],
    )

    result = apply_journal_candidates(conn, None, scope, run_id="run-reject", candidates=[candidate], dry_run=False)

    assert result["counts"]["skipped"] == 1
    assert result["processed_entry_ids"] == []
    row = conn.execute("SELECT reason, candidate FROM journal_rejections WHERE journal_entry_id = 123").fetchone()
    assert row is not None
    assert row["reason"] == "candidate filtered"


def test_journal_duplicate_store_row_links_evidence_without_rejection(tmp_path, monkeypatch):
    conn = _open_memory_db(tmp_path / "memory.sqlite3")
    scope = _scope()
    shared_scope_id = build_shared_scope_id(scope)
    content = "scope-recall journal duplicate evidence must preserve memory_journal_sources instead of losing provenance."
    existing_id, _, _, inserted = store_row(
        conn,
        memory_id="existing-memory",
        scope_id=shared_scope_id,
        platform=scope.platform,
        user_id=scope.user_id,
        chat_id=scope.chat_id,
        thread_id=scope.thread_id,
        gateway_session_key=scope.gateway_session_key,
        agent_identity=scope.agent_identity,
        agent_workspace=scope.agent_workspace,
        session_id="seed",
        source="manual",
        target="memory",
        content=content,
        metadata=json.dumps({"tags": ["existing"]}),
    )
    assert inserted is True

    import scope_recall.journal as journal_module

    monkeypatch.setattr(journal_module, "_find_match", lambda *args, **kwargs: ("", "", 0.0))
    candidate = JournalDigestCandidate(
        content=content,
        target="memory",
        memory_type="procedure",
        entities=["scope-recall"],
        tags=["journal-digest", "duplicate-provenance"],
        entry_ids=[321, 322],
        session_ids=["dup-session"],
    )

    result = apply_journal_candidates(conn, None, scope, run_id="run-dup", candidates=[candidate], dry_run=False)

    assert result["counts"].get("updated", 0) == 1
    assert result["processed_entry_ids"] == [321, 322]
    assert conn.execute("SELECT COUNT(*) FROM memory_journal_sources WHERE memory_id = ?", (existing_id,)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0] == 0


def test_journal_digest_llm_extractor_uses_llm_candidates_and_records_actual_extractor(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    (hermes_home / ".env").write_text("SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="llm-session",
        turn_number=1,
        role="user",
        content="Joy 要求 scope-recall 的 journal digest 用真正 LLM 蒸馏隐性经验，不要启发式摘要。",
    )

    import scope_recall.journal as journal_module

    def fake_call_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float, api_mode: str = "chat_completions", endpoint: str = "", append_v1: bool = True) -> str:
        assert "隐性经验" in prompt
        assert api_key == "test-key"
        return json.dumps([
            {
                "action": "insert",
                "content": "scope-recall journal digest must use LLM extraction for hidden lessons and keep heuristic only as explicit fallback.",
                "target": "memory",
                "memory_type": "procedure",
                "importance": 0.9,
                "confidence": 0.86,
                "entities": ["scope-recall", "journal digest"],
                "tags": ["llm-digest", "hidden-lessons"],
                "reason": "LLM extracted reusable hidden experience.",
            }
        ])

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, extractor="llm", scope=scope, interval_label="test", limit_entries=50)

    assert result["ok"] is True
    assert result["extractor_requested"] == "llm"
    assert result["extractor_used"] == "llm"
    memory = conn.execute("SELECT content FROM memories").fetchone()[0]
    assert "hidden lessons" in memory
    assert "Journal digest" not in memory


def test_journal_digest_default_extractor_is_llm_not_heuristic(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    (hermes_home / ".env").write_text("SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="default-llm-session",
        turn_number=1,
        role="user",
        content="默认 journal digest CLI 必须走 LLM，而不是 heuristic fallback。",
    )

    import scope_recall.journal as journal_module

    assert journal_module.build_arg_parser().parse_args([]).extractor == "llm"

    def fake_call_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float, api_mode: str = "chat_completions", endpoint: str = "", append_v1: bool = True) -> str:
        return json.dumps([
            {
                "action": "insert",
                "content": "journal digest default extractor is LLM-first, not heuristic.",
                "target": "memory",
                "memory_type": "decision",
                "entities": ["scope-recall"],
                "tags": ["default-extractor"],
            }
        ])

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, scope=scope, interval_label="test", limit_entries=50)

    assert result["extractor_requested"] == "llm"
    assert result["extractor_used"] == "llm"
    assert conn.execute("SELECT content FROM memories").fetchone()[0] == "journal digest default extractor is LLM-first, not heuristic."


def test_journal_capture_omits_inline_data_url_payload(tmp_path):
    conn = _open_memory_db(tmp_path / "memory.sqlite3")
    scope = _scope()
    blob = "A" * 12000

    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="image-session",
        turn_number=1,
        role="user",
        content=f"请看截图并审计 scope-recall。 data:image/jpeg;base64,{blob}",
    )

    assert entry_id
    rows = conn.execute("SELECT content FROM journal_entries ORDER BY id").fetchall()
    assert len(rows) == 1
    assert "inline image/jpeg data omitted" in rows[0]["content"]
    assert "data:image" not in rows[0]["content"]
    assert blob[:100] not in rows[0]["content"]


def test_legacy_base64_journal_chunk_is_processed_without_calling_llm(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    entry_id = _insert_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="legacy-image-chunk",
        turn_number=1,
        role="user",
        text="A" * 1800,
        metadata={"chunk_index": 2, "chunk_count": 73, "chunking": "bounded-journal-content"},
    )

    import scope_recall.journal as journal_module

    def fail_if_called(*args, **kwargs):
        raise AssertionError("base64-only journal chunks must not call LLM")

    monkeypatch.setattr(journal_module, "call_llm", fail_if_called)

    result = run_journal_digest(hermes_home=hermes_home, extractor="llm", scope=scope, interval_label="test", limit_entries=50)

    assert result["processed_entries"] == 1
    assert result["candidates"] == 0
    row = conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["processed_run_id"] == result["run_id"]


def test_mixed_journal_digest_does_not_send_tool_content_to_llm(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="mixed-session",
        turn_number=1,
        role="user",
        content="scope-recall mixed journal digest should preserve user intent.",
    )
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="mixed-session",
        turn_number=2,
        role="tool",
        content="RAW_TOOL_DIFF_SHOULD_NOT_ENTER_PROMPT " * 80,
        metadata={"tool_name": "patch"},
    )

    import scope_recall.journal as journal_module

    def fake_call_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float, api_mode: str = "chat_completions", endpoint: str = "", append_v1: bool = True) -> str:
        assert "scope-recall mixed journal digest" in prompt
        assert "RAW_TOOL_DIFF_SHOULD_NOT_ENTER_PROMPT" not in prompt
        return "[]"

    monkeypatch.setattr(journal_module, "call_llm", fake_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, extractor="llm", scope=scope, interval_label="test", limit_entries=50)

    total_entries = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    assert result["processed_entries"] == total_entries
    assert result["candidates"] == 0


def test_tool_only_journal_digest_marks_entries_processed_without_calling_llm(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="tool-only-session",
        turn_number=1,
        role="tool",
        content="Tool execution trace with a large diff that should not be sent to an LLM.",
    )

    import scope_recall.journal as journal_module

    def fail_if_called(*args, **kwargs):
        raise AssertionError("tool-only journal bundles must not call LLM")

    monkeypatch.setattr(journal_module, "call_llm", fail_if_called)

    result = run_journal_digest(hermes_home=hermes_home, extractor="llm", scope=scope, interval_label="test", limit_entries=50)

    assert result["processed_entries"] == 1
    assert result["candidates"] == 0
    assert result["skipped"] == 1
    row = conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["processed_run_id"] == result["run_id"]
    rejection = conn.execute("SELECT reason FROM journal_rejections WHERE journal_entry_id = ?", (entry_id,)).fetchone()
    assert rejection["reason"] == "no durable memory candidate"


def test_journal_digest_marks_reviewed_entries_without_candidates_processed(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm"}}), encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="no-candidate-session",
        turn_number=1,
        role="tool",
        content="Tool trace that was reviewed by the digest but does not deserve durable memory.",
    )

    import scope_recall.journal as journal_module

    monkeypatch.setattr(journal_module, "llm_journal_candidates", lambda *args, **kwargs: [])

    result = run_journal_digest(hermes_home=hermes_home, extractor="llm", scope=scope, interval_label="test", limit_entries=50)

    assert result["processed_entries"] == 1
    assert result["candidates"] == 0
    row = conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["processed_run_id"] == result["run_id"]
    rejection = conn.execute("SELECT reason FROM journal_rejections WHERE journal_entry_id = ?", (entry_id,)).fetchone()
    assert rejection["reason"] == "no durable memory candidate"


def test_llm_digest_transient_failure_retries_before_quarantine(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm", "llm_max_attempts": 3, "llm_retry_delay": 0}}),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text("SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="llm-retry-session",
        turn_number=1,
        role="user",
        content="LLM digest 瞬时超时后应重试，成功后写入高质量候选，而不是立刻 quarantine。",
    )

    import scope_recall.journal as journal_module

    attempts = {"count": 0}

    def flaky_call_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float, api_mode: str = "chat_completions", endpoint: str = "", append_v1: bool = True) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError("simulated timeout")
        return json.dumps(
            [
                {
                    "action": "insert",
                    "content": "Scope Recall journal digest retries transient LLM timeout before quarantine, preserving valuable entries after recovery.",
                    "target": "project",
                    "memory_type": "procedure",
                    "importance": 0.7,
                    "confidence": 0.8,
                    "entities": ["scope-recall"],
                    "tags": ["journal-digest", "retry"],
                    "reason": "retry recovered",
                }
            ]
        )

    monkeypatch.setattr(journal_module, "call_llm", flaky_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, scope=scope, interval_label="test", limit_entries=50)

    assert attempts["count"] == 2
    assert result["extractor_used"] == "llm"
    assert result["inserted"] == 1
    assert result["skipped"] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_rejections").fetchone()[0] == 0


def test_llm_digest_failure_retries_then_quarantines_without_fallback_memory(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm", "llm_max_attempts": 2, "llm_retry_delay": 0}}),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text("SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="llm-failure-session",
        turn_number=1,
        role="user",
        content="LLM digest 持续超时时不得静默降级 heuristic 写长期记忆，但重试耗尽后可以 quarantine 避免 backlog 卡住。",
    )

    import scope_recall.journal as journal_module

    attempts = {"count": 0}

    def failing_call_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float, api_mode: str = "chat_completions", endpoint: str = "", append_v1: bool = True) -> str:
        attempts["count"] += 1
        raise TimeoutError("simulated llm timeout")

    monkeypatch.setattr(journal_module, "call_llm", failing_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, scope=scope, interval_label="test", limit_entries=50)

    assert attempts["count"] == 2
    assert result["ok"] is True
    assert result["processed_entries"] == 1
    assert result["inserted"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert result["extractor_used"] == "llm-quarantine"
    row = conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["processed_run_id"] == result["run_id"]
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    rejection = conn.execute("SELECT reason, candidate FROM journal_rejections WHERE journal_entry_id = ?", (entry_id,)).fetchone()
    assert rejection["reason"] == "retry-exhausted:timeout"
    assert "timeout after 2 attempt" in rejection["candidate"]
    run = conn.execute("SELECT status, extractor, error, metadata FROM journal_digest_runs WHERE id = ?", (result["run_id"],)).fetchone()
    assert run["status"] == "ok"
    assert run["extractor"] == "llm-quarantine"
    assert run["error"] is None
    metadata = json.loads(run["metadata"])
    assert metadata["quarantine_counts"] == {"retry_exhausted": 1}
    assert metadata["extractor_errors"][0]["kind"] == "timeout"
    assert metadata["extractor_errors"][0]["retryable"] is True


def test_llm_digest_auth_failure_quarantines_without_retrying(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps({"vector": {"enabled": False}, "journal": {"extractor": "llm", "llm_max_attempts": 3, "llm_retry_delay": 0}}),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text("SCOPE_RECALL_DIGEST_API_KEY=test-key\n", encoding="utf-8")
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="llm-auth-failure-session",
        turn_number=1,
        role="user",
        content="LLM digest 遇到 403 权限错误时不应盲目重试三次，应分类后隔离并保留审计信息。",
    )

    import scope_recall.journal as journal_module

    attempts = {"count": 0}

    raw_key = "sk-" + "a" * 30

    def forbidden_call_llm(prompt: str, *, model: str, base_url: str, api_key: str, timeout: float, api_mode: str = "chat_completions", endpoint: str = "", append_v1: bool = True) -> str:
        attempts["count"] += 1
        raise RuntimeError(f"HTTP 403 Forbidden: Incorrect API key provided: {raw_key}")

    monkeypatch.setattr(journal_module, "call_llm", forbidden_call_llm)

    result = run_journal_digest(hermes_home=hermes_home, scope=scope, interval_label="test", limit_entries=50)

    assert attempts["count"] == 1
    assert result["extractor_used"] == "llm-quarantine"
    row = conn.execute("SELECT processed_run_id FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["processed_run_id"] == result["run_id"]
    rejection = conn.execute("SELECT reason, candidate FROM journal_rejections WHERE journal_entry_id = ?", (entry_id,)).fetchone()
    assert rejection["reason"] == "dead-letter:auth"
    assert "auth after 1 attempt" in rejection["candidate"]
    assert raw_key not in rejection["candidate"]
    assert "[REDACTED_SECRET]" in rejection["candidate"]
    run = conn.execute("SELECT metadata FROM journal_digest_runs WHERE id = ?", (result["run_id"],)).fetchone()
    metadata = json.loads(run["metadata"])
    assert metadata["quarantine_counts"] == {"dead_letter": 1}
    assert metadata["extractor_errors"][0]["kind"] == "auth"
    assert metadata["extractor_errors"][0]["retryable"] is False
    assert raw_key not in metadata["extractor_errors"][0]["message"]
    assert "[REDACTED_SECRET]" in metadata["extractor_errors"][0]["message"]


def test_journal_digest_dynamic_limit_scales_up_when_backlog_is_large(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    storage = hermes_home / "scope-recall"
    storage.mkdir(parents=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "journal": {
                    "extractor": "llm",
                    "max_entries_per_digest": 2,
                    "dynamic_max_entries_enabled": True,
                    "dynamic_backlog_threshold": 3,
                    "max_entries_per_digest_ceiling": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    conn = _open_memory_db(storage / "memory.sqlite3")
    scope = _scope()
    for index in range(5):
        append_journal_entry(
            conn,
            scope=scope,
            scope_id=build_scope_id(scope),
            shared_scope_id=build_shared_scope_id(scope),
            session_id="dynamic-limit-session",
            turn_number=index + 1,
            role="user",
            content=f"Dynamic backlog smoke valuable user event {index}: release hygiene should process more entries when backlog is large.",
        )

    monkeypatch.setattr(journal_module, "_collect_journal_candidates", lambda *args, **kwargs: ([], "llm", None))

    result = run_journal_digest(hermes_home=hermes_home, scope=scope, interval_label="test")

    assert result["limit_entries"] == 5
    assert result["processed_entries"] == 5
    assert result["skipped"] == 5
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE processed_run_id = ''").fetchone()[0] == 0


def test_journal_capture_keeps_long_english_text_that_only_looks_base64ish(tmp_path):
    conn = _open_memory_db(tmp_path / "memory.sqlite3")
    scope = _scope()
    long_ascii_words = " ".join(["A" * 8, "B" * 7, "C" * 9, "D" * 6] * 80)

    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id="long-english-session",
        turn_number=1,
        role="user",
        content=long_ascii_words,
    )

    assert entry_id > 0
    row_count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    assert row_count >= 1
