# Scope Recall Configuration Reference

This file is generated from the packaged `config.json` registry. It lists every supported leaf key, its default value, risk level, and whether a Hermes restart/reload is normally required.


## `auto_capture`

- `auto_capture` (boolean; risk: `low`; restart_required: `no`) — Capture eligible conversation turns into Scope Recall. Default: `true`

## `auto_recall`

- `auto_recall` (boolean; risk: `low`; restart_required: `no`) — Enable automatic recall injection at turn start. Default: `true`

## `auto_recall_max_chars`

- `auto_recall_max_chars` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `auto_recall_max_chars` in the `auto_recall_max_chars` group. Default: `600`

## `auto_recall_max_items`

- `auto_recall_max_items` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `auto_recall_max_items` in the `auto_recall_max_items` group. Default: `3`

## `auto_recall_min_length`

- `auto_recall_min_length` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `auto_recall_min_length` in the `auto_recall_min_length` group. Default: `15`

## `auto_recall_min_repeated`

- `auto_recall_min_repeated` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `auto_recall_min_repeated` in the `auto_recall_min_repeated` group. Default: `8`

## `auto_recall_per_item_max_chars`

- `auto_recall_per_item_max_chars` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `auto_recall_per_item_max_chars` in the `auto_recall_per_item_max_chars` group. Default: `180`

## `capture_assistant`

- `capture_assistant` (boolean; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_assistant` in the `capture_assistant` group. Default: `false`

## `capture_hard_max_chars`

- `capture_hard_max_chars` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_hard_max_chars` in the `capture_hard_max_chars` group. Default: `2500`

## `capture_llm`

- `capture_llm.api_key_env` (array; risk: `high`; restart_required: `no`) — Scope Recall configuration key `capture_llm.api_key_env` in the `capture_llm` group. Default: `["SCOPE_RECALL_CAPTURE_LLM_API_KEY", "OPENAI_API_KEY"]`
- `capture_llm.base_url` (string; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.base_url` in the `capture_llm` group. Default: `"https://api.openai.com"`
- `capture_llm.enabled` (boolean; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.enabled` in the `capture_llm` group. Default: `false`
- `capture_llm.max_tokens_per_turn` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.max_tokens_per_turn` in the `capture_llm` group. Default: `2000`
- `capture_llm.min_assistant_chars` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.min_assistant_chars` in the `capture_llm` group. Default: `30`
- `capture_llm.min_user_chars` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.min_user_chars` in the `capture_llm` group. Default: `20`
- `capture_llm.model` (string; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.model` in the `capture_llm` group. Default: `"gpt-4o-mini"`
- `capture_llm.timeout` (number; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_llm.timeout` in the `capture_llm` group. Default: `15.0`

## `capture_raw_user`

- `capture_raw_user` (boolean; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_raw_user` in the `capture_raw_user` group. Default: `false`

## `capture_skip_patterns`

- `capture_skip_patterns` (array; risk: `low`; restart_required: `no`) — Scope Recall configuration key `capture_skip_patterns` in the `capture_skip_patterns` group. Default: `["^\\[Recent Telegram chat history", "^\\[CONTEXT COMPACTION", "Earlier turns were compacted into the summary below", "Conversation continues after context compression", "^\\[Your active task list was preserved across context compression\\]", "^\\[IMPORTANT: Background process ", "^## Active Task(?:\\n|\\r|$)", "^## Remaining Work(?:\\n|\\r|$)", "^Review the conversation above and update the skill library", "call the memory tool .*output only the raw json", "reply with ok and nothing else", "^\\s*you are an ai assistant", "<available_skills>[\\s\\S]*?</available_skills>"]`

## `curated_memory`

- `curated_memory.allowed_user_ids` (array; risk: `low`; restart_required: `no`) — Scope Recall configuration key `curated_memory.allowed_user_ids` in the `curated_memory` group. Default: `[]`
- `curated_memory.mode` (string; risk: `low`; restart_required: `no`; choices: `single-user, shared`) — Scope Recall configuration key `curated_memory.mode` in the `curated_memory` group. Default: `"single-user"`

## `enable_tools`

- `enable_tools` (boolean; risk: `low`; restart_required: `no`) — Scope Recall configuration key `enable_tools` in the `enable_tools` group. Default: `true`

## `experience`

- `experience.allow_risky_direct_reuse` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.allow_risky_direct_reuse` in the `experience` group. Default: `false`
- `experience.auto_promote_low_risk` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.auto_promote_low_risk` in the `experience` group. Default: `false`
- `experience.auto_promotion_enabled` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.auto_promotion_enabled` in the `experience` group. Default: `false`
- `experience.auto_promotion_limit_sessions` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.auto_promotion_limit_sessions` in the `experience` group. Default: `20`
- `experience.direct_reuse_min_confidence` (number; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.direct_reuse_min_confidence` in the `experience` group. Default: `0.82`
- `experience.enabled` (boolean; risk: `medium`; restart_required: `yes`) — Enable reusable Experience playbook surfaces. Default: `true`
- `experience.min_query_chars` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.min_query_chars` in the `experience` group. Default: `8`
- `experience.packet_max_chars` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.packet_max_chars` in the `experience` group. Default: `1400`
- `experience.prefetch_enabled` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.prefetch_enabled` in the `experience` group. Default: `true`
- `experience.promotion_min_entries` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.promotion_min_entries` in the `experience` group. Default: `3`
- `experience.promotion_min_tool_entries` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.promotion_min_tool_entries` in the `experience` group. Default: `1`
- `experience.promotion_require_verification` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `experience.promotion_require_verification` in the `experience` group. Default: `true`

## `forgetting`

- `forgetting.archive_assistant_scratch` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `forgetting.archive_assistant_scratch` in the `forgetting` group. Default: `true`
- `forgetting.archive_duplicates` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `forgetting.archive_duplicates` in the `forgetting` group. Default: `true`
- `forgetting.archive_very_short` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `forgetting.archive_very_short` in the `forgetting` group. Default: `true`
- `forgetting.enabled` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `forgetting.enabled` in the `forgetting` group. Default: `true`
- `forgetting.hard_delete_sensitive` (boolean; risk: `high`; restart_required: `no`) — Allow sensitive-data cleanup paths to hard-delete when explicitly invoked. Default: `true`
- `forgetting.soft_archive_default` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `forgetting.soft_archive_default` in the `forgetting` group. Default: `true`

## `identity`

- `identity.cli_user_id_fallback` (string; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `identity.cli_user_id_fallback` in the `identity` group. Default: `"local"`
- `identity.cross_platform_shared_scope` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `identity.cross_platform_shared_scope` in the `identity` group. Default: `false`

## `journal`

- `journal.append_v1` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.append_v1` in the `journal` group. Default: `true`
- `journal.background_digest_enabled` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.background_digest_enabled` in the `journal` group. Default: `true`
- `journal.backlog_fail_entries` (integer; risk: `medium`; restart_required: `yes`) — Doctor failure threshold for unprocessed journal backlog. Default: `3000`
- `journal.backlog_max_age_hours` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.backlog_max_age_hours` in the `journal` group. Default: `72`
- `journal.backlog_warn_entries` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.backlog_warn_entries` in the `journal` group. Default: `500`
- `journal.digest_interval_hours` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.digest_interval_hours` in the `journal` group. Default: `2`
- `journal.digest_on_session_end` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.digest_on_session_end` in the `journal` group. Default: `false`
- `journal.dynamic_backlog_threshold` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.dynamic_backlog_threshold` in the `journal` group. Default: `2000`
- `journal.dynamic_max_entries_enabled` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.dynamic_max_entries_enabled` in the `journal` group. Default: `true`
- `journal.enabled` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.enabled` in the `journal` group. Default: `true`
- `journal.endpoint` (string; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.endpoint` in the `journal` group. Default: `""`
- `journal.extractor` (string; risk: `medium`; restart_required: `yes`; choices: `llm, heuristic`) — Scope Recall configuration key `journal.extractor` in the `journal` group. Default: `"llm"`
- `journal.max_entries_per_digest` (integer; risk: `medium`; restart_required: `yes`) — Maximum journal entries a digest run may review before dynamic backlog expansion. Default: `500`
- `journal.max_entries_per_digest_ceiling` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.max_entries_per_digest_ceiling` in the `journal` group. Default: `1200`
- `journal.retention_days` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.retention_days` in the `journal` group. Default: `0`
- `journal.tool_trace_hard_max_chars` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.tool_trace_hard_max_chars` in the `journal` group. Default: `4000`
- `journal.tool_trace_include_output_preview` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.tool_trace_include_output_preview` in the `journal` group. Default: `false`
- `journal.tool_trace_max_chars` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.tool_trace_max_chars` in the `journal` group. Default: `1800`
- `journal.tool_trace_preview_max_chars` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.tool_trace_preview_max_chars` in the `journal` group. Default: `500`
- `journal.tool_trace_skip_names` (array; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `journal.tool_trace_skip_names` in the `journal` group. Default: `["todo", "skill_view", "skills_list", "session_messages"]`

## `maintenance_tools_enabled`

- `maintenance_tools_enabled` (boolean; risk: `low`; restart_required: `yes`) — Scope Recall configuration key `maintenance_tools_enabled` in the `maintenance_tools_enabled` group. Default: `false`

## `max_recall_per_turn`

- `max_recall_per_turn` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `max_recall_per_turn` in the `max_recall_per_turn` group. Default: `10`

## `min_capture_length`

- `min_capture_length` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `min_capture_length` in the `min_capture_length` group. Default: `40`

## `min_score`

- `min_score` (number; risk: `low`; restart_required: `no`) — Scope Recall configuration key `min_score` in the `min_score` group. Default: `0.18`

## `per_turn_extraction`

- `per_turn_extraction.enabled` (boolean; risk: `low`; restart_required: `no`) — Scope Recall configuration key `per_turn_extraction.enabled` in the `per_turn_extraction` group. Default: `false`

## `query_char_limit`

- `query_char_limit` (integer; risk: `low`; restart_required: `no`) — Scope Recall configuration key `query_char_limit` in the `query_char_limit` group. Default: `1000`

## `retrieval`

- `retrieval.bm25_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.bm25_weight` in the `retrieval` group. Default: `0.15`
- `retrieval.candidate_pool` (integer; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.candidate_pool` in the `retrieval` group. Default: `12`
- `retrieval.entity_distance_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.entity_distance_weight` in the `retrieval` group. Default: `0.04`
- `retrieval.entity_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.entity_weight` in the `retrieval` group. Default: `0.06`
- `retrieval.freshness_base_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.freshness_base_weight` in the `retrieval` group. Default: `0.22`
- `retrieval.freshness_hints` (array; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.freshness_hints` in the `retrieval` group. Default: `["current", "currently", "latest", "new", "newest", "now", "recent", "recently", "today", "updated"]`
- `retrieval.freshness_max_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.freshness_max_weight` in the `retrieval` group. Default: `0.42`
- `retrieval.freshness_step_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.freshness_step_weight` in the `retrieval` group. Default: `0.1`
- `retrieval.fusion_strategy` (string; risk: `medium`; restart_required: `no`; choices: `rrf, weighted`) — Scope Recall configuration key `retrieval.fusion_strategy` in the `retrieval` group. Default: `"rrf"`
- `retrieval.general_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.general_weight` in the `retrieval` group. Default: `0.35`
- `retrieval.include_general` (string; risk: `medium`; restart_required: `no`; choices: `never, same-scope, always`) — Scope Recall configuration key `retrieval.include_general` in the `retrieval` group. Default: `"same-scope"`
- `retrieval.lexical_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.lexical_weight` in the `retrieval` group. Default: `0.45`
- `retrieval.metadata_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.metadata_weight` in the `retrieval` group. Default: `0.08`
- `retrieval.metric` (string; risk: `medium`; restart_required: `no`; choices: `cosine, dot, l2`) — Scope Recall configuration key `retrieval.metric` in the `retrieval` group. Default: `"cosine"`
- `retrieval.min_score` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.min_score` in the `retrieval` group. Default: `0.18`
- `retrieval.mode` (string; risk: `medium`; restart_required: `no`; choices: `lexical, vector, hybrid`) — Recall mode: lexical, vector, or hybrid. Default: `"hybrid"`
- `retrieval.relation_contradiction_mode` (string; risk: `medium`; restart_required: `no`; choices: `surface, suppress, penalize`) — Scope Recall configuration key `retrieval.relation_contradiction_mode` in the `retrieval` group. Default: `"surface"`
- `retrieval.relation_contradicts_penalty` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.relation_contradicts_penalty` in the `retrieval` group. Default: `0.0`
- `retrieval.relation_rerank_enabled` (boolean; risk: `medium`; restart_required: `no`) — Enable small relation-graph rerank bonuses after primary recall scoring. Default: `false`
- `retrieval.relation_rerank_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.relation_rerank_weight` in the `retrieval` group. Default: `0.04`
- `retrieval.relation_superseded_penalty` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.relation_superseded_penalty` in the `retrieval` group. Default: `0.0`
- `retrieval.relation_supersedes_boost` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.relation_supersedes_boost` in the `retrieval` group. Default: `0.04`
- `retrieval.relation_supports_boost` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.relation_supports_boost` in the `retrieval` group. Default: `0.04`
- `retrieval.rrf_bm25_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_bm25_weight` in the `retrieval` group. Default: `1.0`
- `retrieval.rrf_curated_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_curated_weight` in the `retrieval` group. Default: `1.25`
- `retrieval.rrf_k` (integer; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_k` in the `retrieval` group. Default: `60`
- `retrieval.rrf_lexical_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_lexical_weight` in the `retrieval` group. Default: `1.0`
- `retrieval.rrf_min_signals` (integer; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_min_signals` in the `retrieval` group. Default: `2`
- `retrieval.rrf_vector_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_vector_weight` in the `retrieval` group. Default: `1.0`
- `retrieval.rrf_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.rrf_weight` in the `retrieval` group. Default: `0.18`
- `retrieval.temporal_decay_enabled` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_decay_enabled` in the `retrieval` group. Default: `false`
- `retrieval.temporal_decay_floor` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_decay_floor` in the `retrieval` group. Default: `0.65`
- `retrieval.temporal_decay_half_life_days` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_decay_half_life_days` in the `retrieval` group. Default: `180.0`
- `retrieval.temporal_decay_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_decay_weight` in the `retrieval` group. Default: `0.0`
- `retrieval.temporal_policy_durable_types` (array; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_durable_types` in the `retrieval` group. Default: `["constraint", "decision", "environment_fact", "fact", "factual", "memory", "ops", "ops_procedure", "preference", "procedure", "project", "project_fact", "resource", "user_preference", "workflow"]`
- `retrieval.temporal_policy_enabled` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_enabled` in the `retrieval` group. Default: `true`
- `retrieval.temporal_policy_episodic_types` (array; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_episodic_types` in the `retrieval` group. Default: `["episodic", "summary"]`
- `retrieval.temporal_policy_temporary_types` (array; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_temporary_types` in the `retrieval` group. Default: `["scratch", "temporary", "temporary_state", "tool_trace"]`
- `retrieval.temporal_policy_weights.default` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_weights.default` in the `retrieval` group. Default: `1.0`
- `retrieval.temporal_policy_weights.durable_fact` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_weights.durable_fact` in the `retrieval` group. Default: `0.25`
- `retrieval.temporal_policy_weights.episodic` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_weights.episodic` in the `retrieval` group. Default: `0.8`
- `retrieval.temporal_policy_weights.temporary` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.temporal_policy_weights.temporary` in the `retrieval` group. Default: `1.0`
- `retrieval.top_k` (integer; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.top_k` in the `retrieval` group. Default: `5`
- `retrieval.vector_min_score` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.vector_min_score` in the `retrieval` group. Default: `0.12`
- `retrieval.vector_only_min_score` (number; risk: `medium`; restart_required: `no`) — Minimum score for vector-only candidates to survive recall filtering. Default: `0.68`
- `retrieval.vector_weight` (number; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `retrieval.vector_weight` in the `retrieval` group. Default: `0.55`

## `secret_index_tools_enabled`

- `secret_index_tools_enabled` (boolean; risk: `high`; restart_required: `yes`) — Scope Recall configuration key `secret_index_tools_enabled` in the `secret_index_tools_enabled` group. Default: `false`

## `shared_pool`

- `shared_pool.allowed_targets` (array; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `shared_pool.allowed_targets` in the `shared_pool` group. Default: `["memory", "project", "ops"]`
- `shared_pool.enabled` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `shared_pool.enabled` in the `shared_pool` group. Default: `false`
- `shared_pool.pool_id` (string; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `shared_pool.pool_id` in the `shared_pool` group. Default: `"default"`
- `shared_pool.write_enabled` (boolean; risk: `medium`; restart_required: `no`) — Scope Recall configuration key `shared_pool.write_enabled` in the `shared_pool` group. Default: `false`

## `tool_schema_extra_tools`

- `tool_schema_extra_tools` (array; risk: `low`; restart_required: `yes`) — Scope Recall configuration key `tool_schema_extra_tools` in the `tool_schema_extra_tools` group. Default: `[]`

## `tool_schema_profile`

- `tool_schema_profile` (string; risk: `low`; restart_required: `yes`; choices: `compact, standard`) — Scope Recall configuration key `tool_schema_profile` in the `tool_schema_profile` group. Default: `"compact"`

## `vector`

- `vector.backend` (string; risk: `medium`; restart_required: `yes`; choices: `lancedb, sqlite-bruteforce`) — Vector companion backend used for semantic recall. Default: `"lancedb"`
- `vector.embedder.api_key_env` (array; risk: `high`; restart_required: `yes`) — Environment variable names that may hold the embedding API key. Default: `["SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"]`
- `vector.embedder.base_url` (string; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.embedder.base_url` in the `vector` group. Default: `"https://generativelanguage.googleapis.com/v1beta/openai"`
- `vector.embedder.dimensions` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.embedder.dimensions` in the `vector` group. Default: `3072`
- `vector.embedder.model` (string; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.embedder.model` in the `vector` group. Default: `"gemini-embedding-001"`
- `vector.embedder.provider` (string; risk: `medium`; restart_required: `yes`; choices: `openai-compatible, openai, sentence-transformers, local-hash`) — Scope Recall configuration key `vector.embedder.provider` in the `vector` group. Default: `"openai-compatible"`
- `vector.enabled` (boolean; risk: `medium`; restart_required: `yes`) — Enable the rebuildable vector companion index. Default: `true`
- `vector.fallback_backend` (string; risk: `medium`; restart_required: `yes`; choices: `sqlite-bruteforce, disabled`) — Scope Recall configuration key `vector.fallback_backend` in the `vector` group. Default: `"sqlite-bruteforce"`
- `vector.fallback_embedder.dimensions` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.fallback_embedder.dimensions` in the `vector` group. Default: `256`
- `vector.fallback_embedder.model` (string; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.fallback_embedder.model` in the `vector` group. Default: `"hash-v1"`
- `vector.fallback_embedder.provider` (string; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.fallback_embedder.provider` in the `vector` group. Default: `"local-hash"`
- `vector.index_general` (boolean; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.index_general` in the `vector` group. Default: `false`
- `vector.sync_mode` (string; risk: `medium`; restart_required: `yes`; choices: `incremental, rebuild`) — Scope Recall configuration key `vector.sync_mode` in the `vector` group. Default: `"incremental"`
- `vector.table_name` (string; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.table_name` in the `vector` group. Default: `"memories"`
- `vector.top_k` (integer; risk: `medium`; restart_required: `yes`) — Scope Recall configuration key `vector.top_k` in the `vector` group. Default: `8`
