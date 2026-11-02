# Configuration

This reference is generated from runtime definitions and the setup parser.
Regenerate it with `python scripts/generate_configuration_docs.py --write`; `--check` detects drift.

Setup preserves existing routes and grants. A new installation has no model routes and zero model budget.
Model credentials are environment variable names. Never put credential values in a route file.

## Setup options

| Option | Meaning |
|---|---|
| `--host` | Host to configure; repeat to select several. Default: discovered hosts. |
| `--home` | Host home override; requires exactly one host. |
| `--store` | Create or join this shared store. Default: the existing attachment or ~/.scope-recall/shared. |
| `--share-with` | Copy owner grants from this registered shared entry when grants differ. |
| `--project` | Project directory to bind or inspect. Default: current directory. |
| `--python` | Override the interpreter; default: a host venv, then setup's Python. |
| `--configure-models` | Guide endpoint, model, environment reference, pricing and budget choices before confirmation. |
| `--models` | JSON with auxiliary routes, budget and vector configuration; credentials must be environment references. |
| `--worker` | Use bounded host-triggered workers, or print a manual command; no system service is registered. |
| `--dry-run` | Print the plan without writing or running checks. |
| `--plan-json` | Print the plan as JSON; combine with --dry-run for automation. |
| `--yes` | Apply the displayed plan without an interactive confirmation. |
| `--register-project-copy` | Explicitly approve the matching project copy shown by setup; --yes alone never grants it. |
| `--test-mode` | Bind synthetic test installations; never use with a personal store. |

## RuntimeInstanceConfig

| Field | Type | Default |
|---|---|---|
| <a id="binding"></a>`binding` | `InstanceBinding` | `required` |
| <a id="session_id"></a>`session_id` | `str` | `required` |
| <a id="allowed_scope_ids"></a>`allowed_scope_ids` | `frozenset[str]` | `required` |
| <a id="actor_origin"></a>`actor_origin` | `Origin` | `"human_direct"` |
| <a id="project_id"></a>`project_id` | `str &#124; None` | `null` |
| <a id="branch_id"></a>`branch_id` | `str &#124; None` | `null` |
| <a id="host_adapter"></a>`host_adapter` | `str &#124; None` | `null` |
| <a id="owner_id"></a>`owner_id` | `str` | `"scope-recall-worker"` |
| <a id="request_seconds"></a>`request_seconds` | `float` | `45.0` |
| <a id="model_request_seconds"></a>`model_request_seconds` | `float &#124; None` | `null` |
| <a id="drain_seconds"></a>`drain_seconds` | `float` | `120.0` |
| <a id="auto_recall_seconds"></a>`auto_recall_seconds` | `float` | `5.0` |
| <a id="hook_processing_seconds"></a>`hook_processing_seconds` | `float` | `6.0` |
| <a id="max_items"></a>`max_items` | `int` | `32` |
| <a id="lease_seconds"></a>`lease_seconds` | `float` | `60.0` |
| <a id="auxiliary"></a>`auxiliary` | `AuxiliaryRuntimeConfig &#124; None` | `null` |
| <a id="vector"></a>`vector` | `VectorRuntimeConfig &#124; None` | `null` |
| <a id="vector_threshold"></a>`vector_threshold` | `float &#124; None` | `null` |
| <a id="auto_lexical_min_coverage"></a>`auto_lexical_min_coverage` | `float` | `0.2` |
| <a id="auto_lexical_min_terms"></a>`auto_lexical_min_terms` | `int` | `2` |
| <a id="auto_vector_threshold"></a>`auto_vector_threshold` | `float` | `0.3` |
| <a id="owner_names"></a>`owner_names` | `tuple[str, ...]` | `()` |
| <a id="daily_work_limit"></a>`daily_work_limit` | `int` | `0` |
| <a id="auto_retry_cooldown_seconds"></a>`auto_retry_cooldown_seconds` | `float` | `3600.0` |
| <a id="max_auto_recoveries"></a>`max_auto_recoveries` | `int` | `2` |
| <a id="worker_min_interval_seconds"></a>`worker_min_interval_seconds` | `float` | `30.0` |
| <a id="supervisor_enabled"></a>`supervisor_enabled` | `bool` | `true` |
| <a id="supervisor_seconds"></a>`supervisor_seconds` | `float` | `21600.0` |
| <a id="supervisor_max_drains"></a>`supervisor_max_drains` | `int` | `256` |
| <a id="storage_budget_bytes"></a>`storage_budget_bytes` | `int` | `0` |
| <a id="resident_recall_minutes"></a>`resident_recall_minutes` | `int &#124; None` | `null` |

## VectorRuntimeConfig

| Field | Type | Default |
|---|---|---|
| <a id="backend"></a>`backend` | `str` | `required` |
| <a id="storage_dir"></a>`storage_dir` | `Path` | `required` |
| <a id="table_name"></a>`table_name` | `str` | `required` |
| <a id="dimensions"></a>`dimensions` | `int` | `required` |
| <a id="metric"></a>`metric` | `str` | `"cosine"` |
| <a id="test_injection_override"></a>`test_injection_override` | `bool` | `false` |
| <a id="tool_output_retention_days"></a>`tool_output_retention_days` | `int` | `180` |

## AuxiliaryRuntimeConfig

| Field | Type | Default |
|---|---|---|
| <a id="external_embedding"></a>`external_embedding` | `bool` | `required` |
| <a id="external_consolidation"></a>`external_consolidation` | `bool` | `required` |
| <a id="installation_dir"></a>`installation_dir` | `Path &#124; None` | `required` |
| <a id="ledger_path"></a>`ledger_path` | `Path &#124; None` | `required` |
| <a id="budget"></a>`budget` | `BudgetPolicy` | `required` |
| <a id="embedding"></a>`embedding` | `EmbeddingRouteConfig &#124; None` | `required` |
| <a id="consolidation"></a>`consolidation` | `ConsolidationRouteConfig &#124; ResponsesRouteConfig &#124; CodexCliRouteConfig &#124; None` | `required` |
| <a id="consolidation_reserve_input"></a>`consolidation_reserve_input` | `int` | `required` |

## BudgetPolicy

| Field | Type | Default |
|---|---|---|
| <a id="batch"></a>`batch` | `str` | `"auxiliary"` |
| <a id="cap_micro_usd"></a>`cap_micro_usd` | `int &#124; None` | `0` |
| <a id="total_input_cap"></a>`total_input_cap` | `int &#124; None` | `0` |
| <a id="total_output_cap"></a>`total_output_cap` | `int &#124; None` | `0` |
| <a id="total_call_cap"></a>`total_call_cap` | `int &#124; None` | `0` |
| <a id="max_request_bytes"></a>`max_request_bytes` | `int` | `128000` |
| <a id="default_reserve_input"></a>`default_reserve_input` | `int` | `32768` |
| <a id="default_reserve_output"></a>`default_reserve_output` | `int` | `4096` |
| <a id="model_reserve_output"></a>`model_reserve_output` | `Mapping[str, int]` | `{'mimo-v2.5': 131072}` |
| <a id="model_token_caps"></a>`model_token_caps` | `Mapping[str, tuple[int &#124; None, int &#124; None]]` | `{}` |
| <a id="pricing"></a>`pricing` | `Mapping[str, ModelPricing]` | `{}` |
| <a id="approved_models"></a>`approved_models` | `frozenset[str]` | `[]` |
| <a id="period"></a>`period` | `str` | `"lifetime"` |
| <a id="time_zone"></a>`time_zone` | `str` | `"UTC"` |

## EmbeddingRouteConfig

| Field | Type | Default |
|---|---|---|
| <a id="credential_env"></a>`credential_env` | `str` | `required` |
| <a id="model"></a>`model` | `str &#124; None` | `null` |
| <a id="endpoint"></a>`endpoint` | `str &#124; None` | `null` |
| `dimensions` | `int &#124; None` | `null` |
| <a id="dialect"></a>`dialect` | `str &#124; None` | `null` |
| <a id="dimensions_field"></a>`dimensions_field` | `str` | `"dimensions"` |
| <a id="allow_insecure_endpoint"></a>`allow_insecure_endpoint` | `bool` | `false` |

## ConsolidationRouteConfig

| Field | Type | Default |
|---|---|---|
| `model` | `str` | `required` |
| `endpoint` | `str` | `required` |
| `credential_env` | `str` | `required` |
| <a id="output_limit_field"></a>`output_limit_field` | `str` | `required` |
| <a id="max_output_tokens"></a>`max_output_tokens` | `int` | `required` |
| <a id="thinking"></a>`thinking` | `Mapping[str, str] &#124; None` | `null` |
| <a id="response_format"></a>`response_format` | `Mapping[str, str] &#124; None` | `null` |
| <a id="reasoning_effort"></a>`reasoning_effort` | `str &#124; None` | `null` |
| <a id="stream"></a>`stream` | `bool` | `false` |
| <a id="n"></a>`n` | `int` | `1` |
| <a id="headers"></a>`headers` | `Mapping[str, str] &#124; None` | `null` |
| `allow_insecure_endpoint` | `bool` | `false` |
| <a id="temperature"></a>`temperature` | `float &#124; None` | `null` |

## ResponsesRouteConfig

| Field | Type | Default |
|---|---|---|
| `model` | `str` | `required` |
| `endpoint` | `str` | `required` |
| `credential_env` | `str` | `required` |
| `max_output_tokens` | `int` | `required` |
| `reasoning_effort` | `str &#124; None` | `null` |
| <a id="text_format"></a>`text_format` | `Mapping[str, str] &#124; None` | `null` |
| `stream` | `bool` | `false` |
| <a id="kind"></a>`kind` | `str` | `"openai_responses"` |
| `allow_insecure_endpoint` | `bool` | `false` |
| `temperature` | `float &#124; None` | `null` |

## CodexCliRouteConfig

| Field | Type | Default |
|---|---|---|
| <a id="executable"></a>`executable` | `Path` | `required` |
| <a id="executable_sha256"></a>`executable_sha256` | `str` | `required` |
| `model` | `str` | `"gpt-5.6-luna"` |
| `kind` | `str` | `"codex_cli"` |
| <a id="subscription_budget"></a>`subscription_budget` | `SubscriptionBudgetPolicy` | `SubscriptionBudgetPolicy(daily_calls=4, daily_input_tokens=131072, daily_output_tokens=32768, reserve_input=32768, reserve_output=8192, max_request_bytes=24576)` |
