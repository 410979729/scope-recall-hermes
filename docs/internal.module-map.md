# Scope Recall Internal Module Map

Updated: `2026-07-08` for `scope-recall` 1.7.0 productization release.

## Quick rules

- `provider.py`: Hermes lifecycle/hook thin wiring only; do not add policy logic.
- `tooling.py`: tool argument normalization and dispatch only; complex behavior goes to focused modules.
- `schemas.py` / `provider_schemas.py` / `response_schemas.py`: schemas/contracts only.
- `sql_store.py`: schema and row helpers only; business flows belong elsewhere.
- New productization work should prefer small modules: `event_digest.py`, `candidate_extraction.py`, `skill_bridge.py`, `external_bridge.py`, `pgvector_store.py`, CLI/browser modules.
- Any live DB mutation requires backup + dry-run + explicit operator approval.

## Largest modules

- `memory_ops.py` — 1579 lines; capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- `nightly_digest.py` — 1399 lines; capabilities: install/rollout, provider hooks/runtime, journal/digest, candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- `experience_store.py` — 1265 lines; capabilities: candidate/governance/forgetting, experience/playbooks, schema/tooling, external bridge/shared, security/secrets, doctor/dashboard/observability
- `provider.py` — 1069 lines; capabilities: install/rollout, provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- `journal.py` — 1026 lines; capabilities: install/rollout, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- `sql_store.py` — 966 lines; capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- `tooling.py` — 916 lines; capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- `recall.py` — 808 lines; capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, external bridge/shared

## Capability ownership

### candidate/governance/forgetting
- `candidate_promotion.py`
- `capture_llm.py`
- `cli.py`
- `config.py`
- `config_schema.py`
- `digest_quality.py`
- `digest_run_results.py`
- `doctor_common.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `doctor_sqlite.py`
- `doctor_vector.py`
- `experience_bootstrap.py`
- `experience_classification.py`
- `experience_models.py`
- `experience_preflight.py`
- `experience_promotion.py`
- `experience_quality.py`
- `experience_replay.py`
- `experience_store.py`
- `experience_synthesis.py`
- `forgetting.py`
- `freshness.py`
- `governance.py`
- `governance_cleanup.py`
- `governance_scheduler.py`
- `graph.py`
- `graph_relations.py`
- `hygiene.py`
- `installer.py`
- `journal.py`
- `journal_candidates.py`
- `journal_extractors.py`
- `journal_recovery.py`
- `journal_store.py`
- `memory_ops.py`
- `memory_quality.py`
- `migration_openclaw.py`
- `nightly_digest.py`
- `nightly_llm.py`
- ... 13 more

### doctor/dashboard/observability
- `candidate_promotion.py`
- `capture.py`
- `capture_filters.py`
- `cli.py`
- `config_schema.py`
- `digest_quality.py`
- `digest_run_results.py`
- `doctor_common.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `doctor_source.py`
- `doctor_sqlite.py`
- `doctor_vector.py`
- `experience_bootstrap.py`
- `experience_evidence.py`
- `experience_models.py`
- `experience_preflight.py`
- `experience_promotion.py`
- `experience_quality.py`
- `experience_replay.py`
- `experience_store.py`
- `experience_synthesis.py`
- `forgetting.py`
- `freshness.py`
- `gating.py`
- `governance_cleanup.py`
- `governance_scheduler.py`
- `graph_hygiene.py`
- `hygiene.py`
- `installer.py`
- `journal.py`
- `journal_llm.py`
- `journal_recovery.py`
- `maintenance_ops.py`
- `memory_ops.py`
- `memory_quality.py`
- `migration_openclaw.py`
- `nightly_digest.py`
- `nightly_llm.py`
- `provider.py`
- ... 11 more

### experience/playbooks
- `candidate_promotion.py`
- `cli.py`
- `config.py`
- `config_schema.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `doctor_sqlite.py`
- `experience_bootstrap.py`
- `experience_classification.py`
- `experience_evidence.py`
- `experience_models.py`
- `experience_preflight.py`
- `experience_promotion.py`
- `experience_quality.py`
- `experience_replay.py`
- `experience_store.py`
- `experience_synthesis.py`
- `governance.py`
- `governance_cleanup.py`
- `governance_scheduler.py`
- `hygiene.py`
- `journal.py`
- `journal_recovery.py`
- `memory_quality.py`
- `provider.py`
- `provider_schemas.py`
- `response_schemas.py`
- `schemas.py`
- `sql_store.py`
- `task_boundary.py`
- `tooling.py`

### external bridge/shared
- `artifacts.py`
- `capture.py`
- `capture_llm.py`
- `config.py`
- `config_schema.py`
- `doctor_common.py`
- `doctor_experience.py`
- `experience_bootstrap.py`
- `experience_models.py`
- `experience_promotion.py`
- `experience_store.py`
- `governance.py`
- `governance_scheduler.py`
- `journal.py`
- `journal_extractors.py`
- `journal_store.py`
- `maintenance_ops.py`
- `memory_ops.py`
- `memory_quality.py`
- `models.py`
- `nightly_digest.py`
- `provider.py`
- `recall.py`
- `relation_extraction.py`
- `schemas.py`
- `scope.py`
- `secret_index.py`
- `sql_store.py`
- `tooling.py`

### graph/relations
- `artifacts.py`
- `candidate_promotion.py`
- `capture.py`
- `capture_llm.py`
- `config.py`
- `config_schema.py`
- `doctor_sqlite.py`
- `experience_bootstrap.py`
- `experience_classification.py`
- `forgetting.py`
- `governance.py`
- `governance_cleanup.py`
- `graph.py`
- `graph_hygiene.py`
- `graph_relations.py`
- `installer.py`
- `journal.py`
- `journal_candidates.py`
- `journal_extractors.py`
- `journal_store.py`
- `memory_ops.py`
- `migration_openclaw.py`
- `models.py`
- `nightly_digest.py`
- `provider.py`
- `provider_schemas.py`
- `recall.py`
- `recall_pipeline.py`
- `relation_extraction.py`
- `schemas.py`
- `scope.py`
- `scoring.py`
- `secret_index.py`
- `sql_store.py`
- `storage_views.py`
- `tooling.py`
- `vector_runtime.py`
- `vector_store.py`

### install/rollout
- `aliases.py`
- `capture.py`
- `cli.py`
- `doctor_journal.py`
- `doctor_source.py`
- `doctor_sqlite.py`
- `embedders.py`
- `experience_replay.py`
- `forgetting.py`
- `governance.py`
- `governance_cleanup.py`
- `installer.py`
- `journal.py`
- `journal_extractors.py`
- `memory_ops.py`
- `nightly_digest.py`
- `provider.py`
- `recall.py`
- `tooling.py`
- `vector_runtime.py`
- `vector_store.py`

### journal/digest
- `capture_filters.py`
- `cli.py`
- `config.py`
- `config_schema.py`
- `digest_quality.py`
- `digest_run_results.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `embedders.py`
- `experience_bootstrap.py`
- `experience_classification.py`
- `experience_evidence.py`
- `experience_models.py`
- `experience_promotion.py`
- `experience_synthesis.py`
- `forgetting.py`
- `governance.py`
- `governance_cleanup.py`
- `governance_scheduler.py`
- `journal.py`
- `journal_candidates.py`
- `journal_extractors.py`
- `journal_llm.py`
- `journal_recovery.py`
- `journal_store.py`
- `memory_quality.py`
- `migration_openclaw.py`
- `models.py`
- `nightly_digest.py`
- `nightly_llm.py`
- `provider.py`
- `schemas.py`
- `secret_index.py`
- `sql_store.py`
- `sqlite_vector_store.py`
- `task_boundary.py`

### provider hooks/runtime
- `__init__.py`
- `aliases.py`
- `capture.py`
- `capture_filters.py`
- `config.py`
- `config_schema.py`
- `digest_run_results.py`
- `doctor_common.py`
- `doctor_experience.py`
- `doctor_sqlite.py`
- `embedders.py`
- `experience_classification.py`
- `http_utils.py`
- `hygiene.py`
- `installer.py`
- `journal_extractors.py`
- `maintenance_ops.py`
- `memory_ops.py`
- `models.py`
- `nightly_digest.py`
- `nightly_llm.py`
- `prompting.py`
- `provider.py`
- `provider_schemas.py`
- `recall.py`
- `schemas.py`
- `storage_views.py`
- `tooling.py`
- `vector_runtime.py`
- `vector_store.py`

### recall/ranking
- `__init__.py`
- `aliases.py`
- `artifacts.py`
- `capture.py`
- `capture_filters.py`
- `capture_llm.py`
- `cli.py`
- `config.py`
- `config_schema.py`
- `digest_quality.py`
- `doctor_common.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `doctor_sqlite.py`
- `doctor_vector.py`
- `experience_bootstrap.py`
- `experience_classification.py`
- `experience_preflight.py`
- `experience_promotion.py`
- `experience_quality.py`
- `experience_replay.py`
- `forgetting.py`
- `freshness.py`
- `gating.py`
- `governance.py`
- `governance_cleanup.py`
- `governance_scheduler.py`
- `graph.py`
- `graph_hygiene.py`
- `installer.py`
- `journal.py`
- `journal_candidates.py`
- `journal_extractors.py`
- `journal_llm.py`
- `journal_store.py`
- `maintenance_ops.py`
- `memory_ops.py`
- `memory_quality.py`
- `migration.py`
- `migration_openclaw.py`
- ... 18 more

### schema/tooling
- `capture_llm.py`
- `config.py`
- `config_schema.py`
- `digest_run_results.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `doctor_sqlite.py`
- `doctor_vector.py`
- `experience_bootstrap.py`
- `experience_classification.py`
- `experience_models.py`
- `experience_promotion.py`
- `experience_replay.py`
- `experience_store.py`
- `experience_synthesis.py`
- `forgetting.py`
- `freshness.py`
- `governance_cleanup.py`
- `governance_scheduler.py`
- `graph.py`
- `installer.py`
- `journal.py`
- `journal_recovery.py`
- `journal_store.py`
- `memory_quality.py`
- `migration.py`
- `migration_openclaw.py`
- `nightly_digest.py`
- `nightly_llm.py`
- `provider.py`
- `provider_schemas.py`
- `response_schemas.py`
- `schemas.py`
- `scope_recall.py`
- `sql_store.py`
- `sqlite_vector_store.py`
- `tooling.py`
- `vector_runtime.py`
- `vector_store.py`

### security/secrets
- `candidate_promotion.py`
- `capture_filters.py`
- `capture_llm.py`
- `config.py`
- `config_schema.py`
- `doctor_common.py`
- `doctor_experience.py`
- `doctor_journal.py`
- `doctor_sqlite.py`
- `experience_evidence.py`
- `experience_preflight.py`
- `experience_promotion.py`
- `experience_quality.py`
- `experience_store.py`
- `experience_synthesis.py`
- `forgetting.py`
- `gating.py`
- `governance.py`
- `governance_cleanup.py`
- `http_utils.py`
- `hygiene.py`
- `journal.py`
- `journal_candidates.py`
- `journal_llm.py`
- `journal_store.py`
- `memory_ops.py`
- `memory_quality.py`
- `migration_openclaw.py`
- `nightly_digest.py`
- `nightly_llm.py`
- `provider.py`
- `provider_schemas.py`
- `schemas.py`
- `secret_index.py`
- `sql_store.py`
- `tooling.py`
- `vector_runtime.py`

### vector/embedding
- `capture.py`
- `cli.py`
- `config.py`
- `config_schema.py`
- `doctor_common.py`
- `doctor_sqlite.py`
- `doctor_vector.py`
- `embedders.py`
- `experience_bootstrap.py`
- `forgetting.py`
- `hygiene.py`
- `installer.py`
- `journal.py`
- `journal_candidates.py`
- `memory_ops.py`
- `migration_openclaw.py`
- `models.py`
- `nightly_digest.py`
- `provider.py`
- `recall.py`
- `recall_pipeline.py`
- `schemas.py`
- `scoring.py`
- `secret_index.py`
- `sql_store.py`
- `sqlite_vector_store.py`
- `storage_views.py`
- `tooling.py`
- `vector_runtime.py`
- `vector_store.py`

## Module inventory

### `__init__.py`
- Lines: 20
- Capabilities: provider hooks/runtime, recall/ranking
- Top functions: register
- Related tests: `tests/test_embedders.py`, `tests/test_fact_freshness.py`, `tests/test_forgetting.py`, `tests/test_governance_cleanup.py`, `tests/test_hygiene.py`, `tests/test_installer.py`, `tests/test_journal_digest.py`, `tests/test_memory_quality_kernel.py`, `tests/test_nightly_digest.py`, `tests/test_relation_aware_recall.py`, `tests/test_release.py`, `tests/test_retrieval_policy.py`

### `aliases.py`
- Lines: 56
- Capabilities: install/rollout, provider hooks/runtime, recall/ranking
- Top functions: canonicalize_alias
- Related tests: `tests/test_provider.py`, `tests/test_scoring.py`, `tests/test_v1015_audit_regressions.py`

### `artifacts.py`
- Lines: 183
- Capabilities: recall/ranking, graph/relations, external bridge/shared
- Top functions: _strip_url, _artifact_key, _github_artifact_from_match, extract_artifacts, artifact_label, artifact_anchor_block, enrich_content_with_artifact_anchors, merge_artifact_metadata
- Related tests: `tests/test_installer.py`, `tests/test_nightly_digest.py`, `tests/test_provider.py`, `tests/test_release.py`

### `candidate_promotion.py`
- Lines: 273
- Capabilities: candidate/governance/forgetting, experience/playbooks, graph/relations, security/secrets, doctor/dashboard/observability
- Classes: CandidateDecision
- Top functions: default_lane_for_decision, now_iso, load_metadata, lifecycle, _float_meta, _row_value, _contains_any, _normalized_conflict_text, _active_memory_conflict, classify_candidate_row, _scope_filter_sql, candidate_rows, candidate_debt_report
- Related tests: `tests/test_memory_candidate_promotion.py`, `tests/test_memory_quality_kernel.py`, `tests/test_memory_quality_lint.py`

### `capture.py`
- Lines: 171
- Capabilities: install/rollout, provider hooks/runtime, vector/embedding, recall/ranking, graph/relations, external bridge/shared, doctor/dashboard/observability
- Top functions: start_writer, writer_loop, flush_writer, shutdown_writer, enqueue_store, store_now
- Related tests: `tests/test_audit_regressions.py`, `tests/test_capture_filters.py`, `tests/test_capture_llm_manual.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_sqlite_readonly.py`, `tests/test_embedders.py`, `tests/test_entity_graph_hygiene.py`, `tests/test_experience_replay.py`, `tests/test_golden_benchmark.py`, `tests/test_governance_contract_regressions.py`, `tests/test_governance_scheduler.py`, `tests/test_graph_relation_backfill.py`

### `capture_filters.py`
- Lines: 259
- Capabilities: provider hooks/runtime, journal/digest, recall/ranking, security/secrets, doctor/dashboard/observability
- Classes: CaptureFilterResult
- Top functions: sanitize_capture_text, contains_secret_like_text, redact_secret_like_text, redact_private_paths, sanitize_report_text, _configured_patterns, _compiled_configured_patterns, _normalize_skip_pattern, should_capture_text
- Related tests: `tests/test_capture_filters.py`, `tests/test_doctor_experience.py`, `tests/test_governance_contract_regressions.py`, `tests/test_provider.py`

### `capture_llm.py`
- Lines: 372
- Capabilities: candidate/governance/forgetting, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets
- Classes: Candidate
- Top functions: _truthy, extract_capture_candidates, _resolve_api_key, _call_openai_compatible, _log_parse_failure, _repair_truncated, _parse_response
- Related tests: `tests/test_audit_regressions.py`, `tests/test_capture_llm_manual.py`, `tests/test_provider_schemas.py`, `tests/test_v1015_audit_regressions.py`

### `cli.py`
- Lines: 130
- Capabilities: install/rollout, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, doctor/dashboard/observability
- Top functions: _scripts_dir, _run_script, _merge_injected_args, _match_script_command, main
- Related tests: `tests/test_audit_regressions.py`, `tests/test_benchmark_regression_cases.py`, `tests/test_conflict_governance.py`, `tests/test_dashboard.py`, `tests/test_doctor_modularization.py`, `tests/test_embedders.py`, `tests/test_entity_graph_hygiene.py`, `tests/test_experience_bootstrap.py`, `tests/test_experience_store.py`, `tests/test_governance_cleanup.py`, `tests/test_graph_relation_backfill.py`, `tests/test_hygiene.py`

### `config.py`
- Lines: 254
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets
- Top functions: _deep_merge, _expand_dotted_keys, load_runtime_config, save_runtime_config
- Related tests: `tests/test_audit_regressions.py`, `tests/test_benchmark_regression_cases.py`, `tests/test_capture_filters.py`, `tests/test_capture_llm_manual.py`, `tests/test_config_schema.py`, `tests/test_conflict_governance.py`, `tests/test_dashboard.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_doctor_secret_scan.py`, `tests/test_embedders.py`

### `config_schema.py`
- Lines: 159
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: packaged_config_path, load_packaged_config, _value_type, _description, _risk, _restart_required, _flatten, build_config_registry, render_configuration_markdown
- Related tests: `tests/test_config_schema.py`, `tests/test_installer.py`, `tests/test_provider_schemas.py`

### `digest_quality.py`
- Lines: 110
- Capabilities: journal/digest, candidate/governance/forgetting, recall/ranking, doctor/dashboard/observability
- Classes: DigestQuality
- Top functions: _candidate_text, score_digest_candidate
- Related tests: `tests/test_nightly_digest.py`

### `digest_run_results.py`
- Lines: 239
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, schema/tooling, doctor/dashboard/observability
- Top functions: no_unprocessed_journal_result, journal_digest_metadata, journal_digest_success_result, journal_digest_receipt_fields, nightly_no_candidate_fallback, nightly_status_payload, nightly_digest_result, nightly_digest_metadata
- Related tests: `tests/test_digest_run_results.py`

### `doctor_common.py`
- Lines: 207
- Capabilities: provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: contains_secret_like_text, redact_secret_like_text, sanitize_report_text, read_text, plugin_yaml_version, deep_merge, load_profile_dotenv, load_runtime_config, coerce_list, coerce_int, embedder_config_available, expected_embedder_from_config, vector_enabled_from_config, vector_backend_from_config, vector_fallback_backend_from_config, _lifecycle_visible_clause
- Related tests: `tests/test_doctor_modularization.py`

### `doctor_experience.py`
- Lines: 417
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: experience_config_summary, _json_value, _list_count, _scope_values, _duplicate_playbook_groups, _replay_case_count, _experience_maturity_payload, experience_report, nightly_digest_report
- Related tests: `tests/test_doctor_experience.py`, `tests/test_doctor_modularization.py`, `tests/test_fact_freshness.py`, `tests/test_release.py`

### `doctor_journal.py`
- Lines: 489
- Capabilities: install/rollout, journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, security/secrets, doctor/dashboard/observability
- Top functions: journal_enabled_from_config, journal_backlog_age_hours, classify_reason_counts, _json_dict, journal_report
- Related tests: `tests/test_doctor_modularization.py`

### `doctor_source.py`
- Lines: 70
- Capabilities: install/rollout, doctor/dashboard/observability
- Top functions: source_report
- Related tests: `tests/test_doctor_modularization.py`

### `doctor_sqlite.py`
- Lines: 241
- Capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, security/secrets, doctor/dashboard/observability
- Top functions: sqlite_report, memory_candidate_debt_report, memory_quality_lint_report, memory_secret_report
- Related tests: `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_doctor_sqlite_readonly.py`, `tests/test_memory_quality_lint.py`

### `doctor_vector.py`
- Lines: 361
- Capabilities: candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling, doctor/dashboard/observability
- Top functions: lancedb_table_names, lancedb_vector_ids, vector_dimensions, run_vector_search_smoke, sqlite_truth_db_exists, sqlite_indexable_memory_ids, sqlite_indexable_memory_count, apply_vector_truth_consistency, lancedb_vector_report, sqlite_vector_search_smoke, sqlite_vector_report, vector_report, disabled_vector_report
- Related tests: `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_optional_vector_deps.py`, `tests/test_release.py`

### `embedders.py`
- Lines: 549
- Capabilities: install/rollout, provider hooks/runtime, journal/digest, vector/embedding
- Classes: EmbedderInfo, BaseEmbedder, LocalHashEmbedder, LocalDebugEmbedder, OpenAICompatibleEmbedder, OpenAIEmbedder, SentenceTransformersEmbedder, MiniMaxEmbedder
- Top functions: _normalize_feature, _char_ngrams, _coerce_list, _resolve_from_env, _resolve_optional_value, _resolve_api_keys, _known_dimensions, build_embedder
- Related tests: `tests/test_embedders.py`, `tests/test_release.py`

### `experience_bootstrap.py`
- Lines: 250
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, doctor/dashboard/observability
- Top functions: _step, _default_replay_cases, _playbook, _experience_schema_exists, bootstrap_core_playbooks
- Related tests: `tests/test_experience_bootstrap.py`, `tests/test_experience_replay.py`

### `experience_classification.py`
- Lines: 125
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, graph/relations
- Classes: ExperienceClassification
- Top functions: _norm, _has_any, _word_count, classify_experience_task
- Related tests: `tests/test_experience_promotion.py`

### `experience_evidence.py`
- Lines: 90
- Capabilities: journal/digest, experience/playbooks, security/secrets, doctor/dashboard/observability
- Top functions: _entry_value, _kind_for, evidence_anchor_for_entry, extract_evidence_anchors
- Related tests: `tests/test_experience_evidence.py`, `tests/test_experience_replay_generation.py`, `tests/test_release.py`

### `experience_models.py`
- Lines: 198
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, schema/tooling, external bridge/shared, doctor/dashboard/observability
- Classes: ExperienceValidationError, PlaybookStep, ProceduralPlaybook
- Top functions: _require_text, _require_list, _text_tuple, _mapping_tuple, _validate_steps, validate_procedural_playbook
- Related tests: `tests/test_experience_schema.py`, `tests/test_experience_store.py`, `tests/test_experience_synthesis.py`

### `experience_preflight.py`
- Lines: 335
- Capabilities: candidate/governance/forgetting, experience/playbooks, recall/ranking, security/secrets, doctor/dashboard/observability
- Top functions: _experience_config, _bool_config, _float_config, _int_config, _safe_int, _query_is_low_signal, _risky_capabilities, _safe_text, _policy_bool, _all_capabilities, _policy_sequence, _no_reuse_result, render_experience_packet, _preflight_summary, experience_preflight
- Related tests: `tests/test_experience_preflight.py`, `tests/test_experience_promotion.py`, `tests/test_experience_replay_generation.py`, `tests/test_experience_store.py`, `tests/test_experience_tools.py`

### `experience_promotion.py`
- Lines: 592
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: _now_iso, _json_dumps, _hash_id, _coerce_bool, _experience_config, _contains_any, _has_failure_signal, _entry_text, _tail_text, _completion_state, _tool_names, _verification, _risk_level, _promotion_quality, _first_user_goal, _goal_signal_key, _low_signal_goal, _title_suffix
- Related tests: `tests/test_experience_promotion.py`

### `experience_quality.py`
- Lines: 109
- Capabilities: candidate/governance/forgetting, experience/playbooks, recall/ranking, security/secrets, doctor/dashboard/observability
- Top functions: _entry_value, entry_text, _contains_any, assess_experience_quality
- Related tests: `tests/test_experience_quality.py`, `tests/test_experience_replay_generation.py`, `tests/test_release.py`

### `experience_replay.py`
- Lines: 199
- Capabilities: install/rollout, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, doctor/dashboard/observability
- Classes: ReplayCaseValidationError
- Top functions: _clean_term, _contains_term, coverage_hits, _coverage_ratio, _case_id, load_replay_cases, _average, _required_terms_from_case, _min_coverage_gain, evaluate_replay_case, build_replay_report
- Related tests: `tests/test_experience_replay.py`

### `experience_store.py`
- Lines: 1265
- Capabilities: candidate/governance/forgetting, experience/playbooks, schema/tooling, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: _now_iso, _json_dumps, _json_loads_checked, _json_loads, _scope_predicate, _run_scope_predicate, _reject_secret_like_value, _sanitize_report_value, _redact_run, _step_dicts, _playbook_payload, _related_skill_names, _sync_skill_anchors_for_playbook, _skill_governance_for_playbook, _attach_skill_governance, backfill_skill_anchors, _serialize_row, create_playbook
- Related tests: `tests/test_doctor_experience.py`, `tests/test_experience_preflight.py`, `tests/test_experience_replay.py`, `tests/test_experience_replay_generation.py`, `tests/test_experience_store.py`, `tests/test_experience_tools.py`, `tests/test_skill_governance.py`

### `experience_synthesis.py`
- Lines: 131
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, schema/tooling, security/secrets, doctor/dashboard/observability
- Top functions: _anchor_kinds, build_experience_playbook_payload
- Related tests: `tests/test_experience_replay_generation.py`, `tests/test_experience_synthesis.py`, `tests/test_release.py`

### `forgetting.py`
- Lines: 412
- Capabilities: install/rollout, journal/digest, candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling, graph/relations, security/secrets, doctor/dashboard/observability
- Classes: VectorDeleteStore
- Top functions: _now_iso, _json_loads, _json_dumps, _limited, _preview, _snapshot, _scoped_rows, _already_archived, _journal_template_transcript_noise, build_forgetting_report, _archive_memory, _delete_memory, run_forgetting
- Related tests: `tests/test_audit_regressions.py`, `tests/test_experience_tools.py`, `tests/test_forgetting.py`, `tests/test_governance_cleanup.py`, `tests/test_governance_scheduler.py`, `tests/test_graph_relation_backfill.py`, `tests/test_journal_digest.py`, `tests/test_provider_schemas.py`, `tests/test_shared_pool_write_policy.py`, `tests/test_tool_hygiene.py`

### `freshness.py`
- Lines: 289
- Capabilities: candidate/governance/forgetting, recall/ranking, schema/tooling, doctor/dashboard/observability
- Top functions: normalize_validator_kind, _parse_iso, normalize_freshness_status, _row_payload, _table_exists, memory_freshness_map, freshness_penalty, attach_freshness_metadata, _scope_filter_sql, fact_freshness_report
- Related tests: `tests/test_dashboard.py`, `tests/test_experience_schema.py`, `tests/test_fact_freshness.py`, `tests/test_forgetting.py`, `tests/test_governance_scheduler.py`, `tests/test_memory_quality_kernel.py`, `tests/test_provider.py`, `tests/test_roadmap_retrieval.py`, `tests/test_scoring.py`

### `gating.py`
- Lines: 191
- Capabilities: recall/ranking, security/secrets, doctor/dashboard/observability
- Top functions: stringify_content, clean_text, compact_text, is_trivial, normalize_query, should_skip_retrieval, query_tokens, stem_token, normalized_token_set, build_fts_query, like_terms, fts_escape, dedup_key, should_skip_capture, config_bool

### `governance.py`
- Lines: 458
- Capabilities: install/rollout, journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, graph/relations, external bridge/shared, security/secrets
- Classes: ExtractionCandidate
- Top functions: split_sentences, _unique_strings, _authority_for_source, _source_trust_for_authority, normalize_memory_type, classify_memory, merge_metadata, extract_candidates, _conflict_tokens, _claim_slot_and_value, _overlap_ratio, is_conflicting, merge_memory_text
- Related tests: `tests/test_conflict_governance.py`, `tests/test_doctor_journal_health.py`, `tests/test_experience_replay_generation.py`, `tests/test_forgetting.py`, `tests/test_governance_cleanup.py`, `tests/test_governance_contract_regressions.py`, `tests/test_governance_scheduler.py`, `tests/test_installer.py`, `tests/test_journal_digest.py`, `tests/test_journal_recovery.py`, `tests/test_memory_candidate_promotion.py`, `tests/test_memory_classification.py`

### `governance_cleanup.py`
- Lines: 539
- Capabilities: install/rollout, journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, graph/relations, security/secrets, doctor/dashboard/observability
- Top functions: _now_iso, _json_loads, _json_dumps, _is_archived, _has_new_archive_marker, _percent, _governance_table_exists, _table_columns, _audited_archive_ids, classify_cleanup_reason, _scope_clause, active_dirty_counts, find_cleanup_candidates, _snapshot_row, apply_cleanup, _archive_coverage_samples, governance_audit_coverage_report, backfill_legacy_archive_audit
- Related tests: `tests/test_forgetting.py`, `tests/test_governance_cleanup.py`, `tests/test_memory_candidate_promotion.py`, `tests/test_readonly_dry_run_contracts.py`

### `governance_scheduler.py`
- Lines: 323
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, external bridge/shared, doctor/dashboard/observability
- Top functions: _table_exists, _safe_count, _scope_where, _scope_any_where, _journal_snapshot, _experience_snapshot, _candidate_snapshot, _forgetting_snapshot, _cleanup_snapshot, _summary, run_governance_cycle, run_governance_cycle_for_home
- Related tests: `tests/test_experience_replay_generation.py`, `tests/test_governance_scheduler.py`, `tests/test_release.py`

### `graph.py`
- Lines: 467
- Capabilities: candidate/governance/forgetting, recall/ranking, schema/tooling, graph/relations
- Top functions: lifecycle_value, lifecycle_is_hidden, lifecycle_visible_sql, _hinted_cjk_entities, _jieba_entities, clamp_float, _is_tool_trace_entity, normalize_entity, _unique, extract_entities, metadata_entities, load_metadata, ensure_graph_schema, sync_memory_entities, backfill_memory_entities, query_entities, entity_overlap_bonus, entity_distance_scores
- Related tests: `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_entity_graph_hygiene.py`, `tests/test_fact_freshness.py`, `tests/test_forgetting.py`, `tests/test_graph_hygiene.py`, `tests/test_graph_relation_backfill.py`, `tests/test_graph_relation_benchmark.py`, `tests/test_openclaw_import.py`, `tests/test_provider.py`, `tests/test_relation_aware_recall.py`, `tests/test_relation_extraction.py`

### `graph_hygiene.py`
- Lines: 181
- Capabilities: recall/ranking, graph/relations, doctor/dashboard/observability
- Top functions: graph_hygiene_count_keys, empty_graph_hygiene_counts, table_names, graph_hygiene_counts, count_deletable_graph_hygiene_rows, delete_graph_hygiene_rows, remaining_graph_hygiene_rows, memory_db_path, repair_graph_hygiene
- Related tests: `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_entity_graph_hygiene.py`, `tests/test_graph_hygiene.py`, `tests/test_openclaw_import.py`

### `graph_relations.py`
- Lines: 245
- Capabilities: candidate/governance/forgetting, graph/relations
- Top functions: _now_iso, _clean_id, _relation_type, _scope_set, _is_hidden_lifecycle, _memory_exists, upsert_relation, backfill_supersedes_from_metadata, relation_type_counts, graph_relation_stats, as_json
- Related tests: `tests/test_graph_relation_backfill.py`, `tests/test_graph_relation_benchmark.py`

### `http_utils.py`
- Lines: 49
- Capabilities: provider hooks/runtime, security/secrets
- Top functions: _redact_match, redact_sensitive, chat_completions_endpoint

### `hygiene.py`
- Lines: 155
- Capabilities: provider hooks/runtime, candidate/governance/forgetting, experience/playbooks, vector/embedding, security/secrets, doctor/dashboard/observability
- Top functions: _limited, _preview, _vector_records, build_hygiene_report, build_provider_hygiene_report
- Related tests: `tests/test_capture_filters.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_entity_graph_hygiene.py`, `tests/test_graph_hygiene.py`, `tests/test_hygiene.py`, `tests/test_installer.py`, `tests/test_journal_digest.py`, `tests/test_legacy_hygiene_migration.py`, `tests/test_openclaw_import.py`, `tests/test_provider.py`

### `installer.py`
- Lines: 560
- Capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling, graph/relations, doctor/dashboard/observability
- Classes: InstallError
- Top functions: _platform_default_hermes_home, resolve_hermes_home, source_root, plugin_dir_for, _read_manifest_name, _read_manifest_version, _clear_runtime_verify_modules, _load_installed_package, _runtime_verify, _has_discovery_marker, _is_same_tree, _should_skip_entry, _copy_tree, _copy_existing_plugin, _remove_existing_plugin, _backup_stamp, _backup_existing_plugin, _validate_backup_dir
- Related tests: `tests/test_doctor_sqlite_readonly.py`, `tests/test_installer.py`, `tests/test_rollout_profiles.py`

### `journal.py`
- Lines: 1026
- Capabilities: install/rollout, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: _has_high_value_durable_signal, _low_value_promotion_reason, _workflow_continuation_tokens, _is_workflow_continuation, _metadata_entities, _find_match, _memory_scope_id, _record_journal_sources, _record_journal_rejection, _quarantine_journal_entries, _merge_metadata, _candidate_rejection_reason, _candidate_allowed, _cross_platform_metadata, apply_journal_candidates, _collect_journal_candidates, _scope_from_row, _infer_scope_from_journal
- Related tests: `tests/test_capture_filters.py`, `tests/test_config_schema.py`, `tests/test_dashboard.py`, `tests/test_digest_run_results.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_experience_evidence.py`, `tests/test_experience_promotion.py`, `tests/test_experience_replay.py`, `tests/test_experience_store.py`, `tests/test_experience_synthesis.py`

### `journal_candidates.py`
- Lines: 288
- Capabilities: journal/digest, candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, security/secrets
- Classes: JournalDigestCandidate
- Top functions: _unique, _entry_entities, _topic_entities, _topic_tags, _topic_label, _topic_signature, _segment_session_entries, _classify_target_and_type, _looks_like_historical_template_noise, _digest_role_summary, _heuristic_candidate_content, heuristic_journal_candidates, candidate_metadata
- Related tests: `tests/test_journal_candidates.py`, `tests/test_journal_digest.py`, `tests/test_journal_extractors.py`, `tests/test_v1015_audit_regressions.py`

### `journal_extractors.py`
- Lines: 268
- Capabilities: install/rollout, provider hooks/runtime, journal/digest, candidate/governance/forgetting, recall/ranking, graph/relations, external bridge/shared
- Classes: JournalCandidateList
- Top functions: _parse_entry_timestamp, _journal_session_bundles, _journal_from_digest_candidate, _parse_journal_llm_candidates, _runtime_config, _journal_runtime_config, _coerce_positive_int, _coerce_nonnegative_float, _config_bool, llm_journal_candidates
- Related tests: `tests/test_journal_digest.py`, `tests/test_journal_extractors.py`

### `journal_llm.py`
- Lines: 120
- Capabilities: journal/digest, recall/ranking, security/secrets, doctor/dashboard/observability
- Classes: JournalDigestLLMError
- Top functions: _active_call_llm, _quarantine_classification, _call_llm_with_retries
- Related tests: `tests/test_journal_candidates.py`, `tests/test_journal_digest.py`, `tests/test_journal_extractors.py`, `tests/test_journal_llm.py`, `tests/test_journal_store.py`, `tests/test_release.py`

### `journal_recovery.py`
- Lines: 339
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, schema/tooling, doctor/dashboard/observability
- Top functions: _now_iso, classify_rejection_reason, _prefix_clause, find_replay_candidates, recovery_report, schedule_replay, classify_recovery_candidates
- Related tests: `tests/test_journal_recovery.py`

### `journal_store.py`
- Lines: 389
- Capabilities: journal/digest, candidate/governance/forgetting, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets
- Classes: JournalEntry
- Top functions: _strip_inline_data_urls, _looks_like_base64_blob, _journal_entry_for_digest, ensure_journal_schema, _metadata_json, _journal_capture_allowed, _chunk_journal_text, _insert_journal_entry, append_journal_entry, _row_to_entry, load_unprocessed_journal_entries, mark_entries_processed, _journal_unprocessed_count, _prune_processed_journal
- Related tests: `tests/test_journal_candidates.py`, `tests/test_journal_extractors.py`, `tests/test_journal_store.py`

### `maintenance_ops.py`
- Lines: 49
- Capabilities: provider hooks/runtime, recall/ranking, external bridge/shared, doctor/dashboard/observability
- Top functions: effective_apply, memory_db_path, connect_memory_db, json_dumps_stable, now_utc_iso, make_batch_id
- Related tests: `tests/test_maintenance_ops.py`

### `memory_ops.py`
- Lines: 1579
- Capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: _scope_params, _scope_placeholders, _accessible_scope_params, _writable_scope_params, _normalized_scope_mode, _payload_entities, _rollback_provider_conn_after_error, store_memory_now, _conflict_peer_ids, _sync_conflict_metadata, _sync_conflict_metadata_for_ids, _mark_conflicts_for_memory, find_semantic_merge_candidate, _expected_scope_id_for_mode, _row_scope_mode, _target_scope_mode_for_existing, update_memory, merge_memories
- Related tests: `tests/test_governance_cleanup.py`, `tests/test_journal_digest.py`, `tests/test_provider.py`, `tests/test_v1015_audit_regressions.py`

### `memory_quality.py`
- Lines: 469
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, external bridge/shared, security/secrets, doctor/dashboard/observability
- Classes: MemoryQualityDecision
- Top functions: _load_metadata, load_quality_metadata, _row_value, _float_meta, _lifecycle, _memory_type, _metadata_ref_values, _metadata_refs, _is_archived, _is_active_profile_memory, _has_any, quality_decision_for_memory, quality_decision_summary, _looks_like_transcript, lint_memory_row, memory_quality_report
- Related tests: `tests/test_dashboard.py`, `tests/test_governance_cleanup.py`, `tests/test_memory_quality_kernel.py`, `tests/test_memory_quality_lint.py`, `tests/test_release.py`

### `migration.py`
- Lines: 47
- Capabilities: recall/ranking, schema/tooling
- Top functions: migrate_legacy_scope_recall_storage
- Related tests: `tests/test_capture_filters.py`, `tests/test_dashboard.py`, `tests/test_doctor_sqlite_readonly.py`, `tests/test_experience_schema.py`, `tests/test_installer.py`, `tests/test_journal_digest.py`, `tests/test_legacy_hygiene_migration.py`, `tests/test_openclaw_import.py`, `tests/test_release.py`, `tests/test_schema_migrations.py`

### `migration_openclaw.py`
- Lines: 474
- Capabilities: journal/digest, candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling, graph/relations, security/secrets, doctor/dashboard/observability
- Top functions: now_iso, _clean_targets, sanitize_snippet, lint_openclaw_content, looks_like_raw_transcript, _metadata_path, lint_openclaw_metadata, redact_openclaw_metadata, map_openclaw_row, build_import_plan, ensure_import_ledger_schema, backup_sqlite, file_sha256, backup_receipt, graph_repair_receipt, _metadata_for_import, _row_receipt, import_mapped_rows
- Related tests: `tests/test_openclaw_import.py`

### `models.py`
- Lines: 132
- Capabilities: provider hooks/runtime, journal/digest, vector/embedding, recall/ranking, graph/relations, external bridge/shared
- Classes: RecallItem, RuntimeScope, ImportedMemoryRow, VectorIndexRecord
- Top functions: recall_scope_mode, json_dumps_stable, normalize_import_timestamp, normalize_import_fingerprint_timestamp, build_import_fingerprint
- Related tests: `tests/test_doctor_experience.py`, `tests/test_experience_promotion.py`, `tests/test_experience_schema.py`, `tests/test_experience_store.py`, `tests/test_experience_synthesis.py`, `tests/test_fact_freshness.py`, `tests/test_governance_contract_regressions.py`, `tests/test_governance_scheduler.py`, `tests/test_journal_digest.py`, `tests/test_journal_extractors.py`, `tests/test_journal_store.py`, `tests/test_legacy_hygiene_migration.py`

### `nightly_digest.py`
- Lines: 1399
- Capabilities: install/rollout, provider hooks/runtime, journal/digest, candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Classes: MessageRecord, SessionBundle, DigestCandidate, ScopeProfile, DigestOptions, DigestVectorRuntime
- Top functions: _profile_writable_scope_ids, _memory_scope_id, redact_sensitive, _redact_match, parse_date, local_day_bounds, resolve_session_db, _column_names, _read_session_meta, _table_names, load_session_bundles, parse_tool_calls, summarize_command, safe_command_hints, unique_strings, session_chunks, bundle_artifact_anchor_block, heuristic_candidates
- Related tests: `tests/test_dashboard.py`, `tests/test_digest_run_results.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_modularization.py`, `tests/test_nightly_digest.py`, `tests/test_nightly_llm.py`, `tests/test_release.py`, `tests/test_v1015_audit_regressions.py`

### `nightly_llm.py`
- Lines: 588
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, recall/ranking, schema/tooling, security/secrets, doctor/dashboard/observability
- Top functions: config_bool_value, normalize_digest_api_mode, load_dotenv, resolve_api_key, resolve_hermes_credential_pool_token, _dict_child, resolve_llm_config, codex_cloudflare_headers, responses_endpoint, anthropic_messages_endpoint, response_item_get, extract_responses_text, extract_responses_sse_text, decode_responses_body, call_chat_completions_llm, call_codex_responses_llm, extract_anthropic_messages_text, call_anthropic_messages_llm
- Related tests: `tests/test_journal_digest.py`, `tests/test_journal_llm.py`, `tests/test_nightly_llm.py`

### `prompting.py`
- Lines: 93
- Capabilities: provider hooks/runtime, recall/ranking
- Top functions: render_current_turn_recall, _should_attempt_recall, _drop_recently_recalled, _select_recall_items, _fit_summary

### `provider.py`
- Lines: 1069
- Capabilities: install/rollout, provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Classes: ScopeRecallMemoryProvider
- Top functions: register
- Related tests: `tests/test_audit_regressions.py`, `tests/test_benchmark_regression_cases.py`, `tests/test_capture_filters.py`, `tests/test_capture_llm_manual.py`, `tests/test_config_schema.py`, `tests/test_conflict_governance.py`, `tests/test_dashboard.py`, `tests/test_digest_run_results.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_sqlite_readonly.py`, `tests/test_embedders.py`

### `provider_schemas.py`
- Lines: 151
- Capabilities: provider hooks/runtime, candidate/governance/forgetting, experience/playbooks, recall/ranking, schema/tooling, graph/relations, security/secrets, doctor/dashboard/observability
- Top functions: build_config_schema, _schema_profile, _extra_tool_names, build_tool_schemas
- Related tests: `tests/test_config_schema.py`, `tests/test_provider_schemas.py`

### `recall.py`
- Lines: 808
- Capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, external bridge/shared
- Classes: RecallService
- Top functions: _recall_lifecycle_visible_sql
- Related tests: `tests/test_audit_regressions.py`, `tests/test_benchmark_regression_cases.py`, `tests/test_capture_filters.py`, `tests/test_capture_llm_manual.py`, `tests/test_config_schema.py`, `tests/test_conflict_governance.py`, `tests/test_dashboard.py`, `tests/test_digest_run_results.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_doctor_secret_scan.py`

### `recall_pipeline.py`
- Lines: 126
- Capabilities: candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, doctor/dashboard/observability
- Classes: RecallSearchPlan
- Top functions: positive_int, build_search_plan, initial_trace, recall_dedup_key, merge_recall_candidates, rank_recall_items, final_trace_payload
- Related tests: `tests/test_recall_pipeline.py`

### `relation_extraction.py`
- Lines: 394
- Capabilities: candidate/governance/forgetting, recall/ranking, graph/relations, external bridge/shared, doctor/dashboard/observability
- Top functions: _clean_scope_ids, _load_metadata, _memory_rows, _row_payload, _existing_relation_types, _pair_has_relation, _pair_has_contradiction, _pair_key, _delete_generated_relation_edges_for_pairs, _same_topic, _supersedes, _entity_pattern, _trigger_mentions_entity, _typed_relation, _candidate, extract_relation_candidates, _relation_candidate_scan, rebuild_extracted_relations
- Related tests: `tests/test_provider.py`, `tests/test_relation_extraction.py`

### `response_schemas.py`
- Lines: 35
- Capabilities: candidate/governance/forgetting, experience/playbooks, schema/tooling, doctor/dashboard/observability
- Top functions: response_schema_version
- Related tests: `tests/test_release.py`

### `schemas.py`
- Lines: 560
- Capabilities: provider hooks/runtime, journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Related tests: `tests/test_audit_regressions.py`, `tests/test_config_schema.py`, `tests/test_experience_config_defaults.py`, `tests/test_experience_tools.py`, `tests/test_external_shared_memory_examples.py`, `tests/test_provider.py`, `tests/test_provider_schemas.py`, `tests/test_release.py`, `tests/test_roadmap_observability.py`, `tests/test_tool_hygiene.py`

### `scope.py`
- Lines: 252
- Capabilities: graph/relations, external bridge/shared
- Top functions: _scope_component, _truthy, _identity_config, _legacy_identities_config, normalize_scope_identity, _account_key, _identity_enabled, _canonical_user_for_account, _accounts_for_canonical, canonical_user_id, build_shared_scope_id, build_shared_pool_scope_id, build_scope_id, writable_scope_ids, accessible_scope_ids
- Related tests: `tests/test_audit_regressions.py`, `tests/test_benchmark_regression_cases.py`, `tests/test_capture_filters.py`, `tests/test_capture_llm_manual.py`, `tests/test_config_schema.py`, `tests/test_conflict_governance.py`, `tests/test_dashboard.py`, `tests/test_digest_run_results.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_doctor_secret_scan.py`

### `scope_recall.py`
- Lines: 30
- Capabilities: recall/ranking, schema/tooling
- Related tests: `tests/test_audit_regressions.py`, `tests/test_benchmark_regression_cases.py`, `tests/test_capture_filters.py`, `tests/test_config_schema.py`, `tests/test_conflict_governance.py`, `tests/test_dashboard.py`, `tests/test_digest_run_results.py`, `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_modularization.py`, `tests/test_doctor_secret_scan.py`, `tests/test_doctor_sqlite_readonly.py`

### `scoring.py`
- Lines: 169
- Capabilities: candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations, doctor/dashboard/observability
- Top functions: _canonical_tokens, lexical_score, bm25_to_score, semantic_similarity, combine_scores, reciprocal_rank_fusion
- Related tests: `tests/test_experience_preflight.py`, `tests/test_experience_quality.py`, `tests/test_release.py`, `tests/test_retrieval_policy.py`, `tests/test_retrieval_rrf_graph.py`, `tests/test_roadmap_retrieval.py`, `tests/test_scoring.py`

### `secret_index.py`
- Lines: 119
- Capabilities: journal/digest, vector/embedding, recall/ranking, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: _clean_field, _string_list, _secret_type, _fingerprint, build_secret_index
- Related tests: `tests/test_capture_filters.py`, `tests/test_provider.py`, `tests/test_provider_schemas.py`, `tests/test_release.py`, `tests/test_tool_hygiene.py`

### `sql_store.py`
- Lines: 966
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Top functions: _schema_migration_checksum, ensure_schema_migrations, _row_to_dict, schema_migration_status, ensure_schema, ensure_governance_schema, _require_governance_audit_schema, _redact_governance_payload, record_governance_audit_event, ensure_experience_schema, _add_memory_column, ensure_memory_columns, _fts_counts, fts_integrity_report, reconcile_fts_index, rebuild_fts_if_empty, now_iso, store_row
- Related tests: `tests/test_doctor_experience.py`, `tests/test_doctor_journal_health.py`, `tests/test_doctor_secret_scan.py`, `tests/test_doctor_sqlite_readonly.py`, `tests/test_entity_graph_hygiene.py`, `tests/test_experience_bootstrap.py`, `tests/test_experience_preflight.py`, `tests/test_experience_promotion.py`, `tests/test_experience_replay.py`, `tests/test_experience_replay_generation.py`, `tests/test_experience_schema.py`, `tests/test_experience_store.py`

### `sqlite_vector_store.py`
- Lines: 274
- Capabilities: journal/digest, candidate/governance/forgetting, vector/embedding, recall/ranking, schema/tooling
- Classes: SQLiteBruteForceVectorStore
- Related tests: `tests/test_optional_vector_deps.py`, `tests/test_release.py`, `tests/test_report_hygiene_script.py`, `tests/test_sqlite_vector_store.py`

### `storage_views.py`
- Lines: 334
- Capabilities: provider hooks/runtime, candidate/governance/forgetting, vector/embedding, recall/ranking, graph/relations
- Top functions: _recall_lifecycle_visible_sql, _scope_placeholders, _accessible_scope_params, _alias_like_terms, _row_metadata, search_db_memories, search_vector_memories, _curated_memory_allowed, search_curated_memories
- Related tests: `tests/test_retrieval_policy.py`, `tests/test_storage_views.py`

### `task_boundary.py`
- Lines: 223
- Capabilities: journal/digest, candidate/governance/forgetting, experience/playbooks, doctor/dashboard/observability
- Classes: TaskClosure
- Top functions: _entry_value, entry_text, tail_text, goal_signal_key, is_low_signal_goal, has_failure_signal, has_uncertain_signal, contains_any, extract_final_evidence, classify_task_closure
- Related tests: `tests/test_experience_replay_generation.py`, `tests/test_release.py`, `tests/test_task_boundary.py`

### `tooling.py`
- Lines: 916
- Capabilities: install/rollout, provider hooks/runtime, candidate/governance/forgetting, experience/playbooks, vector/embedding, recall/ranking, schema/tooling, graph/relations, external bridge/shared, security/secrets, doctor/dashboard/observability
- Classes: ScopeRecallToolService
- Top functions: _now_iso
- Related tests: `tests/test_tool_hygiene.py`

### `vector_runtime.py`
- Lines: 337
- Capabilities: install/rollout, provider hooks/runtime, vector/embedding, recall/ranking, schema/tooling, graph/relations, security/secrets, doctor/dashboard/observability
- Top functions: _vector_mutation_lock, mark_vector_needs_repair, _normalize_vector_backend, _append_vector_message, _open_sqlite_vector_store, _open_vector_store, setup_vector_layer, refresh_vector_audit, _should_index_target, sync_vector_index, upsert_vector_record
- Related tests: `tests/test_nightly_digest.py`, `tests/test_optional_vector_deps.py`, `tests/test_sqlite_vector_store.py`, `tests/test_vector_policy.py`

### `vector_store.py`
- Lines: 314
- Capabilities: install/rollout, provider hooks/runtime, vector/embedding, schema/tooling, graph/relations
- Classes: LanceVectorStore
- Top functions: _trim_probe_output, _probe_native_vector_dependencies, native_vector_dependency_status, _optional_lancedb, _optional_pyarrow, _sql_quote
- Related tests: `tests/test_forgetting.py`, `tests/test_governance_cleanup.py`, `tests/test_hygiene.py`, `tests/test_nightly_digest.py`, `tests/test_optional_vector_deps.py`, `tests/test_provider.py`, `tests/test_release.py`, `tests/test_report_hygiene_script.py`, `tests/test_retrieval_policy.py`, `tests/test_sqlite_vector_store.py`, `tests/test_v1015_audit_regressions.py`, `tests/test_vector_policy.py`

## Pre-slice duplicate/conflict scan terms

- `event_digest`: no current implementation hit
- `candidate_extraction`: no current implementation hit
- `skill_bridge`: `docs/internal.module-map.md`
- `VectorStore`: `provider.py`, `sqlite_vector_store.py`, `vector_runtime.py`, `vector_store.py`, `docs/internal.module-map.md`, `scripts/repair.vector_index.py`
- `pgvector`: no current implementation hit
- `qdrant`: no current implementation hit
- `chroma`: no current implementation hit
- `textual`: no current implementation hit
- `memory browser`: no current implementation hit
- `external_bridge`: `docs/external-shared-memory.md`, `scripts/check.release.py`
- `postgres_bridge`: no current implementation hit

## Next landing zones from master plan

- P1 install activation: `installer.py`, `cli.py`, `tests/test_installer.py`, `tests/test_rollout_profiles.py`, docs.
- P2 event candidates: new `event_digest.py`, new `candidate_extraction.py`, thin `provider.py` wiring, doctor/dashboard tests.
- P3 skill bridge: new `skill_bridge.py`, CLI route, tests; do not call `skill_manage` automatically.
- P4 vector abstraction: `vector_store.py`, `sqlite_vector_store.py`, `vector_runtime.py`, new `pgvector_store.py` optional backend.
- P5 governance browser: new CLI/browser module first; TUI optional extra later.
- P6 shared bridge/security: new `external_bridge.py`, optional `postgres_bridge.py`, docs and secret/audit tests.
