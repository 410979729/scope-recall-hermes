# Configuration

Scope Recall 3.x is configured by the installation receipt and by
`runtime-config.json` inside the installation-owned Core directory; see
[docs/install.md](install.md) for how the installer writes both and
`maintenance.cli doctor` for how they are read back. The former 2.x
`config.json` key reference lived here until the 2.x engine was removed; it
remains in repository history at tag `v2.0.1`.

The two runtime budgets below are core/host defaults, not receipt fields.

## Automatic recall packet budget

Automatic recall defaults to `4096` character-calibrated token-estimate units, and the automatic ceiling is the same value. `core/recall_budget.py` applies one estimator to admission and the complete canonical packet: ASCII letters, digits and whitespace cost a quarter unit each; CJK and other letters, numbers, combining marks and punctuation cost one unit; other symbols use their UTF-8 byte length. The combined quarter-unit total is rounded up once. This is a deterministic approximation, not a provider tokenizer measurement. UTF-8 bytes are reported separately and no longer penalize each CJK character by its encoded byte length. Explicit smaller `budget_tokens` values keep their cap; `max_items` remains at most 6. This is not a `config.json` leaf.

Codex MCP `recall` advertises the same whole-packet estimate, including packet and source metadata. Omission defaults to `4096` for explicit retrieval; small explicit values can still clip every item. A packet that cannot fit even the minimal honest envelope is rejected. On `budget_token_cap` or `budget_packet_cap`, retry at most once with `4096`; this does not raise the automatic ceiling. The separate `profile` and `entity` read views retain their documented UTF-8 byte budgets.

## Automatic recall latency budget

Automatic remote semantic recall defaults to `auto_recall_seconds=5.0`, which is also the hard maximum. The trusted-host fallback hook budget defaults to `hook_processing_seconds=6.0`, which is also the hard maximum, so the hook can cover a full automatic recall. A 1.5-second automatic default leaves typical hosted Gemini query embeddings about 1.1 seconds and times out; the same query, database, and configuration complete in about 2 seconds when the existing allowed 5-second maximum is used. Timeouts remain in force and degrade to lexical recall. Callers and `runtime-config.json` may set a stricter timeout; that explicit shorter deadline is honored and is clamped by the configured automatic budget. This does not raise money, token, call, packet, or source caps, disable TLS validation, or lower the raw vector 0.70 safety floor.
