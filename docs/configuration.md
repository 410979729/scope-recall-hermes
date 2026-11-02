# Configuration

This reference is generated from runtime input definitions and their readers.
Regenerate with `python scripts/generate_configuration_docs.py --write`; `--check` detects drift.

Setup preserves existing routes and grants. New installations have no model routes and zero model budget.
Credentials are environment variable references. Never put their values in a route file.

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

| Field | JSON input type | Default when omitted |
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
| <a id="session_brief_enabled"></a>`session_brief_enabled` | `bool` | `true` |
| <a id="session_brief_tokens"></a>`session_brief_tokens` | `int` | `1024` |
| <a id="max_items"></a>`max_items` | `int` | `32` |
| <a id="lease_seconds"></a>`lease_seconds` | `float` | `60.0` |
| <a id="auxiliary"></a>`auxiliary` | `AuxiliaryRuntimeConfig &#124; None` | `{"external_embedding": false, "external_consolidation": false, "installation_dir": null, "ledger_path": null, "budget": {"batch": "auxiliary", "cap_micro_usd": 0, "total_input_cap": 0, "total_output_cap": 0, "total_call_cap": 0, "max_request_bytes": 128000, "default_reserve_input": 32768, "default_reserve_output": 4096, "model_reserve_output": {"mimo-v2.5": 131072}, "model_token_caps": {}, "pricing": {}, "approved_models": [], "period": "lifetime", "time_zone": "UTC"}, "embedding": null, "consolidation": null, "consolidation_reserve_input": 32768}` |
| <a id="vector"></a>`vector` | `VectorRuntimeConfig &#124; None` | `null` |
| <a id="vector_threshold"></a>`vector_threshold` | `float &#124; None` | `null` |
| <a id="auto_lexical_min_coverage"></a>`auto_lexical_min_coverage` | `float` | `0.2` |
| <a id="auto_lexical_min_terms"></a>`auto_lexical_min_terms` | `int` | `2` |
| <a id="auto_vector_threshold"></a>`auto_vector_threshold` | `float` | `0.3` |
| <a id="owner_names"></a>`owner_names` | `tuple[str, ...]` | `[]` |
| <a id="daily_work_limit"></a>`daily_work_limit` | `int` | `0` |
| <a id="auto_retry_cooldown_seconds"></a>`auto_retry_cooldown_seconds` | `float` | `3600.0` |
| <a id="max_auto_recoveries"></a>`max_auto_recoveries` | `int` | `2` |
| <a id="worker_min_interval_seconds"></a>`worker_min_interval_seconds` | `float` | `30.0` |
| <a id="supervisor_enabled"></a>`supervisor_enabled` | `bool` | `true` |
| <a id="supervisor_seconds"></a>`supervisor_seconds` | `float` | `21600.0` |
| <a id="supervisor_max_drains"></a>`supervisor_max_drains` | `int` | `256` |
| <a id="storage_budget_bytes"></a>`storage_budget_bytes` | `int` | `0` |
| <a id="recall_log_days"></a>`recall_log_days` | `int` | `30` |
| <a id="recall_log_bytes"></a>`recall_log_bytes` | `int` | `33554432` |
| <a id="recall_log_queue"></a>`recall_log_queue` | `int` | `128` |
| <a id="resident_recall_minutes"></a>`resident_recall_minutes` | `int &#124; None` | `null` |

## InstanceBinding

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="agent_id"></a>`agent_id` | `str` | `required` |
| <a id="installation_id"></a>`installation_id` | `str` | `required` |
| <a id="data_directory"></a>`data_directory` | `Path` | `required` |
| <a id="scope_ids"></a>`scope_ids` | `frozenset[str]` | `required` |
| <a id="test_mode"></a>`test_mode` | `bool` | `false` |
| <a id="installation_kind"></a>`installation_kind` | `str` | `"local"` |

## VectorRuntimeConfig

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="backend"></a>`backend` | `str` | `"sqlite"` |
| <a id="storage_dir"></a>`storage_dir` | `Path` | `required` |
| <a id="table_name"></a>`table_name` | `str` | `required` |
| <a id="dimensions"></a>`dimensions` | `int` | `required` |
| <a id="metric"></a>`metric` | `str` | `"cosine"` |
| <a id="test_injection_override"></a>`test_injection_override` | `bool` | `false` |
| <a id="tool_output_retention_days"></a>`tool_output_retention_days` | `int` | `180` |

## AuxiliaryRuntimeConfig

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="external_embedding"></a>`external_embedding` | `bool` | `required` |
| <a id="external_consolidation"></a>`external_consolidation` | `bool` | `required` |
| <a id="installation_dir"></a>`installation_dir` | `Path &#124; None` | `null` |
| <a id="ledger_path"></a>`ledger_path` | `Path &#124; None` | `null` |
| <a id="budget"></a>`budget` | `BudgetPolicy` | `{"batch": "auxiliary", "cap_micro_usd": 0, "total_input_cap": 0, "total_output_cap": 0, "total_call_cap": 0, "max_request_bytes": 128000, "default_reserve_input": 32768, "default_reserve_output": 4096, "model_reserve_output": {"mimo-v2.5": 131072}, "model_token_caps": {}, "pricing": {}, "approved_models": [], "period": "lifetime", "time_zone": "UTC"}` |
| <a id="embedding"></a>`embedding` | `EmbeddingRouteConfig &#124; None` | `null` |
| <a id="consolidation"></a>`consolidation` | `ConsolidationRouteConfig &#124; ResponsesRouteConfig &#124; CodexCliRouteConfig &#124; None` | `null` |
| <a id="consolidation_reserve_input"></a>`consolidation_reserve_input` | `int` | `32768` |

## BudgetPolicy

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="batch"></a>`batch` | `str` | `"auxiliary"` |
| <a id="cap_micro_usd"></a>`cap_micro_usd` | `int &#124; None` | `0` |
| <a id="total_input_cap"></a>`total_input_cap` | `int &#124; None` | `0` |
| <a id="total_output_cap"></a>`total_output_cap` | `int &#124; None` | `0` |
| <a id="total_call_cap"></a>`total_call_cap` | `int &#124; None` | `0` |
| <a id="max_request_bytes"></a>`max_request_bytes` | `int` | `128000` |
| <a id="default_reserve_input"></a>`default_reserve_input` | `int` | `32768` |
| <a id="default_reserve_output"></a>`default_reserve_output` | `int` | `4096` |
| <a id="model_reserve_output"></a>`model_reserve_output` | `Mapping[str, int]` | `{"mimo-v2.5": 131072}` |
| <a id="model_token_caps"></a>`model_token_caps` | `object: model name → {"input": null, "output": null}` | `{}` |
| <a id="pricing"></a>`pricing` | `Mapping[str, ModelPricing]` | `required` |
| <a id="approved_models"></a>`approved_models` | `frozenset[str]` | `required` |
| <a id="period"></a>`period` | `str` | `"lifetime"` |
| <a id="time_zone"></a>`time_zone` | `str` | `"UTC"` |

## ModelPricing

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="input_usd_per_million"></a>`input_usd_per_million` | `Decimal` | `required` |
| <a id="output_usd_per_million"></a>`output_usd_per_million` | `Decimal` | `required` |

## EmbeddingRouteConfig

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="credential_env"></a>`credential_env` | `str` | `required` |
| <a id="model"></a>`model` | `str &#124; None` | `null` |
| <a id="endpoint"></a>`endpoint` | `str &#124; None` | `null` |
| `dimensions` | `int &#124; None` | `null` |
| <a id="dialect"></a>`dialect` | `str &#124; None` | `null` |
| <a id="dimensions_field"></a>`dimensions_field` | `str` | `"dimensions"` |
| <a id="allow_insecure_endpoint"></a>`allow_insecure_endpoint` | `bool` | `false` |

## ConsolidationRouteConfig

| Field | JSON input type | Default when omitted |
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
| <a id="kind"></a>`kind` | `str` | `"openai"` |

## ResponsesRouteConfig

| Field | JSON input type | Default when omitted |
|---|---|---|
| `model` | `str` | `required` |
| `endpoint` | `str` | `required` |
| `credential_env` | `str` | `required` |
| `max_output_tokens` | `int` | `required` |
| `reasoning_effort` | `str &#124; None` | `null` |
| <a id="text_format"></a>`text_format` | `Mapping[str, str] &#124; None` | `null` |
| `stream` | `bool` | `false` |
| `kind` | `str` | `required` |
| `allow_insecure_endpoint` | `bool` | `false` |
| `temperature` | `float &#124; None` | `null` |

## CodexCliRouteConfig

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="executable"></a>`executable` | `Path` | `required` |
| <a id="executable_sha256"></a>`executable_sha256` | `str` | `required` |
| `model` | `str` | `"gpt-5.6-luna"` |
| `kind` | `str` | `"codex_cli"` |
| <a id="subscription_budget"></a>`subscription_budget` | `SubscriptionBudgetPolicy` | `{"daily_calls": 4, "daily_input_tokens": 131072, "daily_output_tokens": 32768, "reserve_input": 32768, "reserve_output": 8192, "max_request_bytes": 24576}` |
| <a id="subscription_budget_explicit"></a>`subscription_budget_explicit` | `bool` | `false` |

## SubscriptionBudgetPolicy

| Field | JSON input type | Default when omitted |
|---|---|---|
| <a id="daily_calls"></a>`daily_calls` | `int` | `4` |
| <a id="daily_input_tokens"></a>`daily_input_tokens` | `int` | `131072` |
| <a id="daily_output_tokens"></a>`daily_output_tokens` | `int` | `32768` |
| <a id="reserve_input"></a>`reserve_input` | `int` | `32768` |
| <a id="reserve_output"></a>`reserve_output` | `int` | `8192` |
| `max_request_bytes` | `int` | `24576` |

Nested objects use the definitions above: binding → InstanceBinding; auxiliary → AuxiliaryRuntimeConfig;
budget → BudgetPolicy; pricing values → ModelPricing; subscription_budget → SubscriptionBudgetPolicy.
In model_token_caps each model maps to an object with input and output integers or null; both keys are stated.
Embedding descriptor fields may be omitted together to select the built-in space.


## Task handoffs

`handoff_write`, `handoff_list`, `handoff_accept` and `handoff_cancel` are available
on the Codex, Claude Code, WorkBuddy and dsh MCP servers and the Hermes provider.
They use the authenticated entry's read and write scopes. Handoffs are agent
reports attached to task episodes; they do not become facts or consolidation input.

To write a report, pass `protocol_version: "1.1"` and a `report` object:

```json
{
  "task_ref": "episode-reference",
  "progress": "Layout is ready",
  "changed_files": ["src/layout.py"],
  "uncommitted_files": ["src/layout.py"],
  "next_step": "Check the narrow viewport",
  "owner_decisions": [],
  "evidence_refs": ["source-reference@1"],
  "session_label": "sender-conversation-label",
  "ttl_days": 7
}
```

Use a visible task episode and current source references belonging to it. File
paths and progress are reports; the server does not read those files or verify
Git status. `session_label` is optional and always labelled `unverified`; it never
authorizes a write or identifies a unique handoff. The separate `sender_session` comes only from host-bound context, such as Hermes identity, hook or remote ingress context, captured session-end metadata, or reserved MCP thread metadata. Without that attestation it is null, and report-body privacy uses project and store rules rather than the display label. A store-generated reference
identifies each report. A trusted project id, or a host-supplied normalized working
directory when project ids are absent, confines delivery to the same project.
An unverified or missing project directory cannot authorize a directory-based report.

`ttl_days` accepts integers from 1 to 365 and defaults to 7. `handoff_list` returns
at most 30 records by default, up to 100 with `limit`; continue with `next_before`
as `before`. Set `include_closed: true` to include accepted, cancelled and expired
records. Every record includes its confirmation and safety state. Explicit reads
label held reports; they never release a held source's text.

Pass `ref` and `expected_revision` to accept or cancel. Only one entry can accept;
a second attempt reports `already_accepted` and the receiving entry. A stale version
is refused. Cancellation belongs to the sending entry or the local installation
owner through the management page. The owner page uses the existing preview and
confirmation operation. Reports keep their lifecycle audit in SQLite.

At session end, existing task resumes produce unconfirmed drafts. A resume
published after its session end also produces a draft. The sending entry confirms
one through `handoff_write` with `report: {"draft_ref": "handoff-reference",
"expected_revision": 1}`. Confirmation publishes the exact stored snapshot;
changed evidence requires a new report. No additional model call is made.

The session briefing includes at most three confirmed pending handoffs within
`session_brief_tokens`. Each appears as escaped JSON labelled as an external
agent report. Accepted, cancelled, expired, quarantined and pending-review reports
are excluded. Lifecycle, expiry, source visibility and injection policy are checked
again before delivery. Reads apply expiry immediately; the existing briefing
worker records expiry transitions in bounded writes. Turning off
`session_brief_enabled` also turns off automatic handoff delivery. No host memory
file needs to be edited.

The automatic schema upgrade preserves existing facts and keeps the upgrade
coordinator's rollback snapshot. To return to an older release, restore its
pre-upgrade snapshot using the documented restore workflow; older code does not
write the newer schema. Handoffs are local coordination records and are not
included in the portable memory interchange format.
