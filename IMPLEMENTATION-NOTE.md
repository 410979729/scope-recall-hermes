# IMPLEMENTATION-NOTE — Scope Recall 3.0 profile/entity read views

Candidate checkout: `F:/T/sr-profile-entity-20260911`. Package version remains **3.0.0**; protocol remains **1.1**. This note records worker implementation only. It does **not** claim parent acceptance, packaging, publication, production activation, historical migration, or install into the daily interpreter.

## Changed files

### New

- `core/read_views.py` — shared read-only service (`read_profile`, `read_entity`, project-alias resolve, bounds, epoch fail-closed)
- `contracts/profile_request.schema.json`
- `contracts/entity_request.schema.json`
- `contracts/profile_view.schema.json`
- `contracts/entity_view.schema.json`
- `tests/contract/test_v11_profile_entity.py`
- `tests/host/hermes/test_profile_entity_tools.py`
- `tests/host/codex/test_profile_entity_mcp.py`
- `docs/profile-entity.zh-CN.md`

### Modified

- `core/composition.py` — public `MemoryCore.profile` / `MemoryCore.entity`
- `core/claim_storage.py` — optional `kind` / `value_text` / `alias_name` on `list_refs`; default SQL unchanged when those filters are omitted
- `contracts.py` — schema / model-request registration; `_validate_read_view_honesty`
- `adapters/runtime_wiring.py` — `READ_VIEW_BUDGET_GUIDANCE` (UTF-8 byte budget, not tokenizer counts)
- `adapters/hermes/provider.py` — tools `profile` / `entity` in frozen order; thin dispatch; host-base fallback also treats `PermissionError`
- `adapters/codex/mcp_server.py` — same tools, `readOnlyHint`, generated-model `extra=forbid`
- `packaging/v11-module-allowlist.json` — new runtime module + four schemas
- `README.md` — short “Profile and entity read views” section
- `tests/host/hermes/test_operator_tools.py` — frozen tool names
- `tests/host/codex/test_mcp.py` — public MCP tool list / stdio assertions

Unrelated historical docs and `tests/host/hermes/test_source_context_provenance.py` were not rewritten.

## Design decisions

1. **One Core service, thin hosts.** Domain SQL, effective-version selection, alias revalidation, suppression / evidence liveness, and `release_objects` live in Core. Hermes and Codex only bind trusted initialized identity, fill defaults, reject unknown fields, and fail closed if `memory_epoch` changes before envelope delivery.
2. **Current facts only.** `select_effective` plus non-suppressed, live evidence, and `allowed(..., automatic=False)`. `proposed` / superseded / retracted / deleted are not current. `disputed` is a labeled section (profile) or `temporal_status` (entity). Pending intentions are isolated; completed / cancelled / expired are not current work.
3. **Entity is exact one-hop.** `probe` = outgoing current `fact`. `related` = recorded triples. Incoming matches **full scalar `value_text`** (queried name, plus canonical subject only after an unambiguous alias resolve). No co-occurrence, substring, comma-list, or paraphrase edges. `object_text` is not a canonical entity id.
4. **Aliases stay on the existing project-name contract.** Reuse admitted `kind=alias` only; re-run `validate_alias_target` / `validate_alias_source` and evidence liveness on both alias and target. Ambiguous names return `alias_ambiguous` with empty body. Literal identifiers are not silently normalized. Person aliases are not generalized.
5. **Bounds and honesty.** Defaults `max_items=16`, `budget_tokens=4096`. Candidate enumeration cap 200. Budget is UTF-8 bytes of the canonical serialized structured result (including provenance), matching existing conservative convention. `scan_capped` never yields `coverage=complete_for_query`. `no_match` is not global absence. Empty unconsolidated chat reports `consolidation_required` and does not echo raw text.
6. **Fail closed on authority change.** Release fence uses `release_objects(..., automatic=False, history=False)`, then rechecks suppression, evidence, and epoch. `VERSION_CONFLICT` / `SOURCE_MISSING` → `unavailable` without stale profile / edge / alias text.
7. **`list_refs` extension is additive.** Join-on-current-revision is used only when `value_text` or `alias_name` is supplied, so existing mutate / lookup SQL stays byte-stable for the old call shape.
8. **Hermes host base under TEST.** `_memory_provider_base` already fell back to `PublicMemoryProvider` on `ImportError`. The mandated interpreter has an editable `agent` tree at `F:/Agents/runtime/windows/hermes-yuheng/hermes-agent`, which `v11_guard` correctly refuses to read. `PermissionError` is treated as “host base unavailable” so isolated tests do not open the daily install. Installed Hermes without the audit hook still subclasses the real `MemoryProvider`.

## Focused commands run and results

Interpreter: `F:/Agents/runtime/windows/hermes-yuheng/hermes-agent/venv/Scripts/python.exe -X utf8 -B` (no `-I`; `PYTHONPATH` must reach `v11_guard`). Isolation matches `scripts/check.py`: whitelist env, `SCOPE_RECALL_TEST_BOUNDARY_PARENT=C:\Temp\sr\TEST-pe-20260911`, unused `HERMES_HOME` / `HOME` under that parent, `v11_guard` + `no:cacheprovider`. Launcher lived only under the isolated TEST directory (`C:/Temp/sr/TEST-pe-20260911/run_focused.py`). No `check.py` (it writes verification receipts and calls `git rev-parse`). No full suite, packaging build, benchmark, or model evaluation.

```
python -m compileall -q core/read_views.py core/claim_storage.py core/composition.py
  contracts.py adapters/runtime_wiring.py adapters/hermes/provider.py
  adapters/codex/mcp_server.py tests/contract/test_v11_profile_entity.py
  tests/host/hermes/test_profile_entity_tools.py
  tests/host/codex/test_profile_entity_mcp.py
```

Result: `COMPILE_OK`.

```
pytest tests/contract/test_v11_profile_entity.py
  tests/host/hermes/test_profile_entity_tools.py
  tests/host/hermes/test_operator_tools.py::test_operator_tools_expose_frozen_names_and_strict_boundary
  tests/host/codex/test_profile_entity_mcp.py
  -q --tb=short --import-mode=importlib -p v11_guard -p no:cacheprovider
```

Result: **10 passed in 3.97s** (7 contract + 3 host). Earlier isolated contract-only run after the `ClaimVersion` dedupe fix: **7 passed in 1.84s**.

Covered: capture + accept → profile grouping; forward / inverse one-hop; correction replaces old value; delete removes profile fact and reverse relation; project / branch negatives; same-name alias ambiguity; person alias not resolved; no comma-list inference; stored `source_contexts`; invalid tool / Core arguments (unknown field, bool-as-int, enum, protocol, path); truncation / budget honesty; reads do not call `storage.write` or touch vectors; Hermes dispatch; in-process MCP registration, `readOnlyHint`, `extra=forbid`, Core parity.

## Known gaps (not claimed done)

- Continuation / cursor for truncated pages is not implemented; bounded honest partial is the slice.
- `tests/host/codex/test_mcp.py` stdio subprocess cases were updated for tool names but **not executed** by this worker.
- Existing `source_context` regression, full alias / claims / packaging / host suites were not re-run.
- `probe` returns current **facts** only; preferences appear under `related`, not `probe`.
- Incoming still cannot be invented from lists or paraphrases (intentional).
- No deployment, wheel, production DB, or parent sign-off.
