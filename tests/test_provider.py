import json

import pytest

from plugins.memory import load_memory_provider
from tools.memory_tool import MemoryStore, memory_tool

from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id


@pytest.fixture
def provider(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None, "scope-recall plugin should load from $HERMES_HOME/plugins"
    plugin.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
    )
    yield plugin
    plugin.shutdown()


def test_scope_recall_plugin_loads_from_hermes_home_plugins():
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    assert plugin.name == "scope-recall"


def test_prefetch_uses_current_turn_query_not_previous_prefetch(provider):
    provider.sync_turn(
        "We deploy services with uv run and restart the gateway after model changes.",
        "Got it.",
    )
    provider.flush(timeout=2.0)

    provider.on_turn_start(1, "How do we deploy services after model changes?")
    first = provider.prefetch("How do we deploy services after model changes?")
    assert "uv run" in first.lower()

    provider.queue_prefetch("How do we deploy services after model changes?")
    provider.on_turn_start(2, "你好")
    assert provider.prefetch("你好") == ""

    provider.on_turn_start(3, "Where can I buy groceries tonight?")
    assert provider.prefetch("Where can I buy groceries tonight?") == ""


def test_builtin_memory_write_round_trips_into_current_turn_recall(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    payload = json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy prefers concise answers with direct problem-first reporting.",
            store=store,
        )
    )
    assert payload["success"] is True

    provider.on_turn_start(1, "What response style does Joy prefer?")
    result = provider.prefetch("What response style does Joy prefer?")
    assert "concise answers" in result.lower()
    assert "problem-first" in result.lower()


def test_short_or_greeting_query_is_gated(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise answers.", "target": "user"})
    )
    assert payload["stored"] is True

    provider.on_turn_start(1, "hi")
    assert provider.prefetch("hi") == ""

    provider.on_turn_start(2, "谢谢")
    assert provider.prefetch("谢谢") == ""


def test_dedup_prevents_repeat_injection_within_min_repeated(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "The deploy command is uv run app.", "target": "memory"})
    )
    assert payload["stored"] is True

    provider.on_turn_start(1, "What is the deploy command for this service?")
    first = provider.prefetch("What is the deploy command for this service?")
    assert "uv run app" in first.lower()

    provider.on_turn_start(2, "What is the deploy command for this service?")
    second = provider.prefetch("What is the deploy command for this service?")
    assert second == ""

    provider.on_turn_start(9, "What is the deploy command for this service?")
    third = provider.prefetch("What is the deploy command for this service?")
    assert "uv run app" in third.lower()


def test_scope_isolation_uses_user_and_profile(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="other-user",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does the user like?")
        assert p2.prefetch("What style does the user like?") == ""

        p1.on_turn_start(1, "What style does the user like?")
        assert "concise replies" in p1.prefetch("What style does the user like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_scope_isolation_includes_chat_id(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does Joy like?")
        assert p2.prefetch("What style does Joy like?") == ""

        p1.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p1.prefetch("What style does Joy like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_scope_isolation_includes_thread_id(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
        thread_id="topic-a",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
        thread_id="topic-b",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does Joy like?")
        assert p2.prefetch("What style does Joy like?") == ""

        p1.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p1.prefetch("What style does Joy like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_scope_isolation_prefers_gateway_session_key(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        gateway_session_key="telegram:group-1:topic-a",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        gateway_session_key="telegram:group-2:topic-a",
    )

    try:
        payload = json.loads(
            p1.handle_tool_call("scope_recall_store", {"content": "Joy likes concise replies.", "target": "user"})
        )
        assert payload["stored"] is True

        p2.on_turn_start(1, "What style does Joy like?")
        assert p2.prefetch("What style does Joy like?") == ""

        p1.on_turn_start(1, "What style does Joy like?")
        assert "concise replies" in p1.prefetch("What style does Joy like?").lower()
    finally:
        p1.shutdown()
        p2.shutdown()


def test_scope_id_serialization_cannot_collide_via_raw_delimiters():
    embedded_delimiter = RuntimeScope(
        platform="telegram",
        agent_workspace="hermes",
        agent_identity="yuheng",
        user_id="joy|chat:group-1",
    )
    structured_chat = RuntimeScope(
        platform="telegram",
        agent_workspace="hermes",
        agent_identity="yuheng",
        user_id="joy",
        chat_id="group-1",
    )

    assert build_scope_id(embedded_delimiter) != build_scope_id(structured_chat)


def test_budget_caps_number_of_lines(provider):
    for i in range(6):
        payload = json.loads(
            provider.handle_tool_call(
                "scope_recall_store",
                {"content": f"Deploy note {i}: deploy with uv run app and restart gateway step {i}.", "target": "memory"},
            )
        )
        assert payload["stored"] is True

    provider.on_turn_start(1, "How do we deploy the service and restart the gateway?")
    result = provider.prefetch("How do we deploy the service and restart the gateway?")
    bullet_lines = [line for line in result.splitlines() if line.startswith("- ")]
    assert 1 <= len(bullet_lines) <= 3


def test_search_tool_returns_ranked_results(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Use uv run app for deploys.", "target": "memory"})
    )
    assert payload["stored"] is True

    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_search",
            {"query": "What command do we use for deploys?", "limit": 3},
        )
    )
    assert payload["count"] >= 1
    assert payload["results"][0]["content"].lower().startswith("use uv run app")
    assert "base_score" in payload["results"][0]
    assert "recency_bonus" in payload["results"][0]


def test_replace_in_curated_memory_is_reflected_without_stale_old_entry(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    assert json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy likes concise replies.",
            store=store,
        )
    )["success"] is True
    assert json.loads(
        memory_tool(
            action="replace",
            target="user",
            old_text="Joy likes concise replies.",
            content="Joy likes concise but warm replies.",
            store=store,
        )
    )["success"] is True

    provider.on_turn_start(1, "What style does Joy like?")
    result = provider.prefetch("What style does Joy like?")
    assert "warm replies" in result.lower()
    assert "joy likes concise replies." not in result.lower()


def test_remove_from_curated_memory_is_reflected(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.load_from_disk()
    assert json.loads(
        memory_tool(
            action="add",
            target="user",
            content="Joy likes concise replies.",
            store=store,
        )
    )["success"] is True
    assert json.loads(
        memory_tool(
            action="remove",
            target="user",
            old_text="Joy likes concise replies.",
            store=store,
        )
    )["success"] is True

    provider.on_turn_start(1, "What style does Joy like?")
    assert provider.prefetch("What style does Joy like?") == ""


def test_on_memory_write_is_observational_noop(provider):
    before_stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    provider.on_memory_write("add", "memory", "Curated memory is read live, not mirrored into SQLite.")
    after_stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))

    assert after_stats["total_memories"] == before_stats["total_memories"]
    provider.on_turn_start(1, "Is curated memory mirrored into SQLite?")
    assert provider.prefetch("Is curated memory mirrored into SQLite?") == ""



def test_reads_builtin_curated_memory_files(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)
    (memories_dir / "USER.md").write_text(
        "Joy prefers concise answers with direct problem-first reporting.\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-curated",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        plugin.on_turn_start(1, "What response style does Joy prefer?")
        result = plugin.prefetch("What response style does Joy prefer?")
        assert "problem-first" in result.lower()
        stats = json.loads(plugin.handle_tool_call("scope_recall_stats", {}))
        assert stats["curated_memories"] >= 1
    finally:
        plugin.shutdown()


def test_subagent_context_cannot_expose_or_use_tools(tmp_path):
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-sub",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="subagent",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        assert plugin.get_tool_schemas() == []
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "subagent should not write this", "target": "memory"},
            )
        )
        assert payload["error"]
    finally:
        plugin.shutdown()


def test_hybrid_recall_uses_vector_companion_for_semantic_match(provider):
    payload = json.loads(
        provider.handle_tool_call(
            "scope_recall_store",
            {"content": "Deploy services with uv run app.", "target": "memory"},
        )
    )
    assert payload["stored"] is True
    provider.flush(timeout=5.0)

    provider.on_turn_start(1, "What is our rollout command for production releases?")
    result = provider.prefetch("What is our rollout command for production releases?")
    assert "uv run app" in result.lower()


def test_stats_report_vector_state(provider):
    payload = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert payload["provider"] == "scope-recall"
    assert payload["vector"]["enabled"] is True
    assert payload["vector"]["backend"] == "lancedb"
    assert payload["vector"]["ready"] is True


def test_lexical_recall_matches_response_style_aliases(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-lexical-style",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Joy likes warm concise replies.", "target": "user"},
            )
        )
        assert payload["stored"] is True
        plugin.flush(timeout=5.0)

        plugin.on_turn_start(1, "What response style should I use?")
        result = plugin.prefetch("What response style should I use?")
        assert "warm concise replies" in result.lower()
    finally:
        plugin.shutdown()



def test_lexical_recall_matches_prod_rollout_aliases(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-lexical-deploy",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Production rollout uses uv run app.", "target": "memory"},
            )
        )
        assert payload["stored"] is True
        plugin.flush(timeout=5.0)

        plugin.on_turn_start(1, "How do we deploy prod?")
        result = plugin.prefetch("How do we deploy prod?")
        assert "uv run app" in result.lower()
    finally:
        plugin.shutdown()



def test_recency_aware_ranking_prefers_newer_memory_for_current_queries(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-current",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        old_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Current production deploy uses uv run app.", "target": "memory"},
            )
        )
        new_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Production command is uv run service now.", "target": "memory"},
            )
        )
        conn = plugin._require_conn()
        with plugin._lock:
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", old_payload["id"]),
            )
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-05-01T00:00:00+00:00", new_payload["id"]),
            )
            conn.commit()

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {"query": "What is the current production deploy command?", "limit": 3},
            )
        )
        assert payload["results"][0]["content"] == "Production command is uv run service now."
    finally:
        plugin.shutdown()



def test_recency_aware_ranking_keeps_clearer_match_without_freshness_hint(tmp_path):
    config_path = tmp_path / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "retrieval": {"mode": "lexical", "min_score": 0.18},
                "vector": {"enabled": False},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        "session-recency-nonfresh",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
    )
    try:
        old_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Current production deploy uses uv run app.", "target": "memory"},
            )
        )
        new_payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_store",
                {"content": "Production command is uv run service now.", "target": "memory"},
            )
        )
        conn = plugin._require_conn()
        with plugin._lock:
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00+00:00", old_payload["id"]),
            )
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                ("2026-05-01T00:00:00+00:00", new_payload["id"]),
            )
            conn.commit()

        payload = json.loads(
            plugin.handle_tool_call(
                "scope_recall_search",
                {"query": "How do we deploy prod?", "limit": 3},
            )
        )
        assert payload["results"][0]["content"] == "Current production deploy uses uv run app."
    finally:
        plugin.shutdown()



def test_legacy_tool_aliases_still_work(provider):
    stored = json.loads(provider.handle_tool_call("lancepro_store", {"content": "Use uv run app.", "target": "memory"}))
    assert stored["stored"] is True

    searched = json.loads(provider.handle_tool_call("lancepro_search", {"query": "uv run app", "limit": 3}))
    assert searched["count"] >= 1

    stats = json.loads(provider.handle_tool_call("lancepro_stats", {}))
    assert stats["provider"] == "scope-recall"

def test_store_skips_exact_duplicate_content_in_same_scope(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise direct replies.", "target": "user"})
    )
    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "  Joy prefers concise direct replies.  ", "target": "user"})
    )

    assert first["stored"] is True
    assert second["stored"] is False
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 1


def test_auto_capture_filters_system_maintenance_prompts(provider):
    noisy_prompt = "Review the conversation above and update the skill library. Be ACTIVE — most sessions produce at least one skill update."
    provider.sync_turn(noisy_prompt, "OK")
    provider.flush(timeout=2.0)

    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 0


def test_forget_tool_removes_matching_sqlite_and_vector_rows(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Temporary deploy note should be removed.", "target": "memory"})
    )
    assert payload["stored"] is True
    provider.flush(timeout=5.0)

    result = json.loads(provider.handle_tool_call("scope_recall_forget", {"query": "Temporary deploy note", "limit": 5}))
    assert result["deleted"] == 1
    assert payload["id"] in result["ids"]

    provider.on_turn_start(1, "Temporary deploy note")
    assert provider.prefetch("Temporary deploy note") == ""


def test_update_tool_replaces_memory_and_old_fts_is_removed(provider):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Production deploy uses uv run old.", "target": "memory"})
    )
    updated = json.loads(
        provider.handle_tool_call(
            "scope_recall_update",
            {"id": payload["id"], "content": "Production deploy uses uv run new.", "target": "ops"},
        )
    )

    assert updated["updated"] is True
    assert updated["id"] == payload["id"]
    old_results = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "uv run old", "limit": 3}))
    assert all("uv run old" not in item["content"].lower() for item in old_results["results"])
    new_results = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "uv run new", "limit": 3}))
    assert new_results["results"][0]["content"] == "Production deploy uses uv run new."
    assert new_results["results"][0]["target"] == "ops"


def test_dedupe_tool_dry_run_and_apply_collapses_existing_duplicates(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Duplicate note for cleanup.", "target": "memory"})
    )
    assert first["stored"] is True
    # Simulate legacy duplicate row bypassing the new write-time dedupe guard.
    with provider._lock:
        provider._store_now(content="Duplicate note for cleanup.", source="legacy-import", target="memory", session_id="legacy", semantic_merge=False)

    dry = json.loads(provider.handle_tool_call("scope_recall_dedupe", {"dry_run": True}))
    assert dry["duplicate_groups"] == 1
    assert dry["duplicates"] == 1

    applied = json.loads(provider.handle_tool_call("scope_recall_dedupe", {"dry_run": False}))
    assert applied["deleted"] == 1
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 1


def test_dedupe_scope_only_false_collapses_duplicates_across_scopes(tmp_path):
    p1 = load_memory_provider("scope-recall")
    p2 = load_memory_provider("scope-recall")
    assert p1 is not None and p2 is not None

    p1.initialize(
        "session-a",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-1",
    )
    p2.initialize(
        "session-b",
        hermes_home=str(tmp_path),
        platform="telegram",
        agent_context="primary",
        agent_identity="yuheng",
        agent_workspace="hermes",
        user_id="joy",
        chat_id="group-2",
    )

    try:
        for plugin in (p1, p2):
            plugin._store_now(
                content="Duplicate note across scopes.",
                source="legacy-import",
                target="memory",
                session_id="legacy",
                semantic_merge=False,
                allow_duplicate=True,
            )
            plugin._store_now(
                content="Duplicate note across scopes.",
                source="legacy-import",
                target="memory",
                session_id="legacy",
                semantic_merge=False,
                allow_duplicate=True,
            )

        dry = json.loads(p1.handle_tool_call("scope_recall_dedupe", {"dry_run": True, "scope_only": False}))
        assert dry["duplicate_groups"] == 2
        assert dry["duplicates"] == 2

        applied = json.loads(p1.handle_tool_call("scope_recall_dedupe", {"dry_run": False, "scope_only": False}))
        assert applied["deleted"] == 2
        stats = json.loads(p1.handle_tool_call("scope_recall_stats", {}))
        assert stats["total_memories"] == 2
        assert stats["scope_memories"] == 1
    finally:
        p1.shutdown()
        p2.shutdown()


def test_vector_search_error_degrades_to_lexical_and_marks_needs_repair(provider, monkeypatch):
    payload = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Lexical fallback remembers gateway restart command.", "target": "ops"})
    )
    assert payload["stored"] is True

    def broken_search(*args, **kwargs):
        raise RuntimeError("missing lance data file")

    monkeypatch.setattr(provider._vector_store, "search", broken_search)
    result = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "gateway restart command", "limit": 3}))

    assert result["count"] >= 1
    assert "gateway restart command" in result["results"][0]["content"]
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["vector"]["status"] == "needs_repair"


def test_repair_tool_rebuilds_vector_companion(provider):
    provider.handle_tool_call("scope_recall_store", {"content": "Repair command should rebuild vector index.", "target": "ops"})
    provider.flush(timeout=5.0)
    provider._vector_ready = False
    provider._vector_status = "needs_repair"
    provider._vector_message = "forced test damage"

    payload = json.loads(provider.handle_tool_call("scope_recall_repair", {}))

    assert payload["repaired"] is True
    assert payload["vector"]["status"] == "ready"
    assert payload["vector"]["row_count"] == 1

def test_smart_extract_turn_creates_preference_and_fact_memories(provider):
    provider.sync_turn(
        "Joy prefers playful concise replies. The production deploy command is uv run app.",
        "Understood.",
    )
    provider.flush(timeout=5.0)

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Joy reply preference", "limit": 5}))
    assert any(item["target"] == "user" and "playful concise replies" in item["content"] for item in payload["results"])

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "production deploy command", "limit": 5}))
    assert any(item["target"] == "ops" and "uv run app" in item["content"] for item in payload["results"])


def test_semantic_near_duplicate_store_merges_existing_memory(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"})
    )
    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy likes brief responses.", "target": "user"})
    )

    assert first["stored"] is True
    assert second["stored"] is False
    assert second["merged"] is True
    assert second["id"] == first["id"]

    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "Joy response style", "limit": 5}))
    assert payload["count"] == 1
    assert "brief responses" in payload["results"][0]["content"]


def test_semantic_merge_does_not_hide_conflicting_memory(provider):
    first = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"})
    )
    second = json.loads(
        provider.handle_tool_call("scope_recall_store", {"content": "Joy no longer prefers concise replies.", "target": "user"})
    )

    assert first["stored"] is True
    assert second["stored"] is True
    assert second.get("merged") is not True
    stats = json.loads(provider.handle_tool_call("scope_recall_stats", {}))
    assert stats["total_memories"] == 2


def test_merge_tool_combines_memory_content_and_deletes_source(provider):
    a = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"}))
    b = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy likes warm answers.", "target": "user"}))

    result = json.loads(
        provider.handle_tool_call(
            "scope_recall_merge",
            {"target_id": a["id"], "source_ids": [b["id"]], "content": "Joy prefers concise and warm replies.", "target": "user"},
        )
    )

    assert result["merged"] is True
    assert result["target_id"] == a["id"]
    assert result["deleted"] == 1
    payload = json.loads(provider.handle_tool_call("scope_recall_search", {"query": "warm replies", "limit": 5}))
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == a["id"]
    assert "concise and warm" in payload["results"][0]["content"]


def test_export_tool_returns_jsonl_records(provider):
    stored = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Exportable deploy memory uses uv run app.", "target": "ops"}))

    payload = json.loads(provider.handle_tool_call("scope_recall_export", {"format": "jsonl"}))

    assert payload["format"] == "jsonl"
    assert payload["count"] >= 1
    lines = [line for line in payload["data"].splitlines() if line.strip()]
    assert lines
    parsed = [json.loads(line) for line in lines]
    assert any(row["id"] == stored["id"] and row["content"] == "Exportable deploy memory uses uv run app." for row in parsed)


def test_governance_tool_reports_tiers_and_decay_candidates(provider):
    old = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Old temporary note for decay review.", "target": "general"}))
    durable = json.loads(provider.handle_tool_call("scope_recall_store", {"content": "Joy prefers concise replies.", "target": "user"}))
    conn = provider._require_conn()
    with provider._lock:
        conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", old["id"]))
        conn.commit()

    payload = json.loads(provider.handle_tool_call("scope_recall_govern", {"dry_run": True}))

    assert payload["dry_run"] is True
    assert payload["total"] == 2
    assert payload["tiers"]["core"] >= 1
    assert payload["tiers"]["archive"] >= 1
    assert old["id"] in payload["decay_candidates"]
    assert durable["id"] not in payload["decay_candidates"]
