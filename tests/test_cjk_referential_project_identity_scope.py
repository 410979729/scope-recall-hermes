"""Public-store regressions for unquoted CJK project-identity clauses.

A natural “who wrote which project” question must not invent the descriptive
clause as a hard project name after public store expansion, but a named actor
must still be checked against the item text. Quoted names, Project prefixes,
claim subjects, simple CJK names, and other-user SQL scope stay enforced.
Each test owns its Provider home.
"""

from __future__ import annotations

import json

from plugins.memory import load_memory_provider

from scope_recall.graph import extract_entities
from scope_recall.models import RecallItem
from scope_recall.recall import RecallService

OWNER = "开阳"
WRONG_ACTOR = "玄玑"
TOPIC = "短片"
TITLE = "北风把灯塔吹灭了"
PRIOR = "摇光"
FILM_CONTENT = (
    f"{TOPIC}项目《{TITLE}》（short-film）自 2026-03-18 起由{OWNER}（Kaiyang）"
    f"全权拥有：原属{PRIOR}（Yaoguang），经 A2A 交接（task "
    "short-film-handoff-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb，Alice 授权），"
    "已全量校验（12 文件 / 3 目录 / 1959 字节 / 逐文件 SHA-256 一致）"
    "后接收于 [REDACTED_PATH]"
)
NATURAL_QUERY = f"{OWNER}写{TOPIC}的项目叫什么？"
PARAPHRASES = (
    NATURAL_QUERY,
    f"{OWNER}做{TOPIC}的项目叫什么？",
    f"那个写{TOPIC}的项目叫什么？",
    f"{OWNER}负责{TOPIC}的项目是什么名字？",
    f"{OWNER}写的{TOPIC}叫什么？",
)
VERB_NAME = "写意"
VERB_NAME_CONTENT = f"`{VERB_NAME}`项目目前的生产端口是10443。"
PERSON_CONTENT = "`小明`喜欢在工作时喝绿茶。"
ZEPHYR_CONTENT = (
    "Project Zephyr rollback runbook uses systemctl restart zephyr-worker "
    "after queue drain."
)
ATLAS_CONTENT = "Project Atlas production deploy command is uv run atlas-server."


def _write_config(home) -> None:
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps(
            {
                "vector": {"enabled": False},
                "relation_extraction_enabled": False,
                "retrieval": {
                    "mode": "lexical",
                    "min_score": 0.18,
                    "candidate_pool": 12,
                    "top_k": 5,
                    "entity_scope_filter_enabled": True,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _provider(tmp_path, *, user_id: str = "operator", session_id: str = "session-cjk-identity"):
    _write_config(tmp_path)
    plugin = load_memory_provider("scope-recall")
    assert plugin is not None
    plugin.initialize(
        session_id,
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
        agent_identity="kaiyang",
        agent_workspace="hermes",
        user_id=user_id,
        chat_id="dm",
    )
    return plugin


def _route(plugin, tool_name: str, args: dict):
    return json.loads(plugin.route_tool(tool_name, args))


def _store(plugin, content: str, target: str = "project") -> dict:
    receipt = _route(
        plugin,
        "scope_recall_store",
        {"content": content, "target": target, "session_id": "session-cjk-identity"},
    )
    assert receipt.get("stored") is True, receipt
    assert receipt.get("id"), receipt
    return receipt


def _search(plugin, query: str) -> dict:
    return _route(
        plugin,
        "scope_recall_search",
        {"query": query, "limit": 5, "include_trace": True},
    )


def _search_ids(payload: dict) -> list[str]:
    return [
        str(item.get("id") or "")
        for item in payload.get("results") or []
        if isinstance(item, dict)
    ]


def _inspect(plugin, memory_id: str) -> dict:
    return _route(plugin, "scope_recall_inspect", {"id": memory_id})


def _memory_payload(inspected: dict) -> dict:
    memory = inspected.get("memory")
    return memory if isinstance(memory, dict) else inspected


def _persisted_entities(inspected: dict) -> list[str]:
    memory = _memory_payload(inspected)
    metadata = memory.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    raw = metadata.get("entities") if isinstance(metadata, dict) else []
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _recall_item(memory_id: str, content: str, metadata: dict) -> RecallItem:
    return RecallItem(
        id=memory_id,
        content=content,
        summary=str(metadata.get("summary") or content),
        source=str(metadata.get("source") or "tool-store"),
        target="project",
        score=0.8,
        updated_at="2026-03-18T00:00:00+00:00",
        metadata=metadata,
    )


def test_unquoted_identity_clause_is_not_a_named_scope() -> None:
    service = object.__new__(RecallService)

    assert service._explicit_query_scope_entities(NATURAL_QUERY) == {OWNER}
    assert service._explicit_query_scope_entities(
        f"{WRONG_ACTOR}写{TOPIC}的项目叫什么？"
    ) == {WRONG_ACTOR}
    assert service._explicit_query_scope_entities(f"那个写{TOPIC}的项目叫什么？") == set()
    assert service._explicit_query_scope_entities(f"负责{TOPIC}的项目叫什么？") == set()
    assert service._explicit_query_scope_entities(f"{OWNER}的项目叫什么？") == {OWNER}
    assert service._explicit_query_scope_entities("请告诉我星河目前API 地址") == {"星河"}
    assert service._explicit_query_scope_entities("我想知道云舟现在的生产端口") == {"云舟"}
    assert service._explicit_query_scope_entities("小明的偏好是什么") == {"小明"}
    assert service._explicit_query_scope_entities(f"{VERB_NAME}现在的端口是什么") == {
        VERB_NAME
    }
    assert VERB_NAME in service._explicit_query_scope_entities(
        f"`{VERB_NAME}`现在的生产端口"
    )
    assert service._explicit_query_scope_entities(f"`{OWNER}写{TOPIC}`的进度") == {
        f"{OWNER}写{TOPIC}"
    }


def test_public_store_natural_question_matches_exact_title(tmp_path) -> None:
    plugin = _provider(tmp_path)
    try:
        stored = _store(plugin, FILM_CONTENT)
        inspected = _inspect(plugin, stored["id"])
        memory = _memory_payload(inspected)
        metadata = memory.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        entities = _persisted_entities(inspected)
        expected = extract_entities(FILM_CONTENT, target="project")

        assert entities == expected, entities
        assert len(entities) > 3
        assert OWNER not in entities
        assert "kaiyang" in {item.casefold() for item in entities}
        assert f"{OWNER}写{TOPIC}" not in entities

        service = plugin._recall_service
        item = _recall_item(stored["id"], str(memory.get("content") or FILM_CONTENT), metadata)
        ordinary_owner = f"{OWNER}的项目叫什么？"
        wrong_owner = f"{WRONG_ACTOR}写{TOPIC}的项目叫什么？"
        assert WRONG_ACTOR not in str(memory.get("content") or FILM_CONTENT)
        assert service._entity_scope_mismatch(NATURAL_QUERY, item, metadata) is False
        assert service._entity_scope_mismatch(ordinary_owner, item, metadata) is False
        assert service._entity_scope_mismatch(wrong_owner, item, metadata) is True
        assert service._entity_scope_mismatch(TITLE, item, metadata) is False

        natural = _search(plugin, NATURAL_QUERY)
        title = _search(plugin, TITLE)
        ordinary = _search(plugin, ordinary_owner)
        assert stored["id"] in _search_ids(natural), natural
        assert stored["id"] in _search_ids(title), title
        assert stored["id"] in _search_ids(ordinary), ordinary
        assert natural["funnel_trace"]["filters"]["entity_scope_mismatch"] == 0
        assert title["funnel_trace"]["filters"]["entity_scope_mismatch"] == 0
        assert ordinary["funnel_trace"]["filters"]["entity_scope_mismatch"] == 0
        assert natural["funnel_trace"]["stages"]["lexical"]["count"] >= 1
    finally:
        plugin.shutdown()


def test_same_activity_form_keeps_corroborated_actor_and_rejects_absent_actor(
    tmp_path,
) -> None:
    plugin = _provider(tmp_path)
    try:
        stored = _store(plugin, FILM_CONTENT)
        inspected = _inspect(plugin, stored["id"])
        memory = _memory_payload(inspected)
        content = str(memory.get("content") or FILM_CONTENT)
        entities = _persisted_entities(inspected)
        expected = extract_entities(FILM_CONTENT, target="project")
        wrong_query = f"{WRONG_ACTOR}写{TOPIC}的项目叫什么？"
        ordinary_query = f"{OWNER}的项目叫什么？"

        assert entities == expected, entities
        assert len(entities) > 3
        assert OWNER not in entities
        assert WRONG_ACTOR not in entities
        assert OWNER in content
        assert WRONG_ACTOR not in content
        assert f"{OWNER}写{TOPIC}" not in entities

        correct = _search(plugin, NATURAL_QUERY)
        wrong = _search(plugin, wrong_query)
        ordinary = _search(plugin, ordinary_query)
        title = _search(plugin, TITLE)

        assert stored["id"] in _search_ids(correct), correct
        assert correct.get("count") == 1
        assert correct["funnel_trace"]["filters"]["entity_scope_mismatch"] == 0
        assert stored["id"] not in _search_ids(wrong), wrong
        assert wrong.get("count") == 0
        assert wrong["funnel_trace"]["filters"]["entity_scope_mismatch"] >= 1
        assert stored["id"] in _search_ids(ordinary), ordinary
        assert ordinary.get("count") == 1
        assert ordinary["funnel_trace"]["filters"]["entity_scope_mismatch"] == 0
        assert stored["id"] in _search_ids(title), title
        assert title.get("count") == 1
    finally:
        plugin.shutdown()


def test_referential_paraphrases_keep_the_public_store_row(tmp_path) -> None:
    plugin = _provider(tmp_path)
    try:
        stored = _store(plugin, FILM_CONTENT)
        for query in PARAPHRASES:
            payload = _search(plugin, query)
            assert stored["id"] in _search_ids(payload), (query, payload)
            assert payload["funnel_trace"]["filters"]["entity_scope_mismatch"] == 0
    finally:
        plugin.shutdown()


def test_foreign_user_cannot_read_the_public_store_row(tmp_path) -> None:
    owner = _provider(tmp_path, user_id="operator", session_id="session-owner")
    other = _provider(tmp_path, user_id="other-operator", session_id="session-other")
    try:
        stored = _store(owner, FILM_CONTENT)
        payload = _search(other, NATURAL_QUERY)
        assert stored["id"] not in _search_ids(payload), payload
        assert payload.get("count", 0) == 0
    finally:
        other.shutdown()
        owner.shutdown()


def test_named_subject_conflicts_and_verb_like_names_stay_scoped(tmp_path) -> None:
    plugin = _provider(tmp_path)
    try:
        film = _store(plugin, FILM_CONTENT)
        verb = _store(plugin, VERB_NAME_CONTENT)
        person = _store(plugin, PERSON_CONTENT, target="user")
        zephyr = _store(plugin, ZEPHYR_CONTENT, target="ops")
        atlas = _store(plugin, ATLAS_CONTENT)

        film_natural = _search(plugin, NATURAL_QUERY)
        assert film["id"] in _search_ids(film_natural), film_natural
        assert verb["id"] not in _search_ids(film_natural), film_natural

        xinghe = _search(plugin, "星河现在的API 地址是什么？")
        assert film["id"] not in _search_ids(xinghe), xinghe

        quoted = _search(plugin, "`北风计划`的进度")
        assert film["id"] not in _search_ids(quoted), quoted

        verb_hit = _search(plugin, f"{VERB_NAME}现在的端口是什么")
        assert verb["id"] in _search_ids(verb_hit), verb_hit
        assert film["id"] not in _search_ids(verb_hit), verb_hit
        quoted_verb = _search(plugin, f"`{VERB_NAME}`的端口是什么")
        assert verb["id"] in _search_ids(quoted_verb), quoted_verb

        person_hit = _search(plugin, "小明的偏好是什么")
        person_miss = _search(plugin, "小红的偏好是什么")
        assert person["id"] in _search_ids(person_hit), person_hit
        assert person["id"] not in _search_ids(person_miss), person_miss

        zephyr_hit = _search(plugin, "Project Zephyr rollback worker queue drain")
        atlas_hit = _search(plugin, "Project Atlas production deploy command")
        assert zephyr["id"] in _search_ids(zephyr_hit), zephyr_hit
        assert atlas["id"] not in _search_ids(zephyr_hit), zephyr_hit
        assert atlas["id"] in _search_ids(atlas_hit), atlas_hit
        assert film["id"] not in _search_ids(atlas_hit), atlas_hit
    finally:
        plugin.shutdown()


def test_structured_claim_subject_still_owns_scope() -> None:
    service = RecallService(
        type(
            "Provider",
            (),
            {
                "_retrieval_config": {
                    "mode": "lexical",
                    "entity_scope_filter_enabled": True,
                    "min_score": 0.0,
                }
            },
        )()
    )
    claimed = RecallItem(
        id="atlas-claim",
        content="Recovery notes mention Titan only as an unrelated example.",
        summary="Declared Atlas procedure.",
        source="tool-store",
        target="project",
        score=0.9,
        updated_at="2026-03-18T00:00:00+00:00",
        metadata={
            "entities": ["titan"],
            "claim": {"subject": "Atlas", "predicate": "procedure", "value": "x"},
        },
    )

    assert (
        service._entity_scope_mismatch(
            "What is AtLaS recovery procedure?", claimed, claimed.metadata
        )
        is False
    )
    assert (
        service._entity_scope_mismatch(
            "What is Titan recovery procedure?", claimed, claimed.metadata
        )
        is True
    )
