# Codex CLI subscription consolidation

This optional `ConsolidationPort` uses `gpt-5.6-luna` through an existing Codex CLI ChatGPT login. It is not an API-key provider and does not add a scheduler. The installed host's existing worker/drain path retains ownership of work leases, source revisions, schema validation, retries and commits. Installation alone does not enable this route; a successful offline test does not prove real model integration or continuous operation.

## Explicit configuration

Merge the following **auxiliary fields** into the instance's trusted `runtime-config.json` configuration. Preserve its identity, scopes, worker settings, Gemini embedding configuration, API pricing and auxiliary ledger path. Do not replace a complete runtime configuration with this fragment.

```json
{
  "external_consolidation": true,
  "consolidation": {
    "kind": "codex_cli",
    "model": "gpt-5.6-luna",
    "executable": "C:/absolute/path/to/codex.exe",
    "executable_sha256": "960c111d47afd61669954b9df9e56083e302edbfa3ef6962d81dcc14a30051dc",
    "subscription_budget": {
      "daily_calls": 4,
      "daily_input_tokens": 131072,
      "daily_output_tokens": 32768,
      "reserve_input": 32768,
      "reserve_output": 8192,
      "max_request_bytes": 24576
    }
  }
}
```

`executable` must be an absolute path to the verified CLI binary. The adapter checks both the configured digest and its code-owned verified digest set before every model call; setting an arbitrary new digest does not admit a new binary. Missing/changed binaries fail closed. New CLI builds need their own offline request-tool proof before admission. The example digest identifies the first verified Windows build, not a portable promise about arbitrary CLI versions.

Use the same user/service identity that owns the already-established CLI login. The adapter does not read, copy, print or inject OAuth credentials; Codex resolves its own login. It strips ambient API-key/provider overrides and retains only required home/OS/proxy environment locations. An expired/unavailable CLI login fails visibly rather than initiating an interactive login or changing models. Do not put OAuth into an API credential variable or the configuration fragment.

An initialized auxiliary budget database is required, using the existing installer/`initialize_auxiliary_budget_ledger` path. The new table lives in this same ledger, not the memory truth database. Missing/unavailable ledgers refuse calls. Existing API embedding budgets and prices remain independent; do not add a made-up Luna dollar price to API `approved_models`/`pricing`.

## Hard admission and soft token reservations

| Boundary | Contract |
| --- | --- |
| Normal worker pass | At most one model invocation, shared by consolidation and candidate evaluation. A new pass gets a new local allowance; the durable daily ledger is not reset. |
| Daily calls | Hard admission cap, default 4 across processes and restarts, UTC day. Every admitted invocation counts, even failure/timeout. |
| Daily input/output | Hard admission caps, default 131072/32768. Admission requires room for a full 32768/8192 reservation. Known usage replaces the reservation; unknown usage retains it. |
| Per-call input/output | 32768/8192 are **soft reservations, not tokenizer-enforced generation caps**. CLI system overhead is not the serialized prompt byte count. Actual usage exceeding either reservation is recorded in full, rejects that result, and fences subsequent calls across day rollover until operator review. No bounded provider-token-generation claim is made. |
| Request bytes | Hard 24576-byte UTF-8 limit on the serialized role/content message array before spawn; not a token count. |
| Output bytes | Hard 1 MiB in-memory CLI JSON event buffer; oversize terminates this invocation. Not a generated-token quota. |
| Deadline | Uses the core remaining deadline, capped at 45 seconds, with 4 seconds reserved for process-tree cleanup/accounting. |
| Retries | Adapter has no retry/fallback loop; CLI request/stream retries are set to zero. Later work retries remain the existing bounded worker policy and still require daily admission. |

Accounting status reports `accounting="subscription_unpriced"`, `charge_micro_usd=null`, daily counts/tokens, latest status and `meter_breach`. Null means **subscription use is not dollar-metered here**, not free usage. Cached input remains part of reported input usage. An interrupted parent leaves a durable pre-spawn reservation; it must not be erased to obtain another call. `meter_breach` is a persistent safety stop, not an invitation to delete/reset the ledger. Unknown timeout usage is explicitly labelled as reserved, not asserted as observed provider consumption.

Core schema/semantic failures can follow a transport-level `codex_success`; use both the worker receipt and subscription status. Only core decides whether memory commits. Typical failures include `timeout`, `codex_exit_nonzero`, `codex_protocol`, `codex_turn_failed`, `codex_unverified_binary`, `budget_exhausted`, `budget_unavailable` and `meter_breach`. No prompt/model output is included in these error labels.

## Tool and process isolation

Every invocation uses an owned temporary working directory, stdin for the role/content prompt, `--ignore-user-config --ignore-rules --ephemeral --skip-git-repo-check --sandbox read-only`, and `approval_policy="never"`. User plugins, MCP servers, hooks, notifications, project instructions, persistent history and analytics are disabled/overridden. Log and CLI state paths are confined to the temporary directory and removed. stdout is parsed in bounded memory; stderr is discarded. Neither is copied to runtime logs.

Before the model invocation, the verified CLI initializes that temporary state with an empty, credential-free `CODEX_HOME` using `app-server --stdio` and immediate stdin EOF (no RPC/model request). The adapter reads back native backfill completion and an empty thread index, then restores the original login home for ephemeral model execution. This prevents a fresh `sqlite_home` from synchronously importing the user's entire rollout history on every call. Native state markers are never fabricated, the user's state database is not reused, and credentials are not copied. Bootstrap is bounded to five seconds within the existing total deadline; `codex_state_init_failed` refuses the model call before budget reservation.

**Read-only sandboxing or a “do not use tools” prompt is not the enforcement mechanism.** The pinned CLI's supported model catalog removes shell, apply-patch, search and code-mode tools; feature/config gates remove the other tool surfaces. The opt-in offline native test observes the real request sent by that CLI and requires `tools=[]`, no additional tools, exactly one request and no Authorization header against an isolated loopback fake provider. Unexpected tool events are also rejected, but that post-response check is only a secondary guard. A binary update is refused until this boundary is re-verified.

On timeout the adapter targets only its own spawned PID/process tree (`taskkill /PID … /T /F` on Windows; own process group on POSIX), never an image-wide Codex/application kill. No changes to the user's Codex App process, configuration, plugin trust or login store are performed by the adapter.

## Focused verification and activation

Run the focused test file with the supported CLI path to include the offline native wire proof:

```bash
SCOPE_RECALL_CODEX_CLI='C:/absolute/path/to/codex.exe' \
  python -m pytest -q -s tests/contract/test_codex_cli_consolidation.py
```

The native test isolates `CODEX_HOME`, uses synthetic input and a loopback fake HTTP service, and makes **no real model/OAuth call**. Without the environment variable that proof is skipped; unit mocks alone do not qualify another CLI build. The other cases cover success, protocol/exit failure, timeout with an unrelated process left alive, durable budgets/overspend, and normal runtime/core schema/lease behavior in a temporary store.

Before enabling live continuous integration, deploy the candidate through the normal release/install path, verify the same source and CLI identity, then perform the separately authorized synthetic model/core/store acceptance. Read back its worker receipt, schema-accepted outcome, lease state and subscription ledger. A timeout or bare availability probe is not successful consolidation. Verify subsequent normal worker scheduling after deployment; do not install a one-off runner or another scheduled service. To disable future model work, set `external_consolidation=false` through the authorized config/reload path while retaining the ledger and existing embedding settings.

CLI reference: <https://developers.openai.com/codex/cli/reference>. The pinned binary's `exec --help`, bundled catalog and actual offline request are the executable contract for build-specific switches; no blanket support for other CLI builds is implied.
