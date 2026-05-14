import json

import pytest

from plugins.memory import load_memory_provider
from tools.memory_tool import MemoryStore, memory_tool


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
