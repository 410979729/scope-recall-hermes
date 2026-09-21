# Configuration

Scope Recall 3.x reads two files. The **installation record** is written by the
installer and describes identity: `<instance-root>\scope-recall\installation.json`
for Hermes, `<instance-root>\codex-installation.json` for Codex. The
**runtime config**, `runtime-config.json`, is written by you and describes
behaviour: budgets, the vector companion, and the external model routes. This
document is a reference for the second file.

The former 2.x `config.json` key reference lived here until the 2.x engine was
removed; it remains in repository history at tag `v2.0.1`.

## Where the file goes

`runtime-config.json` belongs in the Core data directory, beside `memory.sqlite3`:

| Host | Core data directory |
|------|---------------------|
| Hermes | `<instance-root>\scope-recall\` |
| Codex | `<instance-root>\data\` |

Nothing generates this file. The installer does not create it, and it is never
discovered from the current directory, a parent directory or a credential
location. When a host adapter attaches, it looks only at
`<data-directory>\runtime-config.json` (`RUNTIME_CONFIG_FILENAME` in
`adapters/runtime_wiring.py`). Without the file the Core still runs with basic
capability and the adapter reports the gap `capability_gap:trusted_runtime_unconfigured`.

Three more gaps come from the same code path:

- `capability_gap:trusted_runtime_invalid` — the path is not absolute, or is a
  symlink, reparse point or non-regular file, or the JSON does not parse or does
  not validate, or an auto-discovered file is not directly inside the data directory.
- `capability_gap:trusted_runtime_binding_mismatch` — the `binding` block does not
  match the identity the host bound: `agent_id`, `installation_id`,
  `data_directory`, `scope_ids` and `test_mode` must all agree.
- `capability_gap:trusted_runtime_worker_busy` / `..._audience_capacity` — runtime
  is configured, but no further background worker could be started right now.

The background worker takes the path on the command line instead
(`--config`, `runtime/worker_entry.py`), and accepts any absolute JSON file. The
supported location is still the one above, and `autostart` enforces it: a config
whose parent directory is not the binding's `data_directory` is refused with
`autostart_config_outside_binding`.

Changes take effect the next time a process loads the file. Nothing reloads it
in place.

## How values are read

Every field is checked exactly, with no coercion (`runtime/validation.py`):

- `true` is not `1`, and a count field rejects a boolean.
- `"45"` is not `45.0`. Second fields accept an `int` or a `float`; count fields
  accept only `int`.
- Every path must be absolute. On Windows, write `"C:\\path\\to\\dir"` or
  `"C:/path/to/dir"` — JSON needs the backslash escaped.
- All bounds are closed intervals: the stated minimum and maximum are both legal.
- A bad value fails the whole file. The error message is the field name.

One trap: the readers take the keys they know and ignore the rest
(`RuntimeInstanceConfig.from_mapping`, `AuxiliaryRuntimeConfig.from_mapping`,
`VectorRuntimeConfig.from_mapping` all use `raw.get(...)`). A misspelled
top-level key is silently ignored, not reported. After an edit, confirm the
value you meant to change actually took effect with `doctor`.

## Minimal file

This is the smallest file that validates. It configures no external model route
and no vector companion, so recall stays lexical.

```json
{
  "binding": {
    "agent_id": "default",
    "installation_id": "<installation id from the installation record>",
    "data_directory": "C:/path/to/instance-root/scope-recall",
    "scope_ids": ["<scope id>"],
    "test_mode": false
  },
  "session_id": "local-worker",
  "allowed_scope_ids": ["<scope id>"]
}
```

## `binding` (required)

Identity, not preference. Copy these values from the installation record the
installer wrote; do not invent them. A mismatch is a capability gap, not a
silent rebind.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `agent_id` | string, non-blank, ≤ 240 chars | required | The agent identity the host sends. Must equal the installed `agent_id`. |
| `installation_id` | string, non-blank, ≤ 240 chars | required | The installation identity from the installation record. |
| `data_directory` | absolute path | required | The Core data directory holding `memory.sqlite3`. |
| `scope_ids` | array of strings | required | Every scope this installation owns. |
| `test_mode` | boolean | `false` | Isolated TEST binding semantics. Production installs are `false`. |

## Identity and partition

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `session_id` | string, non-blank, ≤ 240 chars | required | Attribution and de-duplication key stamped onto rows the worker writes. It is not an auth token. |
| `allowed_scope_ids` | array of strings | required | The scopes this config may act in. Must be non-empty and a subset of `binding.scope_ids`; otherwise the file is rejected with `ACCESS_DENIED`. |
| `actor_origin` | one of `human_direct`, `tool_observation`, `external_document`, `imported` | `human_direct` | Provenance the worker's own writes carry. Leave it at `human_direct`: the drain's deferred-source refill only accepts `human_direct`, so any other value makes every pass refuse with `ACCESS_DENIED` on `schedule_origin`. |
| `project_id` | string or `null` | `null` | Partition filter. Work rows carrying a non-null `project_id` are only visible when it equals this value. |
| `branch_id` | string or `null` | `null` | The same filter on the branch axis. |
| `host_adapter` | `"hermes"`, `"codex"` or `null` | `null` | Selects the ingress authorizer that replays the durable capture inbox. Left unset, pending inbox rows can only be reported, with the gaps `capture_gap:durable_ingress_authorizer_unconfigured` and `capture_gap:durable_ingress_pending`. Set it to the host that owns this instance. |
| `owner_id` | string, non-blank, ≤ 240 chars | `"scope-recall-worker"` | The lease holder name written onto claimed work rows and echoed in the worker receipt. Change it only to tell two deliberately separate workers apart. |

Hosts override `session_id`, `allowed_scope_ids`, `host_adapter`, `project_id`
and `branch_id` per session when they attach, so the stored values are the
defaults a standalone worker pass uses.

## Pass and budget knobs

| Key | Type | Default | Bounds | Meaning |
|-----|------|---------|--------|---------|
| `max_items` | int | `32` | 1–1000 | Most work items one pass may reserve and claim. Also caps an operator retry, which takes at most `min(8, max_items)`. A pass claims its embedding group in one page of up to this many items; what it cannot finish inside `drain_seconds` is handed back unspent and claimed again by the next pass. A value far above the default is for a one-off drain of a large embedding backlog and buys nothing once that is gone: put it back afterwards. |
| `drain_seconds` | number | `120.0` | 0.001–120.0 | Wall budget for the whole pass. The drain, the lock wait and the finalize share this one deadline, and it is the watchdog's kill budget. |
| `request_seconds` | number | `45.0` | 0.001–45.0 | Per-call ceiling clamped onto every model and native boundary: consolidation, candidate evaluation, embedding, vector purge, and the vector-open slice. |
| `lease_seconds` | number | `60.0` | `request_seconds`–3600.0 | How long a claimed item stays leased to `owner_id` before a later pass may reclaim it as stale. Must be at least `request_seconds`. |
| `worker_min_interval_seconds` | number | `30.0` | 1–3600 | Minimum gap between two passes. A supervised window sleeps out the remainder; a host coalesces wake-ups against it. |
| `supervisor_enabled` | boolean | `true` | — | When true, a scheduled wake runs a finite supervise loop of several passes. When false, a scheduled wake refuses to launch at all — this is the switch that turns background work off without unregistering it. |
| `supervisor_seconds` | number | `21600.0` (6 h) | 1–86400 | Total lifetime of one supervised window. On expiry the window suspends. |
| `supervisor_max_drains` | int | `256` | 1–1024 | Most passes one supervised window may run before it suspends. |
| `daily_work_limit` | int | `0` | 0–1000000 | Queue items a UTC day may attempt. **`0` means no cap** and is the default. When a cap is spent, the pass runs deletion cleanup only, reports the gap `daily_queue_budget`, and the next wake moves to the next UTC midnight. |
| `storage_budget_bytes` | int | `0` | 0–2^50 | Bytes `memory.sqlite3` and `vectors/` together may occupy before `doctor` reports the gap `storage_budget_exceeded`. **`0` sets no budget.** Nothing is deleted for it; it is a warning, and `doctor` reports the bytes and the day's growth either way. |
| `auto_retry_cooldown_seconds` | number | `3600.0` | 60–86400 | Delay added before a recoverable failure becomes due again. The same value, clamped to 300 s, is the stand-down for a work type a pass reported unavailable. |
| `max_auto_recoveries` | int | `2` | 0–4 | Automatic retries a recoverable failure gets. `0` disables automatic recovery; the failure then waits for `retry-failures`. |
| `auto_recall_seconds` | number | `5.0` | 0.001–5.0 | Deadline for *automatic* recall on the read path. On timeout, recall degrades to lexical. |
| `hook_processing_seconds` | number | `6.0` | 0.001–6.0 | Total budget a trusted-host hook has to answer. Must be at least `auto_recall_seconds`, so the hook can cover a full automatic recall; a smaller value fails the file with `hook_processing_seconds_must_cover_auto_recall`. |

`daily_work_limit` is not the spend guard. Money, calls and tokens are enforced
per request by the auxiliary ledger (`runtime/model_budget.py`), which cannot see
this counter, and this counter cannot see cost. A limit set below the rate at
which work arrives is not thrift, it is a permanent backlog: the comment on the
field records an instance where roughly fifteen work items arrived per captured
source, so the old default of 256 could never drain one day's own output.

The maxima above are protections, not recommendations. `request_seconds` at
45 seconds and `drain_seconds` at 120 seconds are the contract ceilings; a
shorter value is honoured and is often the better setting on a slow link.

## `vector`

Omit the whole block to run without a vector companion; recall is then lexical.
The block is a mapping:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `backend` | `"lancedb"` or `"sqlite-bruteforce"` | `"lancedb"` | `lancedb` needs the `lancedb` extra installed. `sqlite-bruteforce` is dependency-free and scans; it is meant for hosts where LanceDB or PyArrow is unsafe, and for small to medium memory sets. Any other value, including `"pgvector"`, is refused. |
| `storage_dir` | absolute path | required | Must be exactly `<data_directory>/vectors/<embedding space id>`, or the file is refused with `VECTOR_STORAGE_OUTSIDE_BINDING`. The backend creates its own store below it: a `lancedb/` directory, or a `vector.sqlite3` file. |
| `table_name` | string, non-blank, ≤ 240 chars | required | Table or collection name inside the store. |
| `dimensions` | int | required, 1–8192 | Must equal the active embedding space's width, or the file is refused with `VECTOR_DIMENSIONS_MISMATCH`. The shipped space is 3072. |
| `metric` | `"cosine"` | `"cosine"` | The only supported metric. |
| `test_injection_override` | boolean | `false` | A test seam. It is refused unless `binding.test_mode` is true, and it skips the dimension and path checks above. Do not set it in a real installation. |
| `tool_output_retention_days` | int | `180` | Days a tool output's vector is kept after its source entered the store. A worker pass then deletes the vector, up to 2,000 per pass and hourly once caught up, and the following compaction reclaims the space. The same pass deletes at once, whatever their age, the vectors of a tool output that repeats an earlier one in the same scope (the earliest copy keeps its vector) and of the summary the capture filter leaves in place of an output it withheld. The source text, its lexical index and everything derived from it stay, so the output is still found by its words and through what cites it, never again by meaning alone. `0` turns the pass off entirely. |

The embedding space id is the SHA-256 digest of the active space descriptor. For
the shipped space it is
`93ba90c7d52b3574462d6751e2e077a411f1095727862abb4cc80d3780d2e30c`. Naming an
embedding route changes the digest, and therefore the directory — see
[Pointing a route at another provider](#pointing-a-route-at-another-provider).

On Windows keep the data directory short, for example `C:\ScopeRecall\my-agent`.
LanceDB appends index, table and temporary file names to `storage_dir`; when the
resulting native path is too long the worker reports
`native_vector_path_too_long` before starting LanceDB or calling the embedding API.

## `vector_threshold`

A top-level field: the lowest cosine similarity, from `-1.0` to `1.0`, at which a
vector hit is admitted into recall. It is compared with a `1e-6` float tolerance.

It has no default and no installer writes it. Without it, recall refuses every
vector hit with `vector_threshold_unconfigured` and answers degrade to lexical,
while sources and queries are still embedded and metered. `doctor` reports that
state as the gap `vector_threshold_unconfigured` when a `vector` store and an
approved embedding route are configured but the threshold is not.

A threshold is calibrated for one embedding space and does not carry over. The
shipped space — `gemini-embedding-2` at 3072 dimensions, the space used when
`auxiliary.embedding` names no model — was accepted at `0.653189984350642`, and
that is the value the tests pin. The acceptance covers that frozen model,
encoder and space descriptor only. A route that states `model`, `endpoint`,
`dimensions` and `dialect` is a different space with its own digest and vector
directory, **even when it repeats the shipped values**, and falls outside it.
Any other model needs its own calibration on labelled relevant and irrelevant
query-memory pairs embedded with that model. Until one exists, leave the field
unset and let recall stay lexical; a guessed threshold either admits noise as
evidence or admits nothing at all.

## `auxiliary`

External model routes. Omit the block entirely and it defaults to
`{"external_embedding": false, "external_consolidation": false}` — no external
call is possible and no credential is read.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `external_embedding` | boolean | required when the block is present | Approval switch for the embedding route. It must be exactly `true` to enable one; anything else yields the gap `external_embedding_not_approved`. |
| `external_consolidation` | boolean | required when the block is present | The same switch for the consolidation route; the gap is `external_consolidation_not_approved`. |
| `installation_dir` | absolute path or absent | absent | Base directory used to derive a default `ledger_path` of `<installation_dir>/auxiliary-budget.sqlite3`. It has no other effect. |
| `ledger_path` | absolute path or absent | derived from `installation_dir`, else absent | The SQLite budget ledger every external request is metered against. Without it, both routes report `auxiliary_budget_ledger_unconfigured` and no request is made. |
| `budget` | mapping or absent | fail-closed defaults | Spend, token and call policy. See below. |
| `embedding` | mapping or absent | absent | The embedding route. Absent with `external_embedding: true` gives the gap `external_embedding_unconfigured`. |
| `consolidation` | mapping or absent | absent | The consolidation route. Absent with `external_consolidation: true` gives the gap `external_consolidation_unconfigured`. |
| `consolidation_reserve_input` | int ≥ 1 | `32768` | Input tokens reserved against the ledger before a consolidation request, before the real usage is known. |

Enabling a route takes four things together: the switch `true`, the route
mapping, a `ledger_path` whose file exists, and a `budget` that approves and
prices the model. Miss any one and the route stays off with a named gap; nothing
half-works.

### `auxiliary.budget`

Omitted, the policy is deliberately fail-closed: `cap_micro_usd`,
`total_input_cap`, `total_output_cap` and `total_call_cap` are all `0`, and
`approved_models` and `pricing` are empty, so no request is permitted. A present
`budget` mapping **must** state `pricing` and `approved_models`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `batch` | string, non-empty | `"auxiliary"` | Label the ledger records requests under. |
| `pricing` | mapping of model to `{"input_usd_per_million", "output_usd_per_million"}` | required | Rates used to charge each request. Each rate is a non-negative amount given as an int or an exact decimal string, for example `"0.15"`. |
| `approved_models` | array of strings | required | The only models any route may call. Every entry must also appear in `pricing`, or the file is refused with `approved_model_pricing`. |
| `cap_micro_usd` | int ≥ 0 or `null` | `0` | Lifetime spend cap in micro-USD across the whole ledger. `0` means no spend at all; `null` means uncapped. |
| `total_input_cap` | int ≥ 0 or `null` | `0` | Lifetime input-token cap. Same `0` versus `null` distinction. |
| `total_output_cap` | int ≥ 0 or `null` | `0` | Lifetime output-token cap. Same distinction. |
| `total_call_cap` | int ≥ 0 or `null` | `0` | Lifetime request-count cap. Same distinction. |
| `max_request_bytes` | int ≥ 1 | `32000` | Largest request body permitted. |
| `default_reserve_input` | int ≥ 1 | `32768` | Input tokens reserved before usage is known. |
| `default_reserve_output` | int ≥ 1 | `4096` | Output tokens reserved before usage is known. |
| `model_reserve_output` | mapping of model to int ≥ 1 | one shipped entry | Per-model override of the output reservation. |
| `model_token_caps` | mapping of model to `{"input", "output"}` | `{}` | Per-model lifetime token caps. Both sides must be stated; an explicit `null` on a side means uncapped, `0` means no spend. |

The ledger file is never created as a side effect of reading it — a missing file
refuses every request with `ledger_not_initialized`, and no CLI command in this
distribution creates it. Create it once with the exported helper, using the
interpreter the host uses:

```powershell
C:\path\to\python.exe -c "import json, pathlib; from scope_recall.runtime import initialize_auxiliary_budget_ledger; from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig; raw = json.loads(pathlib.Path(r'C:\path\to\instance-root\scope-recall\runtime-config.json').read_text(encoding='utf-8')); cfg = AuxiliaryRuntimeConfig.from_mapping(raw['auxiliary']); initialize_auxiliary_budget_ledger(cfg.ledger_path, cfg.budget); print(cfg.ledger_path)"
```

```bash
/path/to/python -c "import json, pathlib; from scope_recall.runtime import initialize_auxiliary_budget_ledger; from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig; raw = json.loads(pathlib.Path('/path/to/instance-root/scope-recall/runtime-config.json').read_text(encoding='utf-8')); cfg = AuxiliaryRuntimeConfig.from_mapping(raw['auxiliary']); initialize_auxiliary_budget_ledger(cfg.ledger_path, cfg.budget); print(cfg.ledger_path)"
```

### `auxiliary.embedding`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `credential_env` | string matching `^[A-Z][A-Z0-9_]{0,127}$` | required | **Name** of the environment variable holding the API key. The key itself never appears in this file. |
| `model` | string, 1–200 chars | absent | Model name sent on the wire. |
| `endpoint` | `https://` URL, ≤ 2048 chars | absent | Full request URL. Not a base URL: no path is appended. |
| `dimensions` | int, 8–16384 | absent | Vector width. It is sent in the request and the response length is checked against it. |
| `dialect` | `"gemini"` or `"openai"` | absent | Wire shape. See the next section. |
| `dimensions_field` | string, a JSON field name | `"dimensions"` | The request field the `openai` dialect sends the width in. Voyage calls it `output_dimension` and refuses `dimensions`. A wire detail: it does not change the embedding space. |

`model`, `endpoint`, `dimensions` and `dialect` move together. Omit all four and
the route addresses the shipped Gemini space, so an existing installation keeps
its vector directory. State all four to address another provider. State some but
not all and the file is refused with `embedding_route_partial_space`, because
half a descriptor would silently mix a new model with the old width or dialect
and the digest would not reveal it.

### `auxiliary.consolidation`

The default kind is an OpenAI-compatible chat-completions route
(`kind` absent, or `"openai"`):

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `model` | string, non-empty | required | Model name. It must also be in `budget.approved_models` and `budget.pricing`. |
| `endpoint` | `https://` URL | required | Full chat-completions URL. |
| `credential_env` | string matching `^[A-Z][A-Z0-9_]{0,127}$` | required | Name of the environment variable holding the API key. |
| `output_limit_field` | `"max_tokens"` or `"max_completion_tokens"` | required | Which field this provider expects the output limit in. |
| `max_output_tokens` | int, 1–131072 | required | Value sent in that field. |
| `thinking` | mapping of string to string, or absent | absent | Provider-specific reasoning control, for example `{"type": "disabled"}`. |
| `response_format` | mapping or absent | absent | Passed through after validation, for example `{"type": "json_object"}`. |
| `reasoning_effort` | string or absent | absent | Passed through after validation. |
| `stream` | boolean | `false` | Must stay `false`; streaming is refused. |
| `n` | int | `1` | Must stay `1`. |
| `headers` | mapping or absent | absent | Extra request headers, validated before use. A second `Authorization` cannot be smuggled in this way. |

Setting `"kind": "openai_responses"` selects a Responses-API route instead of
chat completions. It targets the documented contract of DeepSeek
`POST https://api.deepseek.com/responses`; nothing about it is claimed for
another provider, and streaming is not implemented:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `model` | string, non-empty | required | Model name, for example `deepseek-flash`. It must also be in `budget.approved_models` and `budget.pricing`. |
| `endpoint` | `https://` URL | required | Full Responses URL. Not a base URL: no path is appended. |
| `credential_env` | string matching `^[A-Z][A-Z0-9_]{0,127}$` | required | Name of the environment variable holding the API key. It is read per request and never written to the ledger; the key travels only in the `Authorization` header, as a `Bearer` token that extra `headers` cannot override. |
| `max_output_tokens` | int, 1–131072 | required | Sent as `max_output_tokens`. On a thinking route this bounds the visible answer **and** the reasoning tokens. |
| `reasoning_effort` | `"none"`/`"low"`/`"high"`/`"max"`, or absent | absent | Sent as `reasoning.effort`; absent leaves the provider's own default. |
| `text_format` | `{"type": "json_object"}` or absent | absent | Sent as `text.format`. Plain text is the default and is expressed by omitting the key; a JSON schema is refused rather than forwarded unvalidated. |
| `stream` | boolean | `false` | Must stay `false`; streaming is refused. |
| `kind` | `"openai_responses"` | required | Selects this route. |
Unknown keys in this block are rejected (`consolidation_unknown_config`) rather
than ignored, the way the `codex_cli` block already behaves: this dialect is new
and has no legacy key to stay compatible with.

The request explicitly carries `store: false`. DeepSeek documents this parameter
as unsupported/ignored and its API as stateless; this flag is not a substitute
for checking another provider's retention policy. Context must be supplied on
every call. Each message becomes one `input` item in its original position,
including interleaved `system` messages. Text parts use `input_text` for `system`
and `user`, and `output_text` for `assistant`. A `tool` message is refused
(`input_invalid`): function-call item pairs are not implemented by this adapter.

Only a response whose own `status` is `completed` is an answer. `incomplete`
currently fails as `DERIVATION_INVALID` on
`model_output_truncated`, exactly like a chat answer cut off at the output
limit (the diagnostic does not yet distinguish content filtering from the
token limit); `failed` fails as `response_status_failed`. From a completed response the
adapter reads only the `output_text` parts of `assistant` message items —
`reasoning` items and `reasoning_text` parts are never treated as an answer, a
refusal part fails as `model_refused`, and an answer with no text fails as
`empty_output`. The text is handed to the existing proposal validator
unchanged, with no JSON repair.

Usage is read as `input_tokens`/`output_tokens`, with
`input_tokens_details.cached_tokens` recorded for observation.
`output_tokens` already counts the reasoning tokens the provider reports
separately in `output_tokens_details.reasoning_tokens`, so those are never
billed a second time. A missing or malformed `usage` block keeps the reserved
charge, and a `200` that carries an error object is not a free call either.

Setting `"kind": "codex_cli"` selects the local Codex CLI subscription route
instead, which calls a signed local executable rather than an HTTP endpoint. Its
keys are `executable` (absolute path), `executable_sha256` (64 lowercase hex
characters), optional `model`, and an optional `subscription_budget` mapping
whose defaults are 4 calls, 131072 input tokens and 32768 output tokens per UTC
day. Unknown keys in this block are rejected rather than ignored. See
[codex-cli-consolidation.md](codex-cli-consolidation.md) for the model
constraints and what it does and does not cover.

### Where the credential actually comes from

`credential_env` names a variable; the process environment supplies the value.
How it gets there differs by process:

- The Hermes gateway passes its own environment down, so exporting the variable
  where the host starts is enough.
- Codex starts the MCP server and the hooks with its own environment, which does
  not carry the name. Pass `--env-file <absolute file>` at install time; the
  installer writes it into `.mcp.json`, `hooks.json` and the Windows hook launcher.
- A background worker started by `autostart` receives it through the autostart
  control file, so pass the same `--env-file` to `autostart enable`.
- A worker pass you run by hand has no `--env-file`. Export the variable in your
  shell first.

The env file is read under a narrow contract (`runtime/resume_entry.py`): UTF-8
with an optional BOM, at most 1 MiB, absolute, not a symlink, one
`NAME=value` per line with an optional `export ` prefix. Only names the runtime
config declares are read; everything else in the file is ignored. One matching
pair of surrounding quotes is stripped, otherwise the value is truncated at the
first ` #`. There is no shell execution and no `${VAR}` interpolation — it is not
a dotenv loader.

## Pointing a route at another provider

### Embedding

Two dialects are supported (`EMBEDDING_DIALECTS` in `core/recall_policy.py`).

**`"openai"`** is the `/v1/embeddings` shape that most providers accept. The
request carries `Authorization: Bearer <credential>` and a body of
`{"model", "input", "dimensions"}`; the vector is read from
`data[0].embedding` and usage from `usage.prompt_tokens`. `endpoint` is the full
URL, for example `https://api.example.com/v1/embeddings`.

```json
"auxiliary": {
  "external_embedding": true,
  "external_consolidation": false,
  "ledger_path": "C:/path/to/instance-root/scope-recall/auxiliary-budget.sqlite3",
  "budget": {
    "approved_models": ["your-embedding-model"],
    "pricing": {"your-embedding-model": {"input_usd_per_million": "0.02", "output_usd_per_million": "0"}},
    "cap_micro_usd": 2000000,
    "total_call_cap": null,
    "total_input_cap": null,
    "total_output_cap": null
  },
  "embedding": {
    "credential_env": "YOUR_EMBEDDING_API_KEY",
    "model": "your-embedding-model",
    "endpoint": "https://api.example.com/v1/embeddings",
    "dimensions": 1024,
    "dialect": "openai"
  }
}
```

Voyage AI is OpenAI-shaped in every respect but one: the width field is
`output_dimension`, and a request carrying `dimensions` is refused. Name the
field on the route; nothing else changes, and the space digest is the same as
for the field's default name.

```json
"embedding": {
  "credential_env": "VOYAGE_API_KEY",
  "model": "voyage-4-large",
  "endpoint": "https://api.voyageai.com/v1/embeddings",
  "dimensions": 2048,
  "dialect": "openai",
  "dimensions_field": "output_dimension"
}
```

**`"gemini"`** is Google's native `batchEmbedContents` shape. The request carries
an `x-goog-api-key` header and a `requests[0]` body with
`embedContentConfig.outputDimensionality`; the vector is read from
`embeddings[0].values` and usage from `usageMetadata.promptTokenCount`.
`endpoint` is the full `...:batchEmbedContents` URL.

### Changing the embedding model rebuilds the vector store

The embedding space descriptor — model, width, endpoint, request encoding, the
prompt encoding and the NFKC preprocessing id — is hashed, and that digest is the
name of the vector directory. Change the model, the dimensionality or the
dialect and the digest changes. The consequences are all deliberate:

1. `vector.storage_dir` must be updated to the new
   `<data_directory>/vectors/<new space id>`, or the config is refused with
   `VECTOR_STORAGE_OUTSIDE_BINDING`.
2. `vector.dimensions` must match the new width, or it is refused with
   `VECTOR_DIMENSIONS_MISMATCH`.
3. The old directory is left alone and its vectors are refused at admission as
   coming from a different space, rather than being compared across incompatible
   geometries. The new store starts empty, and background passes re-embed into
   it. SQLite remains the authority throughout, so nothing is lost — only the
   companion is rebuilt, and semantic recall is thin until it catches up.
4. `vector_threshold` no longer applies. Recalibrate it, or unset it and accept
   lexical recall in the meantime.

Stating the shipped Gemini values explicitly is still a *named* route with its
own digest, because its request-encoding id differs from the frozen
descriptor's. If you want to keep an existing vector directory, omit all four
fields and give only `credential_env`.

To print the digest a config's embedding route resolves to, before a host loads
it. This reads the route only, so it still answers while `vector.storage_dir`
points at the old space:

```powershell
C:\path\to\python.exe -c "import json,pathlib;from scope_recall.core.recall_policy import EMBEDDING_SPACE,embedding_space_id;from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig;raw=json.loads(pathlib.Path(r'C:\path\to\instance-root\scope-recall\runtime-config.json').read_text(encoding='utf-8'));r=AuxiliaryRuntimeConfig.from_mapping(raw.get('auxiliary') or {'external_embedding':False,'external_consolidation':False}).embedding;print(embedding_space_id(r.space() if r else dict(EMBEDDING_SPACE)))"
```

```bash
/path/to/python -c "import json,pathlib;from scope_recall.core.recall_policy import EMBEDDING_SPACE,embedding_space_id;from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig;raw=json.loads(pathlib.Path('/path/to/instance-root/scope-recall/runtime-config.json').read_text(encoding='utf-8'));r=AuxiliaryRuntimeConfig.from_mapping(raw.get('auxiliary') or {'external_embedding':False,'external_consolidation':False}).embedding;print(embedding_space_id(r.space() if r else dict(EMBEDDING_SPACE)))"
```

### Consolidation

The consolidation route has no dialect switch: it is the OpenAI-compatible
chat-completions shape (`kind` absent or `"openai"`), the Responses-API shape
(`"kind": "openai_responses"`, see `auxiliary.consolidation` above), or the
local `codex_cli` kind. To move it to another
provider, change `model`, `endpoint` and `credential_env`, set
`output_limit_field` to whichever of `max_tokens` / `max_completion_tokens` that
provider accepts, and add the model to `budget.approved_models` and
`budget.pricing`. Reasoning controls differ between providers: `thinking`,
`reasoning_effort` and `response_format` are validated, and a value a route does
not accept is refused rather than sent. The Responses kind states
`max_output_tokens` instead of `output_limit_field`, and its reasoning and text
controls are `reasoning_effort` and `text_format`.

Changing the consolidation model does **not** touch the vector store. Only the
embedding space feeds the digest.

## Recall budgets

These two budgets are core and host defaults. They are not `runtime-config.json`
leaves unless noted.

### Automatic recall packet budget

Automatic recall defaults to `4096` character-calibrated token-estimate units,
and the automatic ceiling is the same value. `core/recall_budget.py` applies one
estimator to admission and to the complete canonical packet: ASCII letters,
digits and whitespace cost a quarter unit each; CJK and other letters, numbers,
combining marks and punctuation cost one unit; other symbols use their UTF-8 byte
length. The combined quarter-unit total is rounded up once. This is a
deterministic approximation, not a provider tokenizer measurement. UTF-8 bytes
are reported separately and do not penalise each CJK character by its encoded
byte length. Explicit smaller `budget_tokens` values keep their cap. Automatic
recall also delivers at most six items whatever the request asked for; explicit
modes keep their own `max_items`, which ranges from 1 to 30. None of these is a
`runtime-config.json` key — they are per-query retrieval limits.

Codex MCP `recall` advertises the same whole-packet estimate, including packet
and source metadata. Omission defaults to `4096` for explicit retrieval; small
explicit values can still clip every item. A packet that cannot fit even the
minimal honest envelope is rejected. On `budget_token_cap` or
`budget_packet_cap`, retry at most once with `4096`; this does not raise the
automatic ceiling. The separate `profile` and `entity` read views keep their own
documented UTF-8 byte budgets.

### Automatic recall latency budget

Automatic remote semantic recall defaults to `auto_recall_seconds=5.0`, which is
also the hard maximum. The trusted-host fallback hook budget defaults to
`hook_processing_seconds=6.0`, which is also the hard maximum, so the hook can
cover a full automatic recall. Both are settable in `runtime-config.json` and are
validated against those ceilings. A remote query embedding has to finish inside
the automatic budget, so a value well below the default is the usual reason
semantic recall silently stops contributing: timeouts stay in force and degrade
to lexical recall. A caller may also pass a stricter deadline, and that explicit
shorter deadline is honoured, clamped by the configured automatic budget. None of
this raises a money, token, call, packet or source cap, disables TLS validation,
or changes `vector_threshold`.

## Checking a change

`doctor` reads the file back and reports what it found:

```powershell
scope-recall doctor --host hermes --instance-root C:\path\to\instance-root --python C:\path\to\python.exe
```

It exits `0` only when `status` is `ok`; both `attention` and `degraded` exit `1`.
See [install.md](install.md) for the report and the common gaps.
