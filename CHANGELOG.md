# Changelog

A line or two per release.  Each release's full notes are on its GitHub release page (tag ``v<version>``), and the
longer text this file once held is in its history.

## [Unreleased]

## [3.9.2] - 2026-10-08

3.9.2 changes no behaviour. It is the clean-up's last part: the store transaction and the core are split by what they do (`tx.sources`, `tx.registry`, `core.operations`, `core.records`), the clients' installers share their common functions, logic that was copied lives in one function (duplicated lines in production code 940 to 319), comments say what the code does rather than how it came about, test files are named for what they test, and this changelog keeps a line or two per release (each release's full notes are on its GitHub release page).

### Upgrading from 3.9.1

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the Hermes gateways and the clients' MCP servers. The schema is unchanged (1110), and so are the hook and MCP server commands the installers write. A script that called a moved method calls it on its part: `core.operations.respace_embeddings` (and the other maintenance passes), `core.records.schedule_source` (and the other record calls), `tx.sources.put_source`, `tx.registry.entries`.

## [3.9.1] - 2026-10-08
3.9.1 changes no behaviour. It is the clean-up's second part: stored content, recall results, hook and CLI output, log lines, configuration formats and the modules installed hosts run are those of 3.9.0.

## [3.9.0] - 2026-10-07
3.9.0 changes no behaviour. It is the first part of a clean-up: stored content, recall results, hook and CLI output, configuration formats and the modules installed hosts run are those of 3.8.2.

## [3.8.2] - 2026-10-07
3.8.2 lets a Hermes agent on Gemini use its memory tools (#216, reported by @momolee-deep).

## [3.8.1] - 2026-10-07
3.8.1 keeps a worker up until the candidates of a conversation's last messages settle (#214, reported and measured by @849506054).

## [3.8.0] - 2026-10-06
3.8.0 re-embeds a store into a new embedding space when an operator asks for it, and the doctor says when embeddings have waited a day.

## [3.7.8] - 2026-10-06
3.7.8 keeps what a failed tool call printed in Hermes.

## [3.7.7] - 2026-10-06
3.7.7 makes an automatic recall read what it needs once, and together: a recall that ran 16,222 statements and read 7,087 rows runs 469 and reads 755, and finds the same.

## [3.7.6] - 2026-10-06
3.7.6 keeps a capture's write from growing with its words, its session's episode, the store's scopes and the candidates its words reach. In a busy Hermes gateway one long tool output held the shared store's writer lease for 30 to 43 s, four times in two days.

## [3.7.5] - 2026-10-06
3.7.5 brings back the vector work of claims a provider failed. The automatic recovery read every embedding's subject as a source, so a claim's failed embedding was made obsolete instead of retried.

## [3.7.4] - 2026-10-06
3.7.4 indexes a withheld tool output's placeholder by the tool's own error text alone, and adds `unindex-withheld-outputs` to drop the rest of what earlier releases indexed.

## [3.7.3] - 2026-10-05
3.7.3 reads a Hermes message by its own words, as Hermes does. A notice stays the host's when a compression folded its summary or a to-do list into it. 3.7.3 also lets `retry-failures` clear a failure of any HTTP status.

## [3.7.2] - 2026-10-05
3.7.2 lets a question reach the reply to what the person added before that reply came. It stores the messages Hermes writes into a conversation itself as the host's, not the owner's, and it lets `retry-failures` clear `http_protocol` failures.

## [3.7.1] - 2026-10-05
3.7.1 lets the MCP tools say why they refused a call, and recognises an older copy of the current message whatever its closing punctuation, as long as both ask or neither does.

## [3.7.0] - 2026-10-04
3.7.0 lets DeepSeek Harness (dsh) join a shared store: its prompts are recalled before each turn and each turn's messages are stored, by a dsh plugin that runs the entry's hooks.

## [3.6.2] - 2026-10-04
3.6.2 stops a WorkBuddy entry from storing an error notice as WorkBuddy's reply, and says how long its resident recall server really lives.

## [3.6.1] - 2026-10-04
3.6.1 stores the Hermes tool results that met a busy store, where some were lost.

## [3.6.0] - 2026-10-04
3.6.0 recalls a WorkBuddy entry's prompts with the vector search, a new conversation's first prompt included.

## [3.5.1] - 2026-10-03
3.5.1 keeps every tool result of a Hermes step whose tools run in parallel.

## [3.5.0] - 2026-10-03
3.5.0 brings WorkBuddy into the shared store, and keeps the first recall after an idle stretch whole.

## [3.5.0 candidates] - 2026-10-02 to 2026-10-03
The version moves past the `v3.5.0rc3` tag.

## [3.4.10] - 2026-10-01
3.4.10 fixes three faults reported on GitHub, all on Hermes. A session's hooks could wait out Hermes' hook timeout and then be skipped for every session (#169).

## [3.4.9] - 2026-10-01
3.4.9 lets prompts that come together on an entry's server search by meaning: every handler of the server shares one LanceDB helper. In every process, a vector store left with no table is opened again instead of failing every search until the process ends.

## [3.4.8] - 2026-09-30
3.4.8 answers a question about what was said or done on a day, or by one entry of the shared store on a day, from that day's conversation. Any other message is recalled as on 3.4.7.

## [3.4.7] - 2026-09-30
3.4.7 puts what a question was told the last time it was asked above the best candidate of that time in its automatic recall, and gives the vector threshold a shared store needs in the shipped embedding space.

## [3.4.6] - 2026-09-30
3.4.6 stops a message being lost when another message under the same key still waits in the capture inbox, as Codex's messages sent into a running turn were, and keeps such a message when the other one is deleted.

## [3.4.5] - 2026-09-30
3.4.5 keeps a slow statement from holding up a recall, lets a recall's diagnostic ref be read, and stops `doctor` calling a busy worker failed. Nothing else changes.

## [3.4.4] - 2026-09-30
3.4.4 gives a question asked again what it was told before, stops Hermes storing a turn's message twice, and keeps one recall channel off the whole work queue. Nothing else changes.

## [3.4.3] - 2026-09-30
3.4.3 keeps a long message's recall from stalling. For a long enough message the word search read every event of the conversation's audience one by one: on a copy of the shared store the word search for a Telegram message of 72 characters took 18-21 s, and its recall came back empty at every stage's deadline.

## [3.4.2] - 2026-09-29
3.4.2 keeps a long prompt's recall within its time. The word search of a prompt looked up every word it held: a 2,000-character prompt's 80 words held 273,000 index entries on the shared store and took 9 s, longer than the prompt's whole recall, which then went without its search by meaning as well (`deadline_exceeded_collect`).

## [3.4.1] - 2026-09-29
3.4.1 makes automatic recall search by meaning on the prompts where it fell back to words alone. On 2026-09-29, 4 of 9 prompts from Claude Code on another computer were recalled by words alone, and so was the first prompt of a Claude Code session at home and a prompt after a pause; each recall said so in its gaps.

## [3.4.0] - 2026-09-29
3.4.0 lets Claude Code and Codex on another computer use the shared store, and makes capture, automatic recall and deletion hold up on a large, busy store. A client on another computer is an entry of the store under a name of its own: its hooks forward each event to a server on the store's machine over a private network, and its MCP tools are served from...

## [3.4.0 candidates] - 2026-09-26 to 2026-09-29
The version moves past the `v3.4.0rc12` tag.

## [3.3.0] - 2026-09-26
3.3.0 lets Hermes, Codex and Claude Code share one memory. In 3.2.0 only Hermes agents could attach to a shared store; now Codex and Claude Code attach to the same store as entries.

## [3.3.0 candidates] - 2026-09-24 to 2026-09-26
The changes as they were written when each landed, from the release audit back to 3.2.1rc1, which became 3.3.0rc1.

## [3.2.0] - 2026-09-24
3.2.0 lets several agents share one memory. Until now each agent had a store of its own, and what you told one of them the others could not recall. Attach each agent to a shared store, and what you tell one agent the others can recall; each recalled item says which agent it came in through, and a deletion through any agent applies to all of them.

## [3.1.2] - 2026-09-21
3.1.2 is three things that turned up on the day 3.1.1 went out, two of them while rolling 3.1.1 onto our own instances. What is remembered and how it is asked for do not change, and there is no schema step.

## [3.1.1] - 2026-09-21
3.1.1 is what running 3.1.0 on real instances, ours and yours, turned up, fixed. What is remembered and how it is asked for do not change, and there is no new concept to learn.

## [3.1.0] - 2026-09-18
3.1.0 rebuilt the project (production code from 141,044 lines to 48,289): what is said is stored word for word first and becomes a fact only when its evidence is enough, a fact keeps its versions, and relevant memory reaches the model by itself. A 2.0.1 store goes through a migration (section 9 of its release notes).

## [2.0.1] - 2026-08-30
This patch is cumulative since the last public release, `2.0.0`. It completes the production managed upgrade path for ordinary users and hardens the 2.0 memory runtime: one fixed official stable source, an external resumable idempotent operation journal, strict state transitions, exact-Hermes-home restart control, zero-signal recall admission, candidate...

## [2.0.0] - 2026-08-27
This release candidate is cumulative since the last public release, `1.10.3`. It completes the Scope Recall 2.0 product contract while preserving SQLite truth, stable V1 provider/tool identities, additive migration, and the N-1/N/N-1 compatibility window.

## [1.10.6] - 2026-08-26
This patch candidate is cumulative since the last public release, `1.10.3`, and supersedes the unpublished `1.10.4` and `1.10.5` source checkpoints. It completes Scope Recall 2.0 Program 0A/0B without crossing G0: release controls are deterministic, Vector status has one public contract, and legacy relation fan-out is replaced by finite relation containment.

## [1.10.5] - 2026-08-25
This patch candidate is cumulative since the last public release, `1.10.3`, and supersedes the unpublished `1.10.4` source checkpoint. It closes the remaining bounded-concurrency, release-provenance, and distribution-scanner defects found by exact-epoch review while retaining the issue #50 contract, without changing SQLite authority or stable...

## [1.10.4] - 2026-08-23
This patch candidate is cumulative since the last public release, `1.10.3`. It closes post-release governance and scheduling gaps around issue #50 without changing SQLite authority or stable provider/tool identities.

## [1.10.3] - 2026-08-23
This patch is cumulative since the last public release, `1.10.2`. It fixes issue #50 by recognizing the official `memory_auto_adjudication` + `archive` receipt in governance coverage and cleanup rollback without trusting arbitrary archive writers.

## [1.10.2] - 2026-08-21
This patch source candidate is cumulative since the last public release, `1.9.2`. It incorporates and supersedes the untagged `1.10.0` public source candidate and the `1.10.1` public source candidate that reached the public tree.

## [1.10.1] - 2026-08-20
This patch source candidate is cumulative since the last public release, `1.9.2`. It incorporates and supersedes the untagged `1.10.0` public source candidate that reached `main` without a tag, GitHub Release, or PyPI artifact.

## [1.10.0] - 2026-08-19
This minor source candidate covers public journal restore, backlog fairness, vector inventory, and runtime-module convergence since the last public release, `1.9.2`.

## [1.9.3] - 2026-08-14
This compatibility-preserving source candidate covered the highest-priority open SQLite contention and writer-ownership risks since the last public release, `1.9.2`.

## [1.9.2] - 2026-08-09
This cumulative patch release covers runtime reliability and recall-precision fixes since the last public release, `1.9.1`. SQLite remains authoritative, derived vector state remains replayable, and the stable provider/tool identities are unchanged.

## [1.9.1] - 2026-08-08
This cumulative public release covers all changes since the last public release, `1.8.7`. The version path is documented explicitly because `1.8.8` and `1.8.9` were development intervals rather than tagged package candidates, and `1.9.0` reached `main` as a source candidate but was never tagged, released, or uploaded to PyPI.

## [1.9.0] - 2026-08-06
The 1.9.0 source candidate was pushed to `main` but was not tagged or published. It was superseded by 1.9.1 after cross-platform CI exposed a POSIX-only test-fixture permission mismatch; runtime safety behavior was unchanged.

## [1.8.7] - 2026-08-03
This cumulative release covers all changes since the last public release, `1.8.2`. It keeps SQLite authoritative and the stable provider/tool identities unchanged while combining the 1.8.3-1.8.6 reliability line with final identity, freshness, secret-handling, and cross-platform release hardening.

## [1.8.6] - 2026-08-01
Made legacy fact-freshness backfill quarantine invalid validator metadata, continue past malformed rows, and re-scan under an immediate owner transaction; startup now defers recoverable SQLite contention explicitly.

## [1.8.5] - 2026-08-01
Replaced Windows activation-lease PID probing through `os.kill(pid, 0)` with a read-only process-handle query, preventing child doctor checks from sending `CTRL_C_EVENT` to a process-group owner.

## [1.8.4] - 2026-08-01
Added dry-run-first operator recovery for stale activation leases, legacy freshness coverage, and vector outbox dead-letter events, with verified SQLite backups, idempotent operator-ledger evidence, and mirrored receipts.

## [1.8.3] - 2026-07-31
Added a public 72-pair `gemini-embedding-001` vector-only threshold calibration fixture, a metric gate, bounded completed-outbox retention, and platform-native recovery-command generation.

## [1.8.2] - 2026-07-28
Added a durable, cursor-based relation rebuild queue with bounded foreground synchronization, monotonic lifetime/pass progress, next-revision handoff, background draining, read-only debt reporting, and backup-first repair tooling.

## [1.8.1] - 2026-07-23
Made the dependency-free SQLite vector fallback portable to Windows by applying descriptor-based POSIX mode hardening only where CPython exposes `os.fchmod`; Windows continues to rely on the inherited profile-directory ACL boundary.

## [1.8.0] - 2026-07-15
Added opt-in structured Fact Evolution with temporal current/as-of/history queries, reviewed mutation receipts, and deterministic release benchmarks for scope routing, evidence authority, replay safety, and journal checkpoint atomicity.

## [1.7.2] - 2026-07-12
Added immutable vector-generation manifests with compare-and-swap activation, migration receipts, durable replay outbox handling, and explicitly activated shadow builds.

## [1.7.1] - 2026-07-08
Kept runtime config diagnostics out of persisted operator config by filtering internal `_...` keys from both loaded config state and incoming dotted updates before writing `config.json`.

## [1.7.0] - 2026-07-08
Added event-digest evidence packets and reviewable candidate extraction with dry-run-first storage controls.

## [1.6.3] - 2026-07-07
Closed the SQLite write-lock recovery gap from issue #25 by adding conservative `scope_recall_store` auto-recovery for recoverable SQLite lock/transaction errors: the provider rolls back/probes/reopens the shared connection if needed, retries the store once with identical arguments, and returns...

## [1.6.2] - 2026-07-07
Added `scripts/backfill.graph_relations.py`, a dry-run-by-default deterministic graph backfill that creates same-scope `supersedes` edges from trusted `metadata.superseded_by` provenance.

## [1.6.1] - 2026-06-30
Published documentation, packaging, and release-provenance updates as a dedicated patch release after `v1.6.0` had already been tagged and published.

## [1.6.0] - 2026-06-29
Added production packaging and rollout surfaces: dry-run-by-default installer rollback/apply flows, operator runbooks, cross-profile rollout planning, response-contract documentation, and release-gate wheel/install/doctor smoke checks.

## [1.5.3] - 2026-06-26
Added `scripts/repair.graph_hygiene.py`, a dry-run-by-default maintenance script that reports and, with `--apply`, removes orphan `memory_entities` / `memory_relations` rows from the rebuildable SQLite graph companion.

## [1.5.2] - 2026-06-25
Added Recall Funnel traces for search/explain/benchmark paths, including candidate-pool sizing, per-stage candidate counts, filter counts, returned ids/chars, and retrieval timings.

## [1.5.1] - 2026-06-24
Fixed strict release-gate dirty-tree checks in CI by ignoring known local/runtime scratch directories such as `.hermes-agent-src/` while still blocking real tracked or untracked source changes.

## [1.5.0] - 2026-06-24
Added governance cleanup, journal recovery, operator dashboard, and repository-owned golden benchmark release-readiness tooling.

## [1.4.5] - 2026-06-24
Expanded `scope_recall_explain` so each returned row includes rank-aligned retrieval evidence for lexical/BM25/vector/RRF scores, metadata quality adjustment, entity overlap/distance bonuses, relation evidence/rerank contribution, memory-type temporal policy, temporal decay, recency bonus...

## [1.4.4] - 2026-06-23
Added `docs/contract.matrix.md`, a maintainer gate matrix that maps each major scope-recall contract to source files, targeted tests, release gates, and dynamic probes so large-context changes do not rely on an agent remembering the whole plugin.

## [1.4.3] - 2026-06-20
This is the first public release after `v1.4.0`; the GitHub release notes for `v1.4.3` include the cumulative `v1.4.1`, `v1.4.2`, and `v1.4.3` changes.

## [1.4.2] - 2026-06-20
Clarified Experience Kernel runtime docs so default prefetch and operator-enabled automatic promotion are described as separate controls.

## [1.4.1] - 2026-06-19
Kept Experience preflight packet injection enabled by default but made background reusable-experience promotion opt-in (`experience.auto_promotion_enabled=false`) until the review queue has enough field feedback.

## [1.4.0] - 2026-06-17
Added the conservative Experience Kernel MVP: procedural playbook schema/tables, deterministic `procedural_playbook.v1` validation with per-step `capability_class`, scope-filtered playbook create/search/inspect/preflight/review/feedback/stats tools, feedback run counters, bounded preflight...

## [1.3.0] - 2026-06-14
Added `scope_recall_profile`, a compact high-level profile/context surface over accessible durable `user`/`memory`/`project`/`ops` rows, optional local `general` scratch, and live Hermes curated `USER.md`/`MEMORY.md` entries.

## [1.2.1] - 2026-06-14
Preserved surrounding user text when gateway image attachment markers or local `image_cache/img_*` paths appear inline rather than on their own line, while still stripping the attachment metadata before journal/capture storage.

## [1.2.0] - 2026-06-14
Added `ScopeRecallMemoryProvider.on_pre_compress()` so Hermes context-compression boundaries stage sanitized user/assistant messages into the journal before old turns are summarized/discarded.

## [1.1.2] - 2026-06-14
Sanitized gateway image attachment markers before capture/journal storage, removing local `image_cache/img_*` paths and inline image placeholders while preserving the user's surrounding text.

## [1.1.1] - 2026-06-14
Treated short assistant acknowledgement messages such as `Understood.`, `Noted.`, and common Chinese ACKs as trivial capture input so they cannot enter the journal.

## [1.1.0] - 2026-06-14
Added the `hermes-scope-recall` standalone distribution shape with a `hermes-scope-recall` console script.

## [1.0.16] - 2026-06-14
Probed LanceDB/PyArrow native imports in a child process before importing them inside Hermes, so no-AVX/AVX2 hosts that hit `Illegal instruction` are treated as unsupported instead of crashing the agent process.

## [1.0.15] - 2026-06-13
Reused one chat-completions endpoint builder across capture, journal, and nightly digest paths so provider-specific endpoints and `append_v1=false` are honored consistently.

## [1.0.14] - 2026-06-13
Added opt-in canonical identity mapping for cross-platform durable recall.

## [1.0.13] - 2026-06-12
Added lifecycle-aware conflict review: newly inserted contradictory durable memories now record bidirectional `contradicts` relations plus `needs_conflict_review` metadata without automatically superseding or hiding older rows.

## [1.0.12] - 2026-06-12
Added journal-first provenance capture with `journal_entries`, `journal_digest_runs`, and `memory_journal_sources` tables.

## [1.0.11] - 2026-06-11
Added a `MiniMaxEmbedder` (provider: `minimax`) and a `build_embedder` route for the MiniMax `embo-01` embedding endpoint.

## [1.0.10] - 2026-06-10
Added deterministic external-artifact enrichment for direct memory writes and nightly digest candidates.

## [1.0.9] - 2026-06-09
Added the `sqlite-bruteforce` vector backend for non-AVX or native-dependency-sensitive hosts.

## [1.0.8] - 2026-06-03
Added deterministic Chinese entity fallback hints so compound input-method terms such as `自然码` and `双拼` are extracted even when Jieba is unavailable or segments differently in CI/runtime environments.

## [1.0.7] - 2026-06-03
Added `scripts/doctor.py`, a read-only source/runtime health report that checks release metadata alignment, SQLite truth availability, LanceDB companion readability, and repair recommendations.

## [1.0.6] - 2026-06-01
Added `capture_llm` module: LLM-powered semantic extraction of user+assistant turns into classified durable memory (preference, workflow, pitfall, decision, etc.) with user-configurable model and endpoint.

## [1.0.5] - 2026-06-01
Added `scripts/nightly-digest.py`, a profile-scoped daily conversation digest that reads Hermes `state.db`/legacy `lcm.db`, extracts durable memories, writes through the SQLite truth store, syncs the LanceDB companion when enabled, and records digest run/source ledgers.

## [1.0.4] - 2026-05-31
Added a local SQLite graph layer with `memory_entities` and `memory_feedback` tables.

## [1.0.3] - 2026-05-20
Added structured memory classification metadata for new writes, including category, tier, kind, lifecycle, authority, confidence, sensitivity, expiry, entity, tag, and scope-mode fields.

## [1.0.2] - 2026-05-18
Added `capture_filters.py` to centralize automatic capture hygiene and block runtime-wrapper text such as recent Telegram context, context-compaction handoffs, skill-review meta prompts, and secret-like literals before they enter SQLite or vector storage.

## [1.0.1] - 2026-05-16
Scoped all ID-based write paths (`scope_recall_update`, `scope_recall_merge`, query-driven delete plumbing, and dedupe deletes) to the current accessible scope set so a caller that learns an inaccessible memory id cannot update, merge, or delete that row from a different user, sibling agent, or...

## [1.0.0] - 2026-05-15
Declared the first stable V1 release line with explicit provider identity, storage, tool, retrieval, migration, and runtime-freshness contracts in `docs/stability.md`.

## [0.2.0] - 2026-05-12
Added vector audit stats for physical LanceDB row count, unique id count, and duplicate extra row count.

## 2026-05-20 — Retrieval hygiene regression
Removed arbitrary recent-memory backfill from lexical SQLite retrieval.
