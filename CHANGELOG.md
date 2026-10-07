# Changelog

All notable changes to `scope-recall` will be documented in this file.

## [Unreleased]

## [3.9.0] - 2026-10-07

3.9.0 changes no behaviour. It is the first part of a clean-up: stored content, recall results, hook and CLI output, configuration formats and the modules installed hosts run are those of 3.8.2.

- **One format and a quality check.** `ruff format` (line length 120) formats the tree, tests included; `git blame --ignore-revs-file .git-blame-ignore-revs` skips that commit. CI's `lint` job runs `scripts/quality.py`: formatting, and no more ruff or pyright findings per file and rule than `scripts/quality.baseline.json` records; a function over a size limit may not grow. ruff and pyright are pinned in the `dev` extra and `uv.lock` (CONTRIBUTING.md).
- **The hook and MCP clients live in `adapters/clients`.** Codex, Claude Code, WorkBuddy, dsh and the remote client share that layer; `adapters/codex` keeps only the five modules installed configurations run (`hook_entry`, `mcp_entry`, `remote_client`, `remote_server`, `resident_entry`), so installed hooks keep their trust and MCP servers their approval. A script that imported another module of `scope_recall.adapters.codex` imports it from `scope_recall.adapters.clients`.
- **The hook handler and the Hermes adapter are split by what they do**: a prompt's recall, reading a session record and each hook event; a capture, its retry, a turn, a turn's recall and the session binding. No file of either is over 600 lines, and no function of either is over complexity 25.

### Upgrading from 3.8.2

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the Hermes gateways and the clients' MCP servers. The schema is unchanged (1110), and so are the hook and MCP server commands the installers write.

## [3.8.2] - 2026-10-07

3.8.2 lets a Hermes agent on Gemini use its memory tools (#216, reported by @momolee-deep).

### Fixes

- **Gemini accepts Hermes's memory tools.** `revise` declared its new value as any type, an array among them without `items`, and Gemini refuses a request when any tool it carries does: through Hermes's own Gemini client every request with the tools failed with HTTP 400. The new value is now declared as what the core takes: the new value's text, an object of the fact's fields, or null to withdraw it. The core refused a number, true or false and a list anyway.
- **`revise` with a null value withdraws a fact through the MCP server too** (Codex, Claude Code, WorkBuddy, dsh). The server dropped every argument left empty, a null new value included, and the core refused the call; the person's request was kept, but nothing was withdrawn. A capture still withdraws on its own a fact the person's request names alone; the tool is for a request that fits more than one.

### Tests

- The storage tier runs a known-answer check of three memory invariants on a fresh synthetic store: a fact needs a person's source and a worker pass, revise and forget need the person's own request in the same session naming the exact version, and nothing forgotten comes back, also after the vector companion is rebuilt. It also runs on its own against an installed release (`tests/known_answer/`, #213, by @Adam13y).

### Upgrading from 3.8.1

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the Hermes gateways. The schema is unchanged (1110).

## [3.8.1] - 2026-10-07

3.8.1 keeps a worker up until the candidates of a conversation's last messages settle (#214, reported and measured by @849506054).

### Fixes

- **A worker waits for a settling candidate.** A candidate inside its 15-minute quiet window is in no queue, so the pass after a channel's last message found nothing due and its supervisor stood down before the window closed; the candidate waited for the channel's next session. The wake plan now names the moment it becomes ready (`candidate_settle_window`), also for evidence that came while an earlier question about it waited. The supervisor records the last pass whose sweep saw every ready candidate (`last_pass_at`), so a candidate with nothing new to ask, or whose last question held all its evidence, does not wake the worker again; a pass that could not sweep holds that wake for 15 minutes.
- **The doctor names work, and candidates still marked with new evidence, that have waited a day in any partition of the store** (`due_work_unreached`, attention). Its other work-queue figures still cover its own audience only.
- **Outside Windows, `autostart plan` prints the wake as a systemd user timer and a cron line**, and `enable` writes the control file the wake reads (`docs/install.md`, section 7).

### Upgrading from 3.8.0

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the Hermes gateways and the clients' MCP servers. The schema is unchanged (1110). On Linux and macOS a wake needs the timer of `docs/install.md` section 7. Right after the upgrade the doctor may name work that has long waited (`due_work_unreached`, attention).

## [3.8.0] - 2026-10-06

3.8.0 re-embeds a store into a new embedding space when an operator asks for it, and the doctor says when embeddings have waited a day.

### Added

- **`respace-embeddings` re-embeds what was embedded so far after the embedding model changed.** Found and reproduced by @Vivamisu (#200). `work_items` is unique on its type and subject and says nothing of the space a vector was made in, so an embedding done in one space stayed done when the model changed. The new space received only what came in afterwards, and older memory was found by its words alone.
  - `respace-embeddings --config <file> --start --apply` starts a run over every embedding done so far. Every source and claim is then embedded again and paid for, so a run starts only when asked.
  - Each worker pass in that space reopens a page of done embeddings, newest first. It does so only while fewer than 64 embeddings wait anywhere in the store, or half a pass's worth while candidate evaluations are ready, so what is captured meanwhile is embedded first. A page looks through at most 4,096 work ids. Embeddings of a project whose worker never runs hold the run.
  - A reopened row is pending like new work: its attempts count afresh and any lease on it is fenced off. A tool output whose vector the retention window removed stays without one. Work queued after the start is embedded into the new space as it arrives and is never reopened. The run reopens everything finished before it starts, so whatever waited at the switch or was embedded into the new space before the start is paid for twice: the docs say to let the queue come down before switching, while the old route still answers, and to start right after. The preview shows how many wait (`waiting`).
  - The run lives in SQLite, as one of the store's named cursors, so there is no schema change. `--start` refuses while a run is going and after one into the same space has finished; `--restart` starts again from the newest, `--cancel` stops it, and the plain command shows the run and what it still has to reopen.
  - A worker in another space leaves the run alone, and the doctor names that (`embedding_respace_space_mismatch`). A page that failed is reported in the worker's status (`embedding_respace_failed:<Error>`) and tried again on the next pass.
  - `tests/contract/test_embedding_respace.py`; the model switch is also run end to end on the native vector store (`tests/storage_native/test_runtime_instance_seam.py`).
- **The doctor reports the embedding queue and the provider beside it** (`embedding_health`), as suggested in #200. It shows pending and failed embeddings and the oldest one waiting; with an external route, also the provider's hold and its answers over the last day.
  - With an external embedding route, embeddings that have waited more than 24 hours raise `embedding_backlog_aged`, with the hold and the refusals in the check's detail, or a pointer to the worker when the provider refused nothing. While they wait, recall answers by words alone, and before this nothing said so.

### Upgrading from 3.7.8

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the Hermes gateways and the clients' MCP servers, so that every entry of a shared store runs one version. The store's schema is unchanged (1110), and nothing needs running once. After a change of embedding model, see `docs/configuration.md`, "Changing the embedding model rebuilds the vector store".

## [3.7.8] - 2026-10-06

3.7.8 keeps what a failed tool call printed in Hermes.

### Fixes

- **Hermes keeps a tool call that failed.** Hermes calls a tool result failed when a command exits non-zero or the result carries an error field. The plugin then refused the result as having no scope: what the agent had seen (a traceback, a failing test, a refused command) was never stored, and each one was logged as a failed capture (`not stored (capability_gap)`). On the five instances that was 460 tool results from 2026-10-01 to 2026-10-06, about 6% of their tool output. Codex already kept its failed calls.
  - A failed, cancelled or interrupted call that returned something is now stored like any other tool output: indexed by its words and queued for an embedding, and, like any tool output, rooting no claim and no resume field. It is stored `partial`.
  - It does not end its task either. A tool output's non-zero exit code marks its task failed, and a grep that finds nothing exits 1; a `partial` output tells its own outcome, not the task's, so it now leaves the task's state alone (`core/episodes.py`). Resume offers only an open or interrupted task, so a task marked failed this way would have dropped out of it. This holds for every `partial` tool output, including one the hook adapter of Codex, Claude Code, WorkBuddy and dsh stores cut off past 65,536 characters; a person's or the host's own words still move the task as before.
  - A call whose result is empty keeps only its outcome, in the session's diagnostics, and is no longer logged as a failed capture.
  - `tests/host/hermes/test_bounded_corrections.py`, `tests/contract/test_v11_episode_authority.py`.

### Upgrading from 3.7.7

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the Hermes gateways and the clients' MCP servers, so that every entry of a shared store runs one version. The store's schema is unchanged (1110), and nothing needs running once.

## [3.7.7] - 2026-10-06

3.7.7 makes an automatic recall read what it needs once, and together. A recall on yuheng's questions ran 16,222 statements and read 7,087 rows; it now runs 469 and reads 755, and finds the same.

### Fixes

- **A read transaction keeps what it loaded, and hydration loads its candidates together.**
  - A recall loaded each evidence source of its candidates up to five times (whether it may be delivered, whether it is its group's newest version, its origin, its context, its entry), each load three statements: two for visibility and one for the row. Each candidate claim's versions were read on their own, and so was every candidate's visibility.
  - In a Hermes gateway every statement and every row waits for the GIL while another thread is busy (3.7.6). Beside one thread that kept the CPU busy, yuheng's recall of 12 real questions ran past its 5 s deadline in every stage and found none of them.
  - A read transaction reads one snapshot, so what it loaded stays true until it ends: it now keeps sources, their visibility, whether each is its group's newest, and claims' versions (`Transaction.remembered`). It keeps at most 16,384 answers and 32 million characters of text (about 70 MB in Python), so one that reads a whole store keeps no more; over 483 recalls the largest kept 13 million. A write transaction keeps nothing, and a transaction lets go of what it kept when it ends.
  - Before hydrating, a recall loads every candidate's visibility, the events' rows and the claims' versions in a few statements, and a claim's evidence in three whatever its count: its lineage, its visibility and its rows (`RetrievalStorage.prefetch`, `Transaction.prefetch_sources`, `Claims.prefetch_versions`). Hydrating a claim of 16 sources takes 15 statements, as one of 2 does, where they took 221 and 39. Nothing is loaded ahead once the deadline is gone. Every reader still gets objects of its own.
  - The same packets: on a copy of the shared store, words only and at one fixed time, 483 of 483 cases gave the identical packet, byte for byte but its diagnostic ref (173 real questions from eight entries, Codex having none, and 310 of the older sets).
  - Faster alone, and much faster beside a busy thread. Over the 173 real questions, the two releases alternated twice on the same copy, the median recall went from 0.78 s to 0.72 s and the 90th percentile from 0.99 s to 0.91 s; on yuheng's 12 questions beside a thread busy 10% or 30% of the time, from 1.64 s to 1.31 s and from 1.95 s to 1.45 s, all 12 found either way. Beside a thread that keeps the CPU busy all the time, from 9.4 s to 6.5 s: still past the deadline.
  - `tests/contract/test_recall_row_crossings.py`: loaded together reads as loaded alone (a part of a message whose other part never came, a whole one, a version a newer one replaced, a claim of two versions), nothing is read twice, readers never share an event, a write transaction reads what it wrote, past its limits a transaction answers alike, hydrating a claim of 16 sources costs what one of 2 does, a recall answers alike whether or not it keeps what it loads, and what is loaded together keeps the reader's audience: another project's source or claim, a source blocked from reading, a claim in a scope the reader lacks, and a part whose other part was blocked (review of 3.7.7).

### Known limits

- Beside a thread that keeps the CPU busy all the time, a recall still runs past its 5 s deadline; about 470 statements and 750 rows remain, and its own Python work runs at half speed. Hermes logged no prefetch past its own limit in the last six days.
- A person's message that reads as a correction still loads the versions of every claim it may correct, up to 200, inside its write (3.7.6).

### Upgrading from 3.7.6

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110), and nothing needs running once.

## [3.7.6] - 2026-10-06

3.7.6 keeps a capture's write from growing with its words, its session's episode, the store's scopes and the candidates its words reach. In a busy Hermes gateway one long tool output held the shared store's writer lease for 30 to 43 s, four times in two days. Every other entry's write failed meanwhile, and Hermes skipped the tool hook of every session for a minute, so the tool results of that minute were never stored.

### Fixes

- **A capture's write no longer grows with its words, its episode, the store's scopes or the candidates its words reach.**
  - Every statement, and every row read in Python, hands Python's GIL over and back. In a Hermes gateway whose other threads were busy, each handoff waited out their switch interval.
  - A capture wrote two statements per term and read its terms back one row at a time. On yuheng, terminal outputs of 52,137 characters took 37.1 s (2026-10-04 12:22), of 51,283 characters (9,348 terms) 42.9 s (10-05 15:12) and of 25,067 characters 30.3 s (20:40); on tianshu a file of 11,502 characters took 36.2 s (10-05 12:43), beside ten other captures of the same step.
  - Each of those writes was stored at once and committed tens of seconds later, holding the writer lease all that time. The other entries' captures failed with it, and were kept to retry. Hermes gave up on the hook at 30 s and skipped it for every session for the next minute: 36 tool results of those two days were never stored.
  - Now the terms go in with one statement each way and come back in one row (`lexical_index.index_terms`, `terms_of`). Joining an episode reads its members' lineage, states, visibility, latest time and resume proofs in one row each, where it read up to 200 rows and ran two statements per member (`episode_storage`, `lineage.evidence`, `visibility.allowed_refs`). Every transaction counts the store's scopes in one row, where it read the shared store's 760 one by one (`storage._verify`). A source that is not first-hand, such as what the assistant said, reads the candidates it shares a word with in one row, where it read one row per candidate until sixteen were restated, and the match starts from its words: from the candidates, SQLite looked every word up for each candidate it could reach. On a copy of the shared store, matching an entry's 40 latest messages took 1.3 s where it took 3.2-3.8 s, and found the same candidates for every one (`candidate_intake`, review of 3.7.6).
  - A source restating a muted claim is still muted with it, and the muted claims are now picked before the content is searched. SQLite had searched the content for the subject and predicate of every claim in the scope first: 8,995 claims for yuheng's, 11 of them muted, 1 s for that output.
  - Replayed on a copy of the shared store with one busy thread beside it, as a gateway has, the 51,283-character write took 408 s and now takes 7.9 s; 7.6 s for the 25,067-character one and 7.1 s for 18 characters. Alone, 1.6 s became 0.65 s.
  - `tests/contract/test_capture_row_crossings.py` counts what crosses the boundary: a tool output of 3,000 terms against one of a few, a capture joining an episode of 152 members against one joining an episode of 1, a store of 502 scopes against one of 2, and what the assistant said beside 65 candidates against 5.
- **A part of more distinct terms than SQLite takes parameters is stored (review of 3.7.6).** Matching a source to the candidates bound one parameter per term. A part of 64,000 characters can hold more distinct terms than SQLite takes parameters (32,766 by default), and the whole capture then failed as the store being unavailable, to be kept and retried for good. The terms now go in as one parameter.

### Known limits

- Two paths of a capture still read per item. A person's message that reads as a correction or a confirmation loads the versions of every claim it may touch, up to 200 (1,311 statements in a scope of 252 claims, against about 100 for a plain message). And each further part of a message over 65,536 characters adds about 90 statements.
- An automatic recall reads its candidates one statement and one row at a time too. Replayed on the same copy beside one thread that keeps the CPU busy, yuheng's recall of 12 real questions ran past its 5 s deadline in every stage and found none of them; alone it finds all 12 in about 1.5 s, and beside a thread busy 10% or 30% of the time it still finds all 12. Hermes logged no prefetch past its own limit in the last six days. This is next.

### Upgrading from 3.7.5

Install the package, run `plan-install` and `apply-install` where you upgrade, then restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110), and nothing needs running once. From 3.7.4, also run 3.7.5's `retry-failures` step below.

## [3.7.5] - 2026-10-06

3.7.5 brings back the vector work of claims a provider failed. The automatic recovery read every embedding's subject as a source, so a claim's failed embedding was made obsolete instead of retried. On the shared store, 116 readable claim heads had no vector and were found by their words alone. `retry-failures` now brings back the vector work of 115 of them; the other one was refused on purpose.

### Fixes

- **The automatic recovery retries a claim's embedding (`_embed_retry_reason`).** Every claim head is queued for the vector index, and its embedding can fail as a source's does: a network error, a timeout, a lease that ran out.
  - The recovery reopens such failures after its cooldown. It checked an embedding's subject as a source, though, and a claim is no source, so the claim's embedding was made obsolete (`authority_revoked`).
  - Now a claim's embedding is reopened while its revision is the readable head it was queued for. An older revision's is still made obsolete: only the head needs a vector.
- **`retry-failures` reopens what the recovery dropped (`claim_embeds_reopened`), and queues a head no earlier conversion queued (`claim_embeds_queued`).**
  - It touches only readable heads in its config's scopes.
  - A head refused on purpose stays as it is, for example text the request guard would not send. A muted head gets its vector work back like every other head, and automatic recall still leaves it out.
  - It asks for the heads its config's context can take, so heads of another project never fill a page ahead of them (review of 3.7.5).
  - On a copy of the shared store, the preview reopens 114 and queues 1. One head stays without a vector, refused as sensitive.
  - Review of 3.7.4 found the gap. 114 of the 116 heads without a vector had an obsolete embedding marked `authority_revoked`, one had never been queued, and one was refused as sensitive.

### Upgrading from 3.7.4

Install the package and run `plan-install` and `apply-install` where you upgrade. Then restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110). Then run once per store:

```bash
scope-recall retry-failures --config <the shared worker's or the instance's runtime-config.json> --limit 256 --apply
```

Run it again until `claim_embeds_reopened` and `claim_embeds_queued` are 0. The same command also re-opens the other failed work it always has, so those failures get one more model call each.

## [3.7.4] - 2026-10-06

3.7.4 indexes a withheld tool output's placeholder by the tool's own error text alone, and adds `unindex-withheld-outputs` to drop the rest of what earlier releases indexed. It also keeps the ledger whole when a claim is corrected while its embedding is being written.

### Fixes

- **A withheld tool output's placeholder is found by its error text alone (#206).** An earlier release's capture filter left a one-line summary in place of a tool output it withheld: "Tool execution summary (terminal): tool=terminal; output_chars=377; exit_code=0; output_preview=omitted".
  - All 212,773 such placeholders on the shared store, 68% of its 311,051 sources, came with imported history. No capture path writes one today.
  - Their words are the envelope's own, except the tool's error text that 4,348 of them carry ("...; error=...; output_preview=omitted").
  - Admission already kept them as sources only, never embedded or derived from. But earlier releases, the 1109 upgrade and both imports indexed the whole placeholder.
  - They held 2,015,161 of the store's 14,727,970 postings. The common-term ceiling is 10% of all sources, and the placeholders alone pushed twelve searchable terms over it: tool, summary, omitted, execution, output_preview, output_chars, terminal, exit_code, success, patch, true and status. ("0" too, but one-character terms are never searched.) The lexical channel drops such a term from a question that holds it, and 217 of the owner's 1,776 messages hold one as that channel reads them.
  - A placeholder is now indexed by its error text, when it carries one, and otherwise not at all (`core/events.indexed_terms`). This holds at capture, in the legacy conversion and in the shared import. Indexing one again drops what an older release gave it beyond that, and its projection status agrees. The 1109 upgrade still carries an older store's index forward; run the command below after it.
  - Measured on copies of the shared store taken at the same moment, before and after the command, with the same code. By words alone, the only channel this touches, recall is the same case by case on all 484 cases: the owner's 173 real questions, the older sets, and 36 real questions that hold one of those terms. Each of the 36 still finds what it found, through its other words.
  - So the change is to the index, not to recall: 1,945,720 fewer postings and 43 MB of the store's pages free for what it stores next.
- **`unindex-withheld-outputs` drops what is already there.**
  - It works a bounded page at a time (`--limit`, 500 by default, at most 5,000). Each page is its own write transaction and holds the store's writer lease while it runs. `--until-done` goes on page after page and pauses a moment between pages, so captures get the lease. Without `--until-done`, carry `next_after_id` into `--after-id`. Without `--apply` it only counts.
  - The sources stay, and so does the index of their error text.
  - On a copy of the shared store it took 426 pages, the slowest 0.12 s: 18 s of work, about 1.7 minutes with the pauses. Run again on the cleaned copy, it changed nothing in a few seconds. Every placeholder ended holding exactly its error text's terms, or none.
  - Review caught two faults in an unreleased first version, which ran on copies only. It scanned every tool row on each page while it held the lease, 1–3 s a page, and it dropped the error text with the rest.
  - In a shared store, run it with the shared worker's config, which reaches every scope.
- **An embedding finishing as its claim is corrected stays on the ledger (#205).** A supersede made the old revision's pending and leased work obsolete. An embedding whose vector had already landed could then never complete, so its point stayed in the store with no finished embed to account for it.
  - An embed that has held a lease now completes against its own revision, which stays readable, as one finished a moment earlier would have. That includes one sent back to wait after it wrote, when a dependency or its deadline moved. Other work, and an embed never leased, is still made obsolete.
  - Recall was never affected. It resolves every vector hit against SQLite, and a store keeps old revisions' vectors by design.
  - On the shared store no obsolete embed had left a point. The 264 points its finished embeds name that the vector table lacks all belong to deleted objects, whose purge removes every revision's vectors.

### Known limits

- Automatic recovery reopens a failed embed only when its subject is a source. A claim's failed embed is made obsolete instead, so 116 of the shared store's 12,746 readable claim heads have no vector and are found by their words alone. This predates 3.7.4 and is next.

### Upgrading from 3.7.3

Install the package and run `plan-install` and `apply-install` where you upgrade. Then restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110). Then, once per store:

```bash
scope-recall unindex-withheld-outputs --config <the shared worker's or the instance's runtime-config.json> --until-done --apply
```

## [3.7.3] - 2026-10-05

3.7.3 reads a Hermes message by its own words, as Hermes does. A notice stays the host's when a compression folded its summary or a to-do list into it. 3.7.3 also lets `retry-failures` clear a failure of any HTTP status.

### Fixes

- **A notice stays the host's after a compression.** 3.7.2 found the message that opened a turn by its text, in the run of user messages that ends the conversation `pre_llm_call` hands over. A compression at the turn's start changes that message in two ways:
  - It can fold its summary into it (`ContextCompressor._merge_summary_into_tail_row`). The message then holds more than the turn's text, and it need not be last. One of tianshu's three delegation results after the 3.7.2 upgrade was stored as the owner's that way.
  - It appends the open to-do list to the last user message Hermes counts as a real one, a delegation's result included (`_fold_todo_snapshot`). A background process's notice and a folded message get the list as a message of its own.
- **How the adapter reads a message now.** It reads each message's own words as Hermes reads them back. It drops an appended to-do list, and on a message Hermes marked as folded (`_compressed_summary`) the folded summary, in either of Hermes' two layouts. A message counts as the turn's own only when those words are exactly the turn's text.
  - The latest user message with words of its own decides. Past the last reply it must be a message Hermes folded and marked.
  - It is the host's when it carries a display kind other than steer, and other than hidden on a folded message (`split_user_originated_turn`).
  - The owner's own message carries no kind, so it stays the owner's.
  - A request Hermes restores after a notice decides in its place, and the notice stays the owner's. That is the safe side.
- **What review changed before release.** Three versions were never released:
  - The first took a folded notice that merely contained the turn's text. A summary quotes the person's messages word for word, so the person's words could be stored as the host's.
  - The second took any older folded notice with the same words. The person's later message, which Hermes had prefixed with a note of its own, could be stored as the host's. It also unwrapped an unmarked message that quoted Hermes' lines.
  - The third still looked past such a prefixed message in the run of user messages at the end, to an unanswered notice with the same words. 3.7.2 did the same with a plain notice, so its notes were wrong to say the owner's words are never stored as the host's: with a note before their message, they could be.
- **Every HTTP status failure can be cleared.** Any status a provider answered that is not named elsewhere is now operator-actionable, as `http_400` is. This covers a 4xx such as 404, 409, 413 or 422, and a 5xx the worker does not recover by itself such as 501 or 520–524. So are the HTTP worker's own refusals: `http_redirect` from a wrong base URL, `endpoint_invalid`, `request_limit` and `response_limit`. Once failed, nothing re-opens these by itself. An operator who fixed the cause can re-open them with `retry-failures`.
  - Before, these codes were in neither class. An `http_422` candidate evaluation on the shared store stayed failed, and no command could clear it, as with `http_protocol` before #201.

### Known limits

- The adapter reads a folded message with Hermes 0.21.5's own boundary lines. If Hermes changes them, a folded notice is stored as the owner's again. That is the safe side.
- When no message Hermes counts as the person's survives a compression, it puts one back (`_ensure_compressed_has_user_turn`): a copy of a delegation's result, or for a background process's notice the person's previous request. A copy merged into the to-do message loses its kind, and the person's request after a folded notice hides the notice: either way the notice is stored as the owner's. A copy put back on its own keeps its kind and is stored as the host's.

### Upgrading from 3.7.2

Install the package and run `plan-install` and `apply-install` where you upgrade, and restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110).

## [3.7.2] - 2026-10-05

3.7.2 lets a question reach the reply to what the person added before that reply came. It stores the messages Hermes writes into a conversation itself as the host's, not the owner's, and it lets `retry-failures` clear `http_protocol` failures.

### Fixes

- **What the person adds before the reply is part of the turn.** A recalled message offers the replies of the turn it opened as candidates (`turn_replies`), and a turn ended when the person spoke again. But people add to what they asked while the agent works, and the reply answers both. Of the owner's 1,242 messages from 2026-09-20 to 10-05, 141 had a further message of theirs before the first reply, and those turns offered nothing. Now such a further message, sent before the agent's first reply and within ten minutes of the turn's opening (`TURN_FOLLOWUP_SECONDS`), no longer ends the turn's offer.
  - Those replies are offered and ranked like any other candidate. They never lead an older copy of the current message to what it was told (`latest_turn`, where the last reply is raised above the rest). The person may have dropped the question for another request ("算了，先查值班表"), and then the reply answers that request.
- **A message Hermes writes itself is the host's.** Hermes opens a turn itself, with a user message it marks by a display kind, when a background process finishes, a delegation returns, a plugin speaks or a wake-up is due. The adapter stored that message as the owner's words, so it read as something the owner said ("[IMPORTANT: Background process … finished …]"). It is now stored as the host's (`host_generated`), following Hermes' own rule for what is human input: any display kind but `steer` is not.
  - The turn's own message is the last user message holding its text, in the run of user messages that ends the conversation `pre_llm_call` hands over. A message not found there stays the owner's, as before, so the owner's words are never stored as the host's.
  - `sync_turn` stores the opening message as the host's only when it is the very text that opened a notice turn. It names its turn by the one active when it runs, which can already be the next turn.
  - Reading a turn treats the host's message as before. It ends the question's turn, since the rows do not say which turn its job began in, and it opens a turn of its own.
- **`http_protocol` failures can be cleared** (#201). The transport failing mid-reply is now treated as `network_error` is:
  - consolidation and embedding work that fails with it is recovered automatically;
  - `retry-failures` re-opens any work that fails with it. That includes candidate evaluations, which are not retried automatically after a failure whose effect is unknown.
  - Before, `http_protocol` was in neither set. A model served over plain HTTP left failed rows that only a hand edit could clear.

### Known limits

- Notices stored before 3.7.2 keep the owner's name: 108 on the shared store (tianshu 35, tianji 35, yuheng 29, tianxuan 9). Their origin is not rewritten.
- Some messages Hermes writes carry no display kind (goal continuations, the CLI's and the TUI's heartbeat and loop prompts). They are still stored as the owner's.
- A notice whose text changed before the hook saw it is stored as the owner's. One example is a compression at the turn's start folding a to-do list into it.
- Changing the embedding model does not re-embed what was embedded before. `docs/configuration.md` said it did, and now says it does not; #200 proposes the re-embedding.

### Measured

On a copy of the shared store and its vectors taken at 2026-10-05 07:46, each entry asked with its own binding and audience, 3.7.1 against 3.7.2:

- The owner's 173 real questions asked again on the automatic path:
  - words only: 147 → 154 in the top five, 7 gained and none lost;
  - with vectors: 139 → 142, 3 gained and none lost.
- The older sets (facts, no-match, rephrased questions and the older QA set on the recall tool's path, tianshu and tianji), words only: one more passed (tianji's older QA set, 18 → 19 of 25), none lost.
- The automatic no-match set with vectors: identical case by case.
- Recalls that ran past their five seconds while the machine was busy were asked again alternately on both versions, and all of them were answered on both.
- Review's scenarios are pinned by tests, each answered as 3.7.1 answers it:
  - a question dropped for another request;
  - a job's report after the question's answer;
  - a host's record after the turn's window;
  - a late `sync_turn`.

### Upgrading from 3.7.1

Install the package and run `plan-install` and `apply-install` where you upgrade, and restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110).

## [3.7.1] - 2026-10-05

3.7.1 lets the MCP tools say why they refused a call, and recognises an older copy of the current message whatever its closing punctuation, as long as both ask or neither does.

### Fixes

- **MCP tools say why they refused a call.** mcp 2 shows the model only `Error executing tool <name>` for an exception other than its own `ToolError`, so a refused call gave no reason to correct: a change asked from a client that sends no conversation id (Claude Code, WorkBuddy, dsh), a scope the caller may not write, a malformed argument. A contract refusal now reaches the model as its code and the name of the field it refused (`ACCESS_DENIED: invalid codex_thread_id`); nothing read from the store is in it. `inspect` advertises its `limit` bound (1 to 24), as the Hermes tool already did, so a larger one is refused by the SDK's argument check, whose message names the bound.
- **An older copy whatever its closing marks.** The automatic recall sets an older copy of the current message aside, since the message already says it, and for a query of five search terms or more follows it to what it was told. A copy is now recognised whatever its closing punctuation and surrounding spaces, as long as both end asking (on a question mark or an asking particle) or neither does (`same_message`).
  - Compared character for character, "我家窗外有什么" and "我家窗外有什么？" were two messages; on 2026-10-04 the copy without the question mark took a slot of the owner's automatic packet as if it answered.
  - A statement asked back as a question ("我的航班改到周五早上八点了。", then "……八点了？") stays two messages, however long, so the person's statement is still found. Different words, a space between them, or a different letter case ("Release-2", "release-2") still make another message.
- Whether a message only asks, and where its closing marks begin, are read from its end once. A pattern anchored at the end tried a long run of spaces or marks again from each of its positions: 7.5 s for one message holding a run of 32,000 (measured in review). No message on the shared store holds a run of even 500 inside it.

### Known limits

- **A short question asked again is not given what it was told.** An older copy leads to what it was told only for a query of five search terms or more, so that a short command sent again does not bring back an old turn. "我家窗外有什么" holds four. On 2026-10-04, asked of dsh with vectors on, the automatic recall delivered six items: earlier copies of the question, a complaint about it and the investigation that followed. None said what is outside the window, and the answer ranked twelfth. dsh's own reply that evening restates the answer, and on the store as it stands that reply now comes first, in 3.7.0 and 3.7.1 alike.
  - A lower bar for questions was measured (one more of the owner's 173 questions answered, none lost) and withdrawn in review. Four-term status questions ("测试通过了吗？") would have put their last answer above newer messages that contradict it, and commands that end like questions ("按你说的做吗？") would have brought back old turns. Tests now pin both.

### Measured

On a copy of the shared store taken at 2026-10-04 21:39, each entry asking with its own binding and audience, 3.7.0 against 3.7.1:

- The owner's 173 real questions asked again on the automatic path: identical case by case (rank and item count), words only (147 in the top five) and with vectors (139).
- The older sets (facts, no-match, rephrased questions and the older QA set on the recall tool's path, tianshu and tianji): identical case by case, words only; the set holding the automatic no-match questions also with vectors.
- The window question through tianshu's entry, with vectors, in four spellings ("我家窗外有什么", with "？", with "?", with " ？"): 3.7.0 set aside only the copy spelled exactly like the query (none for " ？"), and the other copy took a slot; 3.7.1 sets aside both copies in every spelling.

### Upgrading from 3.7.0

Install the package and run `plan-install` and `apply-install` where you upgrade, and restart the clients' MCP servers and the Hermes gateways. The store's schema is unchanged (1110).

## [3.7.0] - 2026-10-04

3.7.0 lets DeepSeek Harness (dsh) join a shared store: its prompts are recalled before each turn and each turn's messages are stored, by a dsh plugin that runs the entry's hooks.

### Features

- **dsh as an entry of a shared store** (`attach --host dsh`, `plan-install` / `apply-install --host dsh`), the owner at this machine, measured with dsh 0.2.0-rc.2. dsh's own hooks name no turn and no reply, and its session log is compressed (multi-frame Zstandard), so no hook could record a turn there.
  - A native dsh plugin (`distribution/dsh/scope-recall/index.mjs`, an ES module run inside dsh, no dependencies) runs the entry's hook client (`hook_entry --host dsh`), the one the other clients' hooks run. It owns no memory policy.
  - Recall: before the first step of each turn (`agent/pre-step`) it runs the prompt hook with the person's message, the last of theirs that step takes. The prompt is stored as the owner's, named by the session and dsh's turn number, and what is remembered is added to the step as a message of its own (`source.kind: plugin:scope-recall`). The step waits up to 9 s (`recallTimeoutMs`), then goes on without it; cancelling the turn ends the hook. The plugin takes the answer as soon as it is whole, while the hook goes on to start the resident recall server.
  - Capture: it keeps each turn's messages as dsh commits them (`session/event`: the person's and the model's text; dsh's own context, ours, a subagent's session, tool calls and results, files and the model's reasoning are left out) in a spool, `<entry>\scope-recall\dsh-spool\`, one file per session and dsh process. When the turn ends it runs the `Stop` hook with them: the reply under the turn, what the model said while it worked and what the person sent meanwhile, read as a remote client's record lines (`transcript.dsh_lines`, at most 500 per hook). The hook answers how many lines it stored (`through`), and those leave the spool. A message sent twice is stored once.
  - A `Stop` stores what it can in its time (the hook reads at most 3 s of lines); the next takes the rest at once, and only one that stores nothing ends the store. A failed store keeps the messages: they are stored at the session's next turn end, or by a pass every minute over files idle for 2 minutes, one session at a time, a store that stores nothing ending the pass and the next waiting longer (up to 30 minutes). Only the process that wrote a file rewrites it; the file of a dsh that is gone (or idle for 6 hours) is taken whole by a rename, which one process alone wins. Bounded: a message's text up to 20,000 characters and 36 KB; a message waiting longer than 14 days, or past 5,000 of a session, is dropped and said.
  - A completed turn's reply goes with its `Stop` only when it was said at most a minute before: the store recognises the reply among the turn's messages by its words and a moment at most 120 s away, and the reply's moment is the hook's. A reply said earlier, and the last words of a turn that did not complete, are stored from the turn's messages alone.
  - `<entry>\scope-recall\dsh-plugin-status.json`: the last recall and store, what waits, what was dropped, the next pass, and a privacy alarm should dsh report a session log delivered to its API. A hook that fails is shown with the end of its stderr.
  - The MCP server runs under dsh's own MCP client (`@deepseek-ai/dsh-mcp-client`, stdio); its tools are `mcp__scope-recall__*`.
  - The prompt's recall comes from the entry's resident recall server (3.6.0), which outlives a dsh process; `resident_recall_minutes` is 120 by default for dsh.
- **The installer writes into dsh's home** (`--target-plugin-dir`, default `DSH_HOME`, else `~/.dsh`; `maintenance/install_dsh.py`).
  - The plugin file goes to `<dsh home>\scope-recall\dsh-plugin\index.mjs`, in the receipt.
  - Two rows go into dsh's home patch, `cordis.patch.yml`, as one `insert` between markers: `scope-recall` (the plugin) and `mcp-scope-recall` (the MCP server). dsh composes every profile with that file after the profile's own layers. Every other line is kept, the file is copied to the backups first, a second install changes nothing, and uninstall takes out only this entry's rows. A file that is not a YAML block list at column 0, rows of these names that something else inserts, another MCP server named `scope-recall`, or another entry's rows are refused, and nothing is written. A re-install writes the block where it stood, so that an operation of the person's after it (switching the plugin off, say) stays after it: dsh applies a patch's operations in order.
  - dsh uploads each session's log to its model API by default (`session-log-deepseek`), recalled memories with it. Unless the file already leaves it off, worked out as dsh does (the last `disabled` and the last `config` given for the row decide), the install adds `enabled: false` for it, after every other operation on it, between markers of their own. Uninstall leaves that in place.
  - Every other line is kept byte for byte (lines split at line feeds alone, an indented `[]` kept as the value it is). An install after an uninstall writes the block before an operation of the person's that names one of its rows.
- The host `dsh` is known wherever a client host is: `attach`, `hook_entry`, `mcp_entry`, `resident`, `doctor` and the install commands.

### Known limits

- Local only: the remote client (`remote-entries.md`) does not take dsh yet.
- dsh 0.2.0-rc.2 is a candidate; the plugin relies on its plugin interface and session format V4.
- dsh's feedback upload (`session-telemetry-otel`, a session sent when you send feedback on it) is not changed by the install; `DSH_TELEMETRY_DISABLED=1` switches it off.

### Upgrading from 3.6.2

Nothing changes for the hosts already attached: install the package and run `plan-install` and `apply-install` where you upgrade. Every process on a store must run 3.7.0 or later before a dsh entry attaches to it: an older one does not know the host and cannot replay a capture the entry queued. The store's schema is unchanged (1110).

## [3.6.2] - 2026-10-04

3.6.2 stops a WorkBuddy entry from storing an error notice as WorkBuddy's reply, and says how long its resident recall server really lives.

### Fixes

- When WorkBuddy's model cannot answer (not signed in, a model or network failure), WorkBuddy shows an error in place of the reply and hands it to the `Stop` hook as `last_assistant_message`. The hook stored it as the reply. Seen 2026-10-04 with WorkBuddy's own agent (2.147.0) not signed in: `Authentication required. Please use /login command to sign in to your account` was stored as the assistant's visible words.
  - WorkBuddy's session record marks such a message: `providerData.error` names the error, and the message's words are the error's.
  - The `Stop` hook now reads the record's last model message. When it carries an error whose words are the reply's (whitespace aside), nothing is stored for the reply (`client_error_reply`); the person's prompt is kept. The record read skips that message too.
  - The error's words are kept with the session's turns, so a later turn that is stopped and hands its `Stop` the error again stores nothing either.
  - A reply that broke off with an error (a stream timeout) keeps the words that were shown.
  - A WorkBuddy on another machine judges its reply from its own record and tells its entry's server (`error_reply`), which never opens a record for a request. A client or server of 3.6.1 leaves the field out or ignores it, and the reply is stored as before.
  - A record that cannot be found or read leaves the reply stored as before.

### Known limits, measured

- WorkBuddy's agent (2.147.0) puts itself and every process it starts in a Windows job that ends them all when the agent's process ends. The resident recall server cannot leave that job, so it lives only as long as the conversation's agent process that started it, not apart from WorkBuddy's processes as 3.6.0 said.
  - While one conversation's process runs, another conversation's first prompt finds it warm: answered in 1.48 s with its vector search, measured 2026-10-04 with WorkBuddy's own agent.
  - The first conversation's first prompt after WorkBuddy starts, and a prompt right after the conversation holding the server ended, are recalled without it, usually by words and the stored structure alone. A conversation whose MCP server still runs starts a new one within 30 s, and no sooner than a minute after the last start.
  - [docs/install.md](docs/install.md), section 12, now says so.

### Upgrading from 3.6.1

Quit WorkBuddy, install the package, run `plan-install` and `apply-install` for each host, and restart the clients and the Hermes gateways. The store's schema is unchanged (1110).

## [3.6.1] - 2026-10-04

3.6.1 stores the Hermes tool results that met a busy store, where some were lost.

### Fixes

- A Hermes tool result whose write met the shared store's writer busy past its 1 s was kept in memory, to be written again at the end of the turn. Some never were stored: 10 of tianji's on 2026-10-04, and 6 of tianxuan's, 6 of yuheng's and 2 of tianquan's in the days before. Each was logged once as `not stored (exception), kept to retry at the next turn` and is not in the store.
  - Hermes runs the end-of-turn hook only after a turn with a message and a reply. A turn it injected (a watch notification), one it interrupted and one without a reply wrote nothing again.
  - The retry at a turn's end had a capture's own 1 s for all of them: it wrote about one of up to 16 each time.
  - An idle agent evicted from Hermes' cache keeps its adapter without a shutdown, so nothing wrote them again until a gateway restart dropped them. A session started again in the same adapter cleared them too.
- Now:
  - A retry thread writes the kept tool results every 30 s while there are any, whatever the turns, in passes of up to 5 s, off any hook's time and off Hermes' memory worker. A turn's end still tries for 1 s.
  - Kept across a session switch, each is written in the session it was said in, under its own scope's grant as the installation's manifest gives it now, whatever audience the session that is current has.
  - A shutdown writes them once more, for up to 2 s.
  - One still failing after 30 minutes is given up.
  - Each is logged once when it is kept (`not stored (<reason>), kept to retry`) and once at its end, with its key:
    - `stored on retry`, or `queued on retry` (into the store's inbox);
    - `not stored (authorization revoked), dropped`, when its scope was taken away;
    - `not stored (still failing after 30 minutes), lost`, or `not stored (still failing at shutdown), lost`;
    - `not stored (<reason>)`, when the store refuses it for good.

    A capture still being written at shutdown is said so, and its end is said when it comes.

### Upgrading from 3.6.0

Install the package, run `plan-install` and `apply-install` for each host, and restart the Hermes gateways. A gateway still on 3.6.0 or earlier drops at its restart what it keeps to retry. The log line of a kept capture now ends `kept to retry`, not `kept to retry at the next turn`. The store's schema is unchanged (1110).

## [3.6.0] - 2026-10-04

3.6.0 recalls a WorkBuddy entry's prompts with the vector search, a new conversation's first prompt included.

### Features

- **A resident recall server** keeps the entry's vector search and embedding connection warm apart from the client's own processes (`adapters/codex/resident_entry.py`).
  - WorkBuddy 5.6.2 runs the entry's MCP server, and with it the recall server its hooks asked, only inside a conversation's agent process. A prompt that started one met a server still opening its vector store: a cold server answered with its vector search 12.7 s after its start (measured 2026-10-03), past the prompt hook's 6 s. All three prompts measured on 3.5.0 went without it.
  - The prompt hook starts the resident server after its answer when none runs, at most once a minute. The MCP server WorkBuddy runs with a conversation starts it too, and looks again every 30 s while that process runs. It is started through a process that ends at once, in a process group of its own, broken away from the client's job where Windows allows it, so ending a conversation's process tree does not end it. No task or service is registered with the system.
  - It names itself resident in the entry's endpoint folder, and a hook asks it before any other server.
  - It ends `resident_recall_minutes` after the last prompt's recall and the last look of a running MCP server: 120 for WorkBuddy by default, set in the entry's runtime config, 0 for none. It reads the value every 30 s. Claude Code and Codex keep none by default, since their server runs as long as the client.
  - It ends within 30 s once its package on disk is replaced or removed. A prompt hook that finds one of another version running (from another venv, say) stops it and starts its own where it can prove the process is that server and may end it, and `apply-install` stops the entry's resident. No hook or MCP server keeps one of another version up, so one that cannot be stopped ends at its idle end, and hooks of two versions against one entry switch it at most once a minute.
  - It ends once a recall has run 5 minutes past its time, since such a server answers every hook that it is busy; the next look starts a new one.
  - While it runs it holds a vector helper, about 1 GB. The MCP server WorkBuddy runs with each conversation then answers no hook and warms nothing.
  - One runs for each entry and client, held by a file lock; a second of the same version gives way to the first. It writes nothing to the store.
  - `scope-recall resident status|stop --home <entry> --host workbuddy` shows it or stops it; `stop` exits 1 when one still holds the lock. A process whose identity cannot be proven (no start time, as on macOS) is never stopped. Stop it before a `package-upgrade` of the entry's package, after quitting the client; `apply-install` and `apply-uninstall` stop it.
  - The first prompt after it ended, or after a reboot, starts it and is recalled the old way.
- **A recall server's start warms its query embedding as well as its vector store**, once, for every client, within 10 s. Warmed by the store alone, a cold server lost the vector search of its first two recalls to the embedding's time (`AuxiliaryModelError:timeout`).

### Upgrading from 3.5.1

Install the package, run `plan-install` and `apply-install` for each host, and restart the clients and the Hermes gateways. Quit WorkBuddy before its entry's upgrade. A WorkBuddy entry starts its resident recall server at its next conversation or prompt, with nothing to configure; `resident_recall_minutes` in the entry's runtime config changes its minutes, and 0 keeps none. From 3.6.0 on, stop a running resident with `scope-recall resident stop` before a `package-upgrade` of the entry's package; left running, it ends itself within 30 s of the upgrade. The store's schema is unchanged (1110).

## [3.5.1] - 2026-10-03

3.5.1 keeps every tool result of a Hermes step whose tools run in parallel.

### Fixes

- Hermes calls the tool hook for each of a step's parallel tool calls at once. Each capture held its session across its store write, which took 1.4-4.4 s on the shared store, and the hooks behind it waited. A hook waits for its session at most 10 s, so those past that were not taken, and their tool results were lost: 6 on yuheng and 2 on tianji on 2026-10-03, each logged as `post_tool_call not taken`.
  - A tool result is now written without holding its session, as a finished turn's captures already were. The step's other tool hooks no longer wait for it. Their writes still take turns at the store's single writer, each within its own budget.
  - A shutdown waits up to 10 s for a tool result being written. One still writing after that is counted in the shutdown state (`captures_still_writing`).
  - A capture is kept to retry only once its write fails for a reason that may pass, so a retry pass never writes again a tool result that is still being written.
  - A hook still cannot wait out a session held by a message's capture. That case is logged and counted as before.
- `--target-plugin-dir`'s help names `mcp.json`, the file the WorkBuddy installer writes, instead of `.mcp.json`.

### Upgrading from 3.5.0

Install the package, run `plan-install` and `apply-install` for each host, and restart the Hermes gateways. The store's schema is unchanged (1110).

## [3.5.0] - 2026-10-03

3.5.0 brings WorkBuddy into the shared store, and keeps the first recall after an idle stretch whole. Measured on this machine's Claude Code, on the prompts that came after 40 or more idle minutes:

- On 3.4.10 and 3.5.0rc1 (2026-10-01 15:55 to 2026-10-02 13:18, UTC-4), 7 of 18 such prompts lost their vector search or ran past their time in the store's own search: 4 lost the vector search, 5 ran past their time, and 2 did both.
- On 3.5.0rc3 and rc4 (2026-10-02 18:30 to 2026-10-03 05:30), none of 6 did either.
- Left out of the second count: a prompt that met a server still starting after a session restart, and one at 17:30, when a copy of the store had just read the whole file into the system's cache.

### Requirements

- WorkBuddy on Windows needs Git for Windows. WorkBuddy runs hook commands through Git Bash, and without it through PowerShell, which cannot run them.

### WorkBuddy

- WorkBuddy (the CodeBuddy team's desktop agent workbench) joins a shared store as an entry, the owner at this machine. Run `attach --host workbuddy`, then `apply-install --host workbuddy` with WorkBuddy quit, then approve the MCP server `scope-recall` once in WorkBuddy ([docs/install.md, section 12](https://github.com/410979729/scope-recall-hermes/blob/v3.5.0/docs/install.md#12-workbuddy)).
- Each prompt is stored, and what is remembered is put in front of it. Each reply is stored at `Stop`, together with the text shown between tool calls, read from WorkBuddy's session record. On WorkBuddy 5.6.2, the prompts and replies of three turns were stored as the entry's, and its recalls brought back what other entries had stored.
- `apply-install` adds three command hooks to `settings.json` in WorkBuddy's home: `UserPromptSubmit` waits 15 s, `Stop` and `SessionEnd` 10 s each. It adds the server `scope-recall` to `mcp.json` there.
  - It keeps every other key, hook and server, and copies each file to the entry's backups first.
  - It refuses beside another Scope Recall hook, beside a `scope-recall` server that is not this entry's, and on a file with comments.
  - `apply-uninstall` takes out only this entry's hooks and server.
- A WorkBuddy on another machine joins through the remote client (`"host": "workbuddy"` in `client.json`).
- Known limits:
  - The prompt that makes WorkBuddy start a conversation's agent process is recalled without the vector search, by its words and the stored structure only (`helper_lock_timeout`).
    - WorkBuddy 5.6.2 starts its MCP servers with that process: when a conversation opens, or when a prompt comes to a conversation that has none. That prompt's recall meets this entry's server still opening its vector store. All three prompts measured were such prompts.
    - WorkBuddy keeps the process between turns, and a prompt to a running process is answered by its server. One that had run 74 minutes without a turn answered a test recall, asked as the hook asks, with its vector search in 3.1 s.
    - A WorkBuddy on another machine is answered by its entry's server here, which runs on.
  - A multi-line message is stored as one line. Of the messages sent while a turn runs, the last is stored twice.
  - A subagent's work is not recorded. A session cron's or a goal's first prompt is stored as the owner's.
  - `doctor` does not read WorkBuddy's settings, and the `scope-recall-memory` skill is not installed into WorkBuddy.

### Recall after an idle stretch

- A server that answers its client's prompt recalls now searches its vector store once more after each 10 minutes without a recall that searched it. That covers the MCP servers of Codex, Claude Code and WorkBuddy entries of a shared store (WorkBuddy's while its conversation's process runs), and an entry's server for another machine. Left alone, the OS gave the index's pages to other work, and the first recall after an idle hour searched past its time and recalled by words alone.
- The store's operations read it through a memory map (`SQLiteStorage`). A hook recall read every page it touched with a read call of its own: 151,000 of them for a 3,800-character prompt. Through the map the pages come from the system's file cache, and a recall takes about 40 % less time, warm or cold. On a copy of the shared store, that prompt's recall took 0.73-0.76 s instead of 1.20-1.76 s warm, and 1.64-1.69 s instead of 2.68-3.11 s cold.
  - SQLite maps at most its build's limit, 2,147,418,112 bytes in Python's builds. Past it, the rest of the file is read as before, so the gain fades as a store grows beyond it.
  - An I/O error on a mapped page ends the process instead of failing the read. On Windows, a file another process maps cannot shrink: `VACUUM` leaves it at its size, and nothing here runs one.

### Fixes

- A transaction whose first statements failed left its connection open: reading the store's version, or switching a writer to WAL. A writable one kept the writer lease until its process ended, and every other process's writes failed. The connection is now closed before the failure returns.
- On Windows, a Hermes gateway keeps one vector helper for all the agents it makes.
  - Before, it attached a runtime, with a helper of about 1.15 GB, for every agent. Hermes did not always shut down the one it made before: yuheng's gateway held two on 2026-10-02.
  - The gateway's sessions take turns on that helper, as a server's prompts do.
  - No spare helper starts while a store the process shares holds a live helper.
  - A provider's shutdown no longer stops the helper; it ends with the gateway.

### Upgrading from 3.4.10

1. Stop the hosts, the Scope Recall worker and any remote entry's server, and take a `backup`.
2. Install the 3.5.0 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts and any remote entry's server again, and run `doctor`. A Claude Code or Codex server keeps the code it started with until its client restarts.
4. Attach a WorkBuddy entry only once every process on the store runs 3.5.0, the shared worker first. An older process does not know the host and cannot replay that entry's queued captures.
5. Before going back to 3.4.x or older, take WorkBuddy's hooks out with `apply-uninstall`, or a remote client's by hand. An older package does not know `--host workbuddy`, so every hook would fail on every turn. Each hook command ends in `|| exit 1`, so WorkBuddy reports the failure and lets the prompt through, but an older package cannot take the hooks out.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.5.0/CHANGELOG.md).

## [3.5.0 candidates] - 2026-10-02 to 2026-10-03

### Scope Recall 3.5.0rc4 - 2026-10-03

- The version moves past the `v3.5.0rc3` tag.
- A Hermes gateway keeps one vector helper, whatever number of agents it makes. Every runtime of the process searches one store of each table through one helper, as a server's runtimes already did (`vector.process_store.share`).
  - Before, the gateway attached a runtime, with a helper of its own (about 1.15 GB), for every agent it made. Hermes did not always shut down the one it made before.
  - On 2026-10-02, yuheng's gateway held two helpers after its agent was made again: one per registration of the provider, at 19:20 and 22:42. Tianji's held one for its one registration. The machine stood at 96 % of its commit limit.
  - The gateway's sessions take turns on the helper, as a server's do. A provider's shutdown no longer stops it: it serves the gateway's other runtimes and ends with the process.
- A process that shares its stores starts no spare helper while a store it shares holds a live helper. A gateway asks for one each time it binds an agent, and that spare would never have been taken (about 0.55 GB idle).
  - A store that has just taken the spare counts, before it asks for its table.
  - A helper that ended outside any request does not count, so the next bind starts a spare for the reopen.

### Scope Recall 3.5.0rc3 - 2026-10-02

- The version moves past the `v3.5.0rc2` tag.
- The connections the store's operations open read the store through a memory map (`SQLiteStorage`; maintenance and `doctor` open their own, unmapped). Each operation opens its own connection, and on a shared store another process writes between any two recalls, so SQLite's own page cache never carries over. A hook recall read every page it touched with a read call of its own: 151,000 of them, 586 MiB, for a 3,800-character prompt. Through the map the pages come straight from the system's file cache, and a recall takes about 40 % less time.
  - Measured on a copy of the shared store, two series of runs. That recall took 0.73-0.76 s instead of 1.20-1.76 s with the file cache warm, and 1.64-1.69 s instead of 2.68-3.11 s with it cold. A short prompt took 0.70-0.73 s instead of 1.07-1.24 s warm, and 1.56-1.58 s instead of 2.43-2.52 s cold.
  - The cold case is meant to model a server's first recall after an idle stretch. On this machine's Claude Code, 9 of 40 prompts after 40 to 90 idle minutes recalled past their time (`deadline_exceeded_collect`, `_hydrate`, `_relation`), with or without the vector search.
  - SQLite maps no more than the file holds and at most its build's limit: 2,147,418,112 bytes in Python's builds (SQLite 3.53.1). The shared store here is 2,031,501,312 bytes, so all of it is mapped. Past the limit a store is read as before, and new pages land at the end of the file, so the gain fades as a store grows beyond it. Writes are unchanged.
  - Two behaviours change. An I/O error on a mapped page ends the process instead of failing the read. A file that another process maps cannot shrink: a `VACUUM` leaves it at its size, as SQLite documents (nothing here runs one), and a tool that truncates the live file in place is refused.
- A transaction whose first statements failed (reading the store's version, switching a writer to WAL) left its connection open. A writable one kept the writer lease until its process ended, and every other process's writes failed. A busy store can answer those statements with "database is locked". The connection is now closed before the failure returns.

### Scope Recall 3.5.0rc2 - 2026-10-02

- The version moves past the `v3.5.0rc1` tag.
- A server that answers its client's prompt recalls searches its vector store once more after each 10 minutes without a recall that searched it. That is the MCP server of Codex, Claude Code and WorkBuddy, and an entry's server for another machine. The vector helper keeps the index in its memory, and a search touches the part its filter keeps. Left alone, the OS gave those pages to other work. The first recall after an idle hour then searched past its time and recalled by words alone.
  - This machine's Claude Code lost the vector search on 2 of the 4 prompts it had after an idle hour.
  - It lost it on none of the 5 it had while another process searched the same index every 10 minutes.
  - The search filters on every partition the entry's recalls search. Filtered on one, it held no rows for any entry of the shared store and touched 26 MB of the 306 MB a recall needs (copy of the store).
  - What the search finds is not looked at, and it writes nothing. A moment when a recall holds the server's handler is skipped. A search that failed is tried once more at once, and closing the server does not wait for one.

### Scope Recall 3.5.0rc1 - 2026-10-02

- WorkBuddy (the CodeBuddy team's desktop agent workbench) joins a shared store as an entry, the owner at this machine: `attach --host workbuddy`. It runs the hook client and MCP server that Claude Code and Codex run. Each prompt is stored and what is remembered is put in front of it. Each reply is stored at `Stop`, and so is the text shown between tool calls, read from WorkBuddy's session record. The hook payloads and the record's layout were checked against a live WorkBuddy 5.3.14.
- `apply-install --host workbuddy` adds three command hooks to `settings.json` in WorkBuddy's home: `UserPromptSubmit` (15 s), `Stop` and `SessionEnd` (10 s each). It adds the MCP server `scope-recall` to `mcp.json` there, the file of the user's own servers. WorkBuddy starts its agent with its connector proxy alone and never reads another server from `.mcp.json`. It starts a server from `mcp.json` once that server is approved in its MCP settings.
  - Every other key, hook and server stays, and each file is copied to the entry's backups before it changes.
  - Neither file enters the receipt. `apply-uninstall` takes out only this entry's hooks and server.
  - The install refuses beside another Scope Recall hook, beside a `scope-recall` server that is not this entry's, and on a file with comments.
  - See [docs/install.md](docs/install.md), section 12.
- WorkBuddy's hooks name no turn they share. A prompt opens one: under its `generation_id` when that id is new to the session, else under an id made from the session, the words and the moment. Its `Stop` closes that turn. The session record's messages are matched to the kept turns, so each message is stored once, although WorkBuddy hands the hook its words without their line breaks.
- In the record, a user message counts as the owner's only inside its `<user_query>` blocks. WorkBuddy's own user messages are not stored as the owner's words: command and shell output, a teammate's report, a slash command's expansion. Messages sent while a turn runs are merged into one, and the prompt hook gets only the last. The others are stored from the record when that turn ends, together with the last, which is so stored twice.
- A reply that repeats the session's last one is what WorkBuddy hands the `Stop` of a turn stopped before it said anything, and the hook does not store it. When the record shows the turn did say those words again, they are stored from the record.
- Prompts WorkBuddy sends on its own are not the owner's: a background task's notice, and a Stop hook's or a goal's request to go on (`Stop hook feedback:`). A session cron's or a goal's first prompt cannot be told from the owner's, and is stored as theirs.
- WorkBuddy pastes a prompt hook's raw output into the prompt when that output carries no `additionalContext`. For this host, a hook with nothing to recall prints nothing, even when its remote client cannot load its configuration. The other hosts still get `{}`.
- WorkBuddy blocks a prompt whose hook exits 2, which is what argparse exits with when an older package does not know `--host workbuddy`. Every WorkBuddy hook command ends in `|| exit 1`, so a failing hook is reported and the prompt goes through. Take the hooks out with `apply-uninstall` before rolling back below this release.
- A WorkBuddy on another machine joins through the remote client (`"host": "workbuddy"` in `client.json`). Its `install` merges into WorkBuddy's own two files the same way ([docs/remote-entries.md](docs/remote-entries.md)).
- Upgrade order: every process on the store must run 3.5.0rc1 before a WorkBuddy entry attaches, the shared worker first. An older process does not know the host and cannot replay that entry's queued captures.
- Known limits: a multi-line message is stored as one line, a subagent's work is not recorded, and `doctor` does not read WorkBuddy's settings.

## [3.4.10] - 2026-10-01

3.4.10 fixes three faults reported on GitHub, all on Hermes. A session's hooks could wait out Hermes' hook timeout and then be skipped for every session (#169). The sessions of a dashboard login were never stored or recalled, and nothing said so (#175). On a host that hands its packages over on `PYTHONPATH`, the LanceDB helper could not start, so the vector search was dead (#176). Our own five gateways were not exposed to #175 or #176, and met #169 rarely.

### Hermes hooks (#169)

- Each Hermes session has its own lock, and a hook waited for it without a bound. The lock was held through a prefetch's recall, which Hermes stops waiting for after 8 s and lets run on, and through the writing of the whole previous turn. A hook waited out past Hermes' 30 s hook timeout was abandoned. Scope Recall registers one callback per hook for the whole gateway, so Hermes 0.21.5 then skipped that hook for every session for a minute. Our five gateways logged 18 hook timeouts and 19 skips from 2026-09-20 to 2026-10-01; the reporter, with 7 to 15 sessions at once, 247 skips in ten days.
- A hook now waits for its own session at most a third of the host's timeout, 10 s at most. One it cannot wait for in that time returns, and is counted and logged with what holds the session.
- The prefetch reads the turn's state under a wait of at most 2 s and recalls without the lock. The end of a turn is written one capture at a time with the lock released, so the next turn can start meanwhile. Its message and reply are dated when its writing began, and a shutdown waits until it is written.
- The callbacks are named `scope_recall_<hook>`, so Hermes' own timeout and skip lines name them. A hook that still outlives the host's timeout logs a warning. Both counts show in the `status` tool. The bounds and the log lines are in [docs/configuration.md](docs/configuration.md).
- A skipped `pre_llm_call` leaves its turn id for the turn's start, so the turn's interim messages are still matched to it. With Hermes' hook timeout set to 0 or less, which Hermes reads as none, a hook waits at most 10 s and nothing reports a skip.
- Not done: a hook that cannot wait is not kept to be written later. A skipped tool hook loses that tool's result and a skipped `api_request_error` its failure mark; the turn's message and reply are still stored when it ends.

### Dashboard logins (#175)

- Hermes passes a desktop or tui session's dashboard login (`basic:<name>`) as its user and names no chat. No owner grant could match that route, and no installer option approved a login. Every session of someone working through the desktop client with basic auth stored and recalled nothing. Nothing said so: Hermes reads none of the adapter's gaps, and `doctor` stayed healthy.
- On desktop or tui, a login whose host names no chat is now a one-to-one chat with that login (`private`, the login, thread `main`), the way an owner grant is written. An unapproved login gets no owner grant: only what an audience row on its route gives it, like any gateway user. The session that names no user, the CLI and the platforms that name their chats are unchanged.
- `apply-install --owner-login <platform>=<login>` approves a login as the owner's own, on desktop or tui. It is Hermes only, and refused on a shared store entry, like `--local-platform`. See [docs/install.md](docs/install.md), which also warns that a dashboard served to other machines runs its Chat tab as a session that names no user.
- A desktop or tui session that binds no memory scope logs one warning, with its platform, its login, its gaps and what to do, never what was said. A gateway chat left unmapped stays silent, as before.
- `doctor` reports an owner grant whose user is no owner principal: `audience_owner_unverified`, `attention`, counted by platform.
- Upgrade note: a hand-written audience row for a desktop or tui login with an empty chat no longer matches, and such a session now logs the warning. Rewrite the row's chat to `private`, the login and thread `main`. Approve the login with `--owner-login` only if it is the owner's own.

### LanceDB helper (#176)

- The helper runs isolated (`-I`), which keeps `PYTHONPATH` off its path. Hermes Desktop's package manager starts a bundled Python and hands its packages over on `PYTHONPATH`. There the helper died at its first import, before it answered. Every embed and every vector search failed as `worker_failed`, recall fell back to words, and only `doctor`'s `vector_unavailable` said anything.
- The host now hands the helper the directories it imports `jsonschema`, LanceDB, PyArrow and numpy from, and nothing else of its path. The helper stays isolated.
- When the worker cannot open the vector store because its helper ended before answering, it runs the start-up once more. That run is sent no request, so it holds no memory text. Its last error line, paths removed, goes into the `worker_error` that `doctor` shows. The worker's gap now names the fault: `vector_unavailable:RuntimeError:worker_failed`.

### Upgrading from 3.4.9

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.10 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.10/CHANGELOG.md).

## [3.4.9] - 2026-10-01

3.4.9 lets prompts that come together on an entry's server search by meaning: every handler of the server shares one LanceDB helper. In every process, a vector store left with no table is opened again instead of failing every search until the process ends.

### Recall

- An entry's server answers a prompt with the handler it keeps. A prompt that comes while that handler is busy gets a handler made for it, and that handler's vector store started a LanceDB helper of its own. The new helper spent about 2 s importing LanceDB and then opened the table while the prompt's words were searched. This applies to the work computer's remote server and to the MCP server of Claude Code and Codex on this machine.
- On the work computer, parallel sub-agents opened Codex sessions two and three a second. Their prompts lost the vector search that way, and some lost their whole recall.
- Every handler of a server now searches one store through one helper, warm from the server's start. The prompts take turns on it, one search each. A kept handler made anew, after its configuration changed, finds the helper warm as well.
- A store kept across requests could be left looking open with no table: when it was opened again after a fault and the helper could not open the table, or the open ran out of time before a helper started. Every later search then said the table was not open, until the process ended. This held on 3.4.8 for the handler a server keeps and for a Hermes gateway's runtime, and with the shared store it would have held for every prompt of a server. Such a store is now closed, and a later request opens it again: the next one, or the one after when the helper's failure came in late. On a sharing server, a prompt's recall reports a table that is not made yet at once, as a store of its own does, without starting a helper.
- The prompts of a server now share their helper's faults as well: a helper that stops answering costs each of them its vector search until it is replaced, after 60 s.
- A server no longer keeps a spare helper beside the one its prompts share: about 0.55 GB less committed memory for each server, as measured on the running servers of 3.4.8. A helper started after a fault therefore imports LanceDB anew, about 2 s.
- Measured on a copy of the shared store, with bursts of three prompts with long briefs, each stored and then recalled within the hook's 6 s as the server does it: 45 of 48 recalls kept their vector search, against 29 of 48 on 3.4.8. None lost it to a helper's start, against 16; the other three lost it to the time the embedding provider or the search took. Over five such rounds on 3.4.8, 2 recalls of 120 were lost whole; none of the 48 on 3.4.9 was. A prompt that comes alone was recalled with its vector search 16 times of 16 either way.

### Upgrading from 3.4.8

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.9 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`. Restart a remote entry's server so that it serves with the shared helper.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.9/CHANGELOG.md).

## [3.4.8] - 2026-09-30

3.4.8 answers a question about what was said or done on a day, or by one entry of the shared store on a day, from that day's conversation. Any other message is recalled as on 3.4.7.

### Recall

- A message that names a day ("9月29日", "2026-09-29", "昨天", "昨晚"; one to three days) and otherwise only asks or requests what was said or done then ("聊了什么", "帮我看看做了哪些工作", "总结一下", "有什么进展", "what did we do yesterday") is answered from that day: the messages sent to the agents and the replies they showed, in the asker's audience, spread across the whole day, several named days taking turns. A short message that only acknowledges ("继续", "好的，继续吧", "按你说的做") is left out. When the message also names an entry of the shared store ("9月29日工作机 Claude Code 聊了什么"), only that entry's messages. Searched by its words alone, the date and the entry's name matched nothing useful: on a copy of the shared store, of 106 such questions (every entry and day of 09-16 to 09-29 with at least three messages of the owner, asked two ways), 4 found a message of the named entry from the named day. Now 106 do when asked from Claude Code and 104 when asked from yuheng (the rest are outside yuheng's audience), and 99% and 97% of what is delivered is from that entry and day. In seven more wordings of the same 53 entry-days, 371 of 371 and 364 of 371 do, against 16 on 3.4.7. The day questions were measured by words alone.
- Any other message that names a day is recalled exactly as if it named none: a question about a subject ("9月2日发布的 3.4.2 修了什么", "继续昨天的任务"), which lost the answer said on another day, the current task or the claim that answered it; a question about what to do ("今天做什么") or where the work stopped ("昨天聊到哪了"), which the current task answers; a message that only mentions a day ("今天在吗"); a range of days ("9月28日到30日"), more than three, a day still to come, a placeholder date such as 9999-12-31; a message of more than 512 characters. Every recall of the owner's real questions and of two agents' older question sets answers exactly as on 3.4.7 by words alone; none of the 428 is read as a day question, so with vectors on they are answered as on 3.4.7 too. A time of day is not used: "昨天下午3点聊了什么" is answered from the whole day.
- A day is the calendar day in the zone the host tells its model, the zone the recalled times are shown in: Hermes's `timezone` setting (else the machine's); for Codex and Claude Code, the machine's, with that day's daylight-saving offset; for a remote entry, the serving machine's.

### Upgrading from 3.4.7

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.8 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.8/CHANGELOG.md).

## [3.4.7] - 2026-09-30

3.4.7 puts what a question was told the last time it was asked above the best candidate of that time in its automatic recall, and gives the vector threshold a shared store needs in the shipped embedding space. Nothing else changes.

### Recall

- A question asked again word for word leads to the replies its earlier copies received (3.4.4), and they came at a first rank's fixed score, below every candidate two channels agree on, as most are once vectors are on. The automatic recall now raises at most half the packet of the last copy's turn above the best candidate said up to that turn, the turn's other replies included: the replies a search channel ranked highest, then the turn's last reply when the turn was read to its end. Only the last copy's turn, so an answer that changed since is not put beside the one that replaced it. No more than that: an agent's turn opens with what it is about to do and names the subject as it works, and a turn cut by the window that reads it has not reached its answer. What was said after that turn keeps its place above them only when it ranks higher still: with vectors on, a newer statement both channels find stays above them when nothing said before the turn was found by both; by words alone an answer that changed usually comes before what replaced it. Recall in the other modes is unchanged.
- On a copy of the shared store, the owner's 173 real questions asked again get their answer in the top five for 146 instead of 124 by words alone, and for 141 instead of 99 with vectors on at 0.70. By words alone 24 are gained and 2 lost: in both, the reply the benchmark counts as the answer, the first long reply of the turn, stands below the raised replies of the same turn, once sixth instead of fifth and once left out of the packet. With vectors on, over all 428 questions measured, 42 are gained and none is lost. Facts, rephrased questions, questions with no answer and two agents' older question sets are answered as before either way.

### Configuration

- `vector_threshold` on a shared store in the shipped embedding space: 0.70, where the configs hold the accepted 0.653, set by hand. Measured on 3.4.5 on a copy of the shared store of nine entries: at 0.653, two agents' sets of 20 questions that have no answer were each given an unrelated memory for 12 of them, and from 0.68 for 2, as by words alone. Over the 428 questions, 0.70 answers 23 more than 0.653 and one fewer, and 0.72 loses a fact an agent had been told. A store in another space, or with a threshold calibrated on it, keeps its own. See `docs/configuration.md`.

### Upgrading from 3.4.6

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.7 package, then run `plan-install` and `apply-install` for each host.
3. On a shared store in the shipped embedding space whose runtime configs hold `vector_threshold` 0.653, set 0.70 in each entry's runtime config.
4. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.7/CHANGELOG.md).

## [3.4.6] - 2026-09-30

3.4.6 stops a message being lost when another message under the same key still waits in the capture inbox, as Codex's messages sent into a running turn were, and keeps such a message when the other one is deleted. Nothing else changes.

### Capture

- A message is no longer refused because another message under its key still waits in the capture inbox; this holds for every host. Codex gives a message sent into a running turn the id of the turn it joins, so it comes under the key of the turn's first message: it waits in the capture inbox, and the worker's next pass stores it under a key made from its words, about 45 s later. The inbox knew a capture by its key alone, so a third message sent into the turn before that pass found the second's place, was refused as a changed copy of it (`VERSION_CONFLICT`) and was lost. Comparing the Codex session records of both computers attached to the shared store with the store itself since 2026-09-28, six prompts were missing, each sent 3-5 s after another into the same turn (five the owner wrote, one the context of Codex's in-app browser), and no other. A capture's place in the inbox now depends on its words as well. A hook sent again with the same words, as a remote client does when the store was too busy to take it, still finds its own place and stores the message once; the same words sent twice into one turn before the next pass are kept once. Messages lost before 3.4.6 are not recovered; Codex's own session record still holds them.
- A delete cancels a waiting capture that holds the deleted message (its words, one of its parts, a later version of it, or a part of it sent without its first), and keeps another message waiting under the deleted message's key, which is then stored under a key of its own, as it is when it comes after the delete. Every waiting capture of the deleted message's source group was cancelled, but one already being given a new key: a message still waiting under a Codex turn's key was lost with another message of the turn that was deleted.

### Upgrading from 3.4.5

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.6 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.6/CHANGELOG.md).

## [3.4.5] - 2026-09-30

3.4.5 keeps a slow statement from holding up a recall, lets a recall's diagnostic ref be read, and stops `doctor` calling a busy worker failed. Nothing else changes.

### Recall

- A candidate statement still running at the recall's deadline is interrupted: that channel gives nothing, the gap says `deadline_exceeded_collect`, and what the channels before it found still answers. A statement cannot see the deadline itself: before 3.4.3 the word search ran 18-21 s for a long Telegram message and the recall came back empty long after its deadline, the Hermes turn waiting for it. The deadline is looked at once every million steps of a statement, 10-60 ms apart: each look takes Python's lock back from the statement, and looked at a hundred times as often, a 25 ms statement took 1.4 s beside a busy thread. On a copy of the shared store every recall of the owner's real questions and of two agents' older question sets answers exactly as on 3.4.4, and its median time is unchanged; that was measured by words alone, the embedding provider's project being over its monthly spending cap.
- `inspect` reads a recall packet's `diagnostic_ref` for the session that recalled: that recall's counts and gap codes, kept by the process that ran it for its last 64 recalls. It answered `SOURCE_MISSING` for every one; it still does for another session's ref, or for one that process no longer holds, such as one from a prompt hook. A remote entry's MCP tools share one server session, so there any of the entry's conversations reads the others' refs; each holds only counts and gap codes.

### Maintenance

- `doctor` no longer reports a worker pass that yielded as `worker_last_exit_failed`: exit 75 with status busy means another writer held the store, or another pass the worker lock, and the supervisor tries again after a pause. Any other non-zero exit still is a failure.

### Upgrading from 3.4.4

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.5 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.5/CHANGELOG.md).

## [3.4.4] - 2026-09-30

3.4.4 gives a question asked again what it was told before, stops Hermes storing a turn's message twice, and keeps one recall channel off the whole work queue. Nothing else changes.

### Recall

- A question asked again word for word is given what it was told before. The automatic recall leaves out an older copy of the current message, since the message already says it, and that copy was the only way to the answer it had received whenever the answer shares no word with the question. It now still leads to its turn's replies and is never delivered itself. A short message does not: one of fewer than five search terms, about six Chinese characters, such as "继续执行" or "按你说的做", brings back no old turn. On a copy of the shared store, over the owner's real questions of the last two weeks asked again on every agent, the answer is in the top five for 124 of 173 instead of 59, and none that was answered before is lost; facts, rephrased questions, questions with no answer and two agents' older question sets are answered exactly as before. All of it was measured by words alone: the embedding provider's project was over its monthly spending cap.
- A turn's replies no longer stop at the same message stored again (see below): of the 125 Hermes turns of 2026-09-16 to 09-29 that were stored that way, 100 lead to their answers again; most of the rest were answered more than 30 minutes later, past the window a turn is read in.
- The channel that offers the session's messages still waiting to be consolidated reads the pending queue, not every consolidation ever made, which the queue keeps: 6-12 ms of each recall on the shared store, growing with every consolidation, is now 0.1 ms, and with one scope it no longer reads every event of that scope.

### Hermes capture

- A session's hooks go to the memory provider that bound it last. When Hermes rebuilds an agent it had evicted, the new provider binds the same session while the old one stays registered, and the hooks could go to the old one: the turn's message was stored through it, and again, with the reply, through the new one. It happened on the first turn after each rebuild, 6 of 48 turns on two agents since 2026-09-28. Messages stored twice before 3.4.4 keep their second copy.

### Upgrading from 3.4.3

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.4 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.4/CHANGELOG.md).

## [3.4.3] - 2026-09-30

3.4.3 keeps a long message's recall from stalling. For a long enough message the word search read every event of the conversation's audience one by one: on a copy of the shared store the word search for a Telegram message of 72 characters took 18-21 s, and its recall came back empty at every stage's deadline. In a sample of the owner's messages since 2026-09-16, 39 of the 92 over 80 characters on the five Hermes instances were planned that way. Nothing else changes.

### Recall

- The word search starts from the message's search terms, never from its audience's scopes. A store keeps no statistics for SQLite's planner, which weighed the terms against the scopes by rule of thumb and, past about thirty terms with the five scopes of a Telegram conversation, started from the scopes instead. On the copy all 655 real prompts sampled now start from their terms, and the word search of the two questions that had stalled takes 0.3 s instead of 18-20 s and finds the same memories. The owner's questions of the last two weeks, asked again on each agent, lose no answer and gain those two. Two agents' older question sets are answered exactly as before by words alone, and with vectors for facts and questions with no answer; the vector runs of the other two sets were cut short by the embedding provider's spending cap.

### Upgrading from 3.4.2

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.3 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.3/CHANGELOG.md).

## [3.4.2] - 2026-09-29

3.4.2 keeps a long prompt's recall within its time. The word search of a prompt looked up every word it held: a 2,000-character prompt's 80 words held 273,000 index entries on the shared store and took 9 s, longer than the prompt's whole recall, which then went without its search by meaning as well (`deadline_exceeded_collect`). On 2026-09-29 Codex on another computer, whose prompts are often that long, was recalled for by words alone that way on 7 of about 16 prompts. Nothing else changes for a question of up to 16 search terms.

### Recall

- The word search looks for the prompt's rarest search terms: all of them for a question of up to 16, and for a longer prompt as many more as 20,000 index entries allow. The rarest terms are the ones that tell memories apart, and a term the question needs as an identifier is always searched. A memory it finds is still weighed against every term of the prompt, the ones left out of the search included. On a copy of the shared store a long prompt's word search took 0.7-1.5 s instead of 4-9 s. On two agents' question sets nothing changed: the questions the owner asked (27 of 30 and 18 of 25), facts, rephrased questions and questions with no answer were answered exactly as before.

### Upgrading from 3.4.1

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.2 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.2/CHANGELOG.md).

## [3.4.1] - 2026-09-29

3.4.1 makes automatic recall search by meaning on the prompts where it fell back to words alone. On 2026-09-29, 4 of 9 prompts from Claude Code on another computer were recalled by words alone, and so was the first prompt of a Claude Code session at home and a prompt after a pause; each recall said so in its gaps. Nothing else changes.

### Recall

- The prompt's embedding is asked for as soon as its recall starts, beside the word searches, instead of after them. Behind them it had about 2 s of a prompt's 4 s: too little when a pause of more than 30 s had closed the connection to the provider and a new one had to be made (`vector_error:AuxiliaryModelError:timeout`).
- The recall handler that the MCP server of Claude Code and Codex, and a remote entry's server, keep between prompts is made, and its vector table opened and searched once, when the server starts. Made at the first prompt, it spent that prompt's time opening the table, and the first prompt of every Claude Code session was recalled by words alone (`helper_request_deadline`).
- The vector helper's answer to a search that ran out of time is taken by the next search, however much later that comes. After a minute it was taken for a hung helper, which was closed, and the next prompt started cold again (`worker_unresponsive`). An embedding that fails while the table opens no longer closes the helper, nor waits for the table to open.
- A vector search left no time by its embedding says so (`helper_request_deadline`); it answered nothing, as if it had searched and found nothing.

### Upgrading from 3.4.0

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.1 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110). Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.1/CHANGELOG.md).

## [3.4.0] - 2026-09-29

3.4.0 lets Claude Code and Codex on another computer use the shared store, and makes capture, automatic recall and deletion hold up on a large, busy store. A client on another computer is an entry of the store under a name of its own: its hooks forward each event to a server on the store's machine over a private network, and its MCP tools are served from there. How to set one up: [docs/remote-entries.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.0/docs/remote-entries.md).

### Requirements

- Python 3.11 to 3.14, as for 3.3.0.
- Claude Code 2.1.196 or later.
- A remote entry needs a network between the two computers that nobody else can reach, such as a tailnet. Its server listens only on the one address you give it, never on every interface, and refuses any request without the entry's token.

### Recall

- Claude Code's and Codex's prompts are recalled by the entry's MCP server, which the client keeps running, with its vector table open and its embedding connection kept between prompts; a remote entry's server does the same. A hook is a process of its own and paid for opening LanceDB and a new embedding worker on every prompt: on a store of 280,000 sources a recall took about 4 s and often went without its vector search. Kept, a recall takes 1.7-2.8 s with it.
- The worker builds a nearest-neighbour index (IVF over 8-bit quantized vectors) once a store holds 10,000 vectors. A search probes every partition and re-ranks by exact distance, so the index makes each comparison cheaper and never decides which rows are compared.
- A turn is recalled even when its message could not be stored, a prompt longer than a query is recalled for by its first 8,192 characters or 128 search terms, and a recall that fails or runs without its vector search says why.

### Capture

- The worker no longer holds the writer lease while it matches sources against candidates; on the pilot it had held it most of the time, and every hook that waited for it lost its capture. A prompt waits up to 2 s for the lease.
- Hermes records more of each turn: what you send while a turn runs, and what the assistant shows between tool calls. A capture that timed out on a busy store is written again at the next turn.
- A capture that meets a busy store on another computer is kept by its client and sent again.
- Codex's own requests for suggestions, and the rest of the thread they open, are not stored as your words.
- The embedding bound counts every character as one token (#151): digit-dense text such as logs, IDs and hashes was refused by some providers and never embedded. An import's history gets the embeddings its source store never queued.

### Deletion

- A delete erases the deleted text, its claims and its vectors on disk. The worker misread a purge's operation id, so reads were blocked but the content stayed.
- A later capture in a thread whose episode was deleted starts a new episode instead of failing for good. A capture waiting in the inbox is cancelled only when it holds a deleted message. Under a deleted message's key, a later version of it, a part of it and a copy of it are refused and leave the inbox, while another message under the key (a restarted Hermes gateway numbers its turns from 1 again) is stored under a key of its own. A copy of a suppressed message that took another key is stored suppressed.

### Also in this release

- Candidate evaluations keep their verdict when the model miscopies a supplied source, and a candidate is woken only by evidence that can pose it a new question.
- An agent's own edit of one of its Scope Recall skills stays across an upgrade that leaves the skill as it was.
- The secret screen lets through what follows a credential word without being a credential (code, placeholders, a word that describes the value), and its scans stay linear on adversarial text.
- `plan-install`, `apply-install` and `doctor` pass `--python` on as given (#141). The vector helper finds a venv's packages when the host starts the base interpreter (#139).
- The release tier no longer asks for the P18 formal evaluation receipt, which no release could provide.

### Upgrading from 3.3.x

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.4.0 package, then run `plan-install` and `apply-install` for each host.
3. Approve the plugin's changed hooks in Codex (their timeouts changed in 3.4.0). Until you do, Codex skips a changed hook without saying so.
4. Start the hosts again and run `doctor`.
5. `scope-recall retry-failures --config <runtime-config.json> --include-terminal --apply` gives one more look to embeddings that failed with `http_400` on digit-dense text and to evaluations that failed on a miscopied source.

The store's schema is unchanged (1110), so a 3.3.x process can still open it. Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.4.0/CHANGELOG.md).

## [3.4.0 candidates] - 2026-09-26 to 2026-09-29

### Scope Recall 3.4.0rc13 - 2026-09-29

- The version moves past the `v3.4.0rc12` tag.
- A delete cancels only the captures waiting in the capture inbox that hold a deleted message, by the comparison that already kept a row put off or a key collision (3.4.0rc10); every other capture waiting in the same scope is stored. A capture waiting for the next pass was cancelled with the whole scope, whichever client had sent it, so a message that arrived while someone deleted another in the same conversation was lost. The delete now reads each waiting capture of its scope while it holds the writer lease: about 5 s for an inbox at its 64 MB cap, where it had cancelled them all unread.
- Another message under the key of a deleted message is stored under a key of its own, as a key collision is. It was refused for good: a restarted Hermes gateway numbers its turns from 1 again, and a delete removes its own command's key, so the next message at that turn was lost. What the deletion contract keeps out stays out: a later version of the deleted message, a part of it sent without its first, and a copy of it under its key. A copy is decided on the whole message: a part with a deleted part's words; while the deleted words are kept, all of them held or a near copy, as a delete compares waiting captures; after the purge, the same words spaced, cased or punctuated otherwise, whose digests the purge now keeps. After the purge, a copy with other words added is stored as another message. A purge replaces a long message's group key once, where it hashed it once for each part, reads each version's words once, and when run again (a restore runs it) keeps what the first run kept. A delete purged before this version kept no such digests: whatever comes under its key is still refused, as before.
- That copy now leaves the inbox. It stayed there with its code, and doctor reported `capture_ingress_blocked`, and the patrol its line, until someone removed it by hand. The hook is told it was cancelled; a pass counts it among the rows it cancelled (`ingress_cancelled`); a client on another machine does not send it again; and a Claude Code Stop that meets it in the session record reads on, where every later Stop had stopped at that line. `retry-failures --apply` returns a capture an earlier release refused this way (`ACCESS_DENIED`, counted as `inbox_refused`) to the replay.
- A copy of a suppressed or deleted message that took another message's key is stored suppressed. Stored under a new key, a group of its own, it had come back to automatic recall (3.4.0rc10). It is a copy when a part of it has the words of a suppressed part in the same scope, project and branch, or when it holds the words of the message whose key it took, compared as a delete compares them; a long message is then suppressed whole. A message whose words match no suppressed one is stored as before.
- A hook whose capture or recall meets SQLite's own "database is locked" fails that step alone. The error escaped the hook: the work computer's server answered 500 and the prompt got no recall at all, as fifteen of its hooks did within one second on 2026-09-28. The capture is now kept to send again, the recall is answered as failed (`recall_exception`, `OperationalError`), and a store broken otherwise is named by its class in the server's log.
- The work computer's server logs where a hook's time went: making its handler, the capture, attaching the handler's own runtime and closing it (`4063 ms (build 120 ms, capture 850 ms, attach 700 ms, close 40 ms)`); the rest is the recall. On 3.4.0rc12 its prompts took about 4.1-4.4 s end to end, against 1.7-2.8 s for the kept recall alone.

### Scope Recall 3.4.0rc12 - 2026-09-29

- The version moves past the `v3.4.0rc11` tag.
- The entry's MCP server on this machine, and the server a work computer's hooks reach, keep the handler they recall with from one prompt to the next, and with it the LanceDB table open and the embedding worker connected. Each made a handler for every prompt, which opened the table (about 2.3 s) and started the embedding worker and its connection (about 1 s): on the pilot a warm server's recall took 3.9-4.1 s, two of five lost their vector search to the time, and on 2026-09-29 a third of the work computer's prompts were recalled by words alone. Kept, recalls one after another took 1.6-2.1 s with their vector search; one after a pause of more than 30 s also opens a new connection to the provider first (below). One recall uses the kept handler at a time; another that comes meanwhile is recalled by a handler of its own, as before. It is made anew when the files it was made from change (the runtime config, the entry's pointer to the store and the store's record of its grants, and on this machine the env file), after a recall that raised, and while its runtime is not attached from a config it could read; the handler it replaces is closed after the recall, not within its time. It is closed when the server stops. The embedding worker it keeps no longer sends a request on a connection idle for more than 30 s or one its server closed: the next prompt after a pause failed on it at once and went without its vector search (review of rc12), which a Hermes gateway's worker, kept as long, met as well. An embedding call that fails on its connection or its worker (`network_error`, `http_protocol`, `transport_*`) is now the server's own fault, so the hook recalls as well. On the work computer's server the prompt is still stored by its request's own handler, which recalls as well only while the kept one has not answered in time, as a hook here does with the MCP server; the server's log line says how the kept recall went (`warm recall answered`).
- An agent's own edit of one of its Scope Recall skills stays across an upgrade that leaves the skill as it was. A Hermes agent keeps what it learns in its skills, and one agent edited its memory skill between two releases that did not change it: the upgrade reported an edited prior file and stopped before its apply, which left the new package under the old wrapper and receipt. Such a skill is now left as the agent edited it and the rest is installed (`plan-install` lists it under `kept`); the receipt keeps the package's digest for it, so the next upgrade compares the skill with the package again. A skill the package changed, and any other edited file, is still a conflict.

### Scope Recall 3.4.0rc11 - 2026-09-28

- The version moves past the `v3.4.0rc10` tag.
- Claude Code's and Codex's prompts on this machine have their recall answered by the entry's MCP server, which the client runs for as long as it is open, with its LanceDB helper kept ready. A prompt hook is a process of its own, and one that started LanceDB for its recall was often not ready before the recall's budget ran out: 6 of 8 cold Claude Code prompts on the pilot recalled by words alone (`helper_request_deadline`), against 5 of 5 with the helper ready, in 2.2-2.5 s. The hook still stores the prompt itself and then asks the server for the recall only, with all of its time; the server writes no memory. A server that has not answered when 1.5 s are left is recalled alongside, with the LanceDB helper the hook started at its own start, and the query is then embedded twice; the hook uses the answer that ran its vector search, and a hook whose own recall went without it waits for the server until its own time is up. A server answer without its vector search is used as it is unless what failed was the server's own (its key, its LanceDB helper), when the hook recalls as well; an embedding call's failure (what the provider answered, the network, the time it took) the hook would meet as well. An answer that comes after the hook is done is dropped, and one that failed, or came back empty because its read did not finish (the store unreadable, or its time up), is replaced by the hook's own. The server listens on 127.0.0.1 and names itself with a token in a folder of the user's own profile (`%LOCALAPPDATA%\scope-recall\hook-endpoints\`, or `~/.cache/scope-recall/hook-endpoints/`); a hook asks the newest server of its own version once the server has proved it holds the token, which is never sent, and takes only an answer the server signed. A name whose process is gone, or is another process under its id, is removed without a connection. A prompt hook that asked says how its server answered on stderr (`CODEX_RECALL_RESIDENT:`), and a recall of the server's that failed with its reason (`failed:<reason>`). A server with a recall past the time its hook gave it tells every hook at once that it is busy, until that recall ends, and one that does not prove itself within 0.5 s loses its name until its own check, every 2 s, finds it answering in time. A recall that fails in the server is answered as failed, with the last frames of its traceback on the server's stderr. It reads its key again at the next prompt after its env file or the runtime config changed, or when it could not read them before. `hooks.json` does not change, so Codex's trust stands. A server started before an upgrade is not asked until its client restarts.
- An import's history leaves room for candidate evaluations. The worker takes embeddings before evaluations, and the backfill that 3.4.0rc10 added kept a page of them waiting at every pass (up to 127: the queue was measured before a page of 64 joined it), so every pass of 32 took embeddings alone: on the pilot, 251 evaluations (220 of them given another look by `retry-failures`) waited behind the backfill for the hours it runs. While an evaluation the pass would take is ready, the backfill now tops the queue up to half a pass (16 at 32 items) instead of 64, so evaluations share every pass once the embeddings already waiting are taken (at most two passes of 32), and the backfill still moves when they cannot be done, if slowly: while a model refuses them before any request, a quiet store's worker wakes every five minutes and takes 16. Otherwise a page tops the queue up to 64 and never past it. One under a provider hold, or in a pass without an evaluator or that only purges, does not count.
- A prompt, a reply or a session-record line holding half of a broken emoji (a lone surrogate, which JavaScript's `JSON.stringify` writes) is stored with U+FFFD in its place, from a hook on this machine or over HTTP from another (a remote entry's request failed). A prompt or a reply holding one was refused whole (`INPUT_INVALID`) and a record line was skipped: the whole message was lost for one character. In a session record, a line whose own id holds one is still skipped, and a prompt id holding one is set aside (the message is then matched by its words and moment). A hook payload, a session-record line or a Codex reply nested deeper than the JSON parser takes is passed over (the hook answers nothing) instead of ending the hook, and the rest of the session record is still read.

### Scope Recall 3.4.0rc10 - 2026-09-28

- The version moves past the `v3.4.0rc9` tag.
- A candidate's evaluation keeps its verdict when the model leaves a supplied source out of `source_refs` or miscopies one. Which sources a question carried is the evaluation's own record. Asked to echo every supplied ref, the model got it wrong in 5.2% of the pilot's evaluations with nine or more sources (2.8% with five to eight, none with one), and each such verdict failed for good as `derivation_invalid` (`candidate_source_refs`): 188 since 2026-09-20. With the other refusals, 7-8% of the model's evaluations failed on 2026-09-27 and 28. What a verdict cites (a span, a counterexample, a state reference) must still be one of the supplied sources. `scope-recall retry-failures --config <runtime-config.json> --include-terminal --apply` gives each evaluation that failed this way one more look, unless it already had one in this schema generation (1110, current since 3.2.0): none of the 182 still failed on the pilot had. The flag also re-opens every other terminal failure not yet looked at again: 24 more on the pilot, each `derivation_invalid`.
- A task goes on in a new episode after its episode is deleted. Deleting a source deletes the episode whose resume rests on it, and a task names its episode series outright, so every later capture of the task landed on the deleted episode, was refused (`SOURCE_MISSING`, `episode_unavailable`) and stayed in the capture inbox for good, with doctor reporting `capture_ingress_blocked` from then on. On the pilot the Codex thread in which the owner sent a delete, and a work-computer thread whose test turns were deleted, stored nothing after it. A capture refused that way before this version (a bare `SOURCE_MISSING` in the inbox) is replayed once more; a missing source is now recorded with its field (`SOURCE_MISSING:<field>`), so a failure after that replay stays final. Such a row now wakes the worker (the wake counted only rows never tried and two passing failures), and doctor no longer counts it as blocked. A capture whose key another message had taken is still given a new key by the next pass; one that its new key cannot store either now keeps a final code (`VERSION_CONFLICT:rekeyed`) instead of being refused again on every pass, and a long message whose key was taken is stored under a new group: its segments had kept the old group and were never stored. A key of 490 characters or more is cut to fit its new key. A row whose stored capture a replay cannot check again (a context field a newer release wrote, a host whose check fails for now) is put off instead of stopping every row after it on every pass: it is tried again after a minute, doubling to an hour, by the replay that put it off and whichever release runs, and when its 24th try again fails it is given up where doctor (`capture_inbox_given_up`) and the patrol show it. Once its cause is fixed, `retry-failures --apply` returns it to the replay: those of the partition its config's worker replays, which without `--apply` it counts by cause (`inbox_by_kind`). Run it after going back to an earlier release and forward again, since such a row may have been given up meanwhile. A contract error is named with its field, and a stored context the release cannot read as `INPUT_INVALID:ingress_context`. A delete keeps such a row, and a key collision waiting for its new key, unless it holds a deleted message: all of its text (whitespace aside) where that text is 24 characters or more, or the row is that text with at most a tenth more; the same letters and digits with at most a tenth more, punctuation and case aside, where it has four or more ("我要辞职了。"); one of its segments as stored; or for a row not taking a new key the same source. A message that quotes only part of it, holds a short one ("ok") among other words, or quotes a long one reformatted (its punctuation or case changed), is kept, and so is a short one with a few words more ("[图片] 我要辞职了"); a message of only symbols or emoji is compared whitespace aside only. A row taking a new key is no longer matched by the key it took, and one the rekey path gave up goes back to that path. A key collision now wakes the worker, whose next pass stores it under a new key, and doctor no longer calls it blocked; a passing failure (a busy store) no longer takes a row off its path or its place across a delete; and a suppress no longer cancels the captures waiting in the inbox: what arrives of the same message, or restates a suppressed claim, is suppressed as it is stored. A copy of it that took another message's key is stored under a new key without the suppression (as one arriving later is). A worker pass says how many rows it put off and gave up (`ingress_deferred`, `ingress_given_up`) and keeps both in its status file, where doctor shows them. A busy store met by a replay leaves the rest for the next pass, and the pass says so (`capture_gap:durable_ingress_pending`) instead of hiding what it did or skipping the replay of collided keys; a deferral the store did not take is not reported as one. Doctor calls a row put off blocked until its time is up, and the worker is woken only for rows of the partition it replays.
- The rest of a thread Codex opens to ask the model for suggestions is not stored either. 3.4.0rc9 kept out the request and its JSON answer, but the thread's tool calls and its end came through: four tool outputs of 2-11 kB and an end marker in one thread on the pilot. The thread is marked when its request is recognized (`scope-recall/host-threads/` beside an entry's pointer, or in the data directory of a store of its own), so that its later hooks, each a process of its own, can tell. The owner speaking in the thread, or its end, removes the mark, and a mark lasts a day at most.
- A candidate is woken only by evidence that can pose it a new question. Only first-hand testimony changes what an evaluation asks (`question_digest`), but any evidence marked a candidate pending and reset its evidence clock: 2,045 candidates on the pilot waited in `pending_evaluation` for an evaluation nothing would schedule, every agent read them as a backlog, and a tool output every few days kept them from going dormant. Other evidence is now recorded for the next evaluation and changes nothing else about the candidate, its dormancy clock included, and each worker pass returns up to 32 candidates left pending that way to `waiting_evidence` (`no_new_question`).
- An import's history in a person's roles gets the embedding its source store never queued. An import re-queued an embedding only where its source store had one, so a store that never had one left its history findable by its words alone: on the pilot one agent's, 1,928 of the owner's messages, 6,552 replies and 3,009 notes from June to September. Each worker pass queues the next 64 of them, from a cursor each worker keeps beside the vectors (`embed-backfill-<partition>.json`), and only while fewer than 64 embeddings wait, so a message captured now is never queued behind them; once none is left, the store is looked through again after a day. Only imports in the worker's own project and branch are queued: a store converted from 2.x can keep others, which this worker would never claim. doctor shows the backfill's last outcome beside each vector store (`embed_backfill_outcome`). Imported tool output is left out: 200,000 outputs on the pilot, more to embed than everything else in the store.
- A correction that names no claim to place it against no longer opens an unresolved update. Such a row could never close (`resolve_updates` needs one of its candidates revised), and 36 on the pilot were handed to every read; each worker pass closes the ones left as `obsolete`. The message itself is stored and consolidated as before.
- A prompt over 65,536 characters is stored once. It is stored in segments under keys of their own, so the Stop hook's read of the session record did not find it by its prompt id and stored it a second time; one still waiting in the capture inbox was missed the same way. A named message that was deleted also counts as said: once the delete was purged its rows no longer carried the key, and a read of the session record stored the words again under a key of the record's.
- A provider that answers that it is overloaded (HTTP 529, as MiniMax and Anthropic do) is refused for capacity, as 503 is: the item waits and is tried again, and the attempt is refunded. One such answer failed a candidate evaluation for good on 2026-09-28.
- A Hermes gateway starts its vector helper when it first binds, on Windows. The first search opened the helper, and its LanceDB import (about 2 s) could outrun that recall's budget: a probe run as a gateway's first turn after a start came back without its vector search (`helper_open_deadline`).
- A worker pass asks its drain for no more than its own budget. Windows' clock ticks every 15.6 ms before Python 3.13, and a pass that reached its drain within one tick asked for the time left computed as `deadline - now`, a hair over the budget for some clock values, which the drain refused: the pass failed as `worker_error:ValueError`. On a machine booted minutes before, about one such pass in ten; one CI run met it.

### Scope Recall 3.4.0rc9 - 2026-09-28

- The version moves past 3.4.0rc8, which was not tagged.
- Claude Code and Codex recall by meaning again. On the pilot, the owner asked the work computer's Claude Code and Codex what was outside their window. The answer was in the store: another agent had been told about a pigeon five days earlier. Neither found it. Only its meaning matched the question, and their vector search never finished:
  - Each hook is a new process, and the helper it started for the vector search spent 2.1 s importing LanceDB.
  - The search then read all 78,403 vectors of 3,072 dimensions (750 ms), more than the 2.9 s the search is given.
  - Hermes, which keeps its helper, was not affected.
  - A hook now starts the helper when the prompt arrives, so the import runs while the prompt is stored and the words are searched.
  - A remote entry's server keeps one helper started and ready.
  - The worker builds a nearest-neighbour index once a store holds 10,000 vectors, at the start of a pass with the time for it: IVF over 8-bit quantized vectors, about 8 s for the pilot's store. A search probes every partition and re-ranks five times its limit by exact distance, so the index makes each comparison cheaper and never decides which rows are compared: 10-40 ms against 748 ms, and in 6,600 checks against the exact scan under three entries' filters (one a single scope) no nearest row was missed. An HNSW index, tried first, missed 10 of 600 and once returned 2 rows for 10: half the store's rows share their vector with another row, and over such duplicates the graph leaves rows unreachable. A vector index of another kind is replaced. Compaction adds later vectors to the index.
  - A search no longer returns each hit's vector through the helper's pipe; nothing read it.
  - Replayed on the store as it stood before the question, the recall now finds the pigeon. Without the vector channel it found two unrelated claims.
  - A vector search whose helper was still opening the table when the recall's time ran out now reports `vector_error:TimeoutError:helper_open_deadline`. It returned no rows and no gap, as if the search had found nothing, so hooks lost the vector channel this way without a trace.
- Codex's own request for suggestions of what to do next is no longer stored as the owner's words. Codex sends it through the prompt hook.
  - On the pilot four were stored, 11,000 to 15,000 characters each, and claims were drawn from them as if the owner had said them. One of those claims filled a background slot in every recall on the work computer.
  - The model's JSON answer to such a request is not stored as a reply either; nine were.
  - Neither is recalled for.
- A prompt the store was too busy to take is recalled by meaning as well. The vector search came only with a stored or queued capture, so a capture that failed left the turn to a recall by words alone: six prompts on the work computer's two entries in one night. A prompt refused as holding a credential still goes without it, so nothing of it reaches an embedding provider.
- A prompt longer than a recall query's 8,192 characters is recalled for by its first 8,192, as Hermes does. Sent whole, the request was refused and the turn had no recall at all: three of the work computer's Codex prompts in one morning.
- A failed automatic recall says what stopped it: the class of the error and, for a contract error, its code. It goes on the hook's stderr (`CODEX_RECALL:`) and on the remote server's log line. The work computer's server said only `recall_exception`. A recall that ran without its vector search names the gap the same way (`CODEX_RECALL_VECTOR:`, and `recall without vectors: ...` on the server's line): until now only the model saw it.
- A prompt with more than 128 distinct search terms, about 150 Chinese characters and up, is recalled for by the first 128 it reaches. It was refused whole: no recall at all, by words or by meaning, on every host, and nothing said so. rc9's own long-prompt fix had not helped real long prompts, whose terms are mostly distinct. The terms are worked out once per query: a recall asks for them 150 to 190 times, and for a long varied prompt that had cost 4.3 s of a 4.6 s recall.
- A prompt whose first 8,192 characters are blank is recalled for by what follows them.
- A prompt is screened for a credential itself before its recall gets the vector search. A capture that failed before it screened the message had counted as no refusal.
- A long-running host whose vector table open failed after its caller had stopped waiting reopens the table on the next search. The helper's late answer was taken without a look, and every search said "not open" until the host restarted.
- The nearest-neighbour index is built again as one once it has more than 16 segments. Each compaction that indexed new rows added one and nothing merged them: at 120 segments a search over 78,000 vectors took 78 ms warm instead of 47. A build is recorded as started before it starts, so a pass the watchdog ends mid-build is not followed by the same build on every pass. An index already in place is looked at whatever the store's size, and only a build that is due is sized against the pass's time: the build estimate is twice the rate measured on a busy machine. An index of another kind on a store below the threshold is replaced too.
- "recall without vectors" names only a recall whose vector search did not run or did not finish, including one the whole search failed before. A search that ran and had candidates refused is no longer named.
- A server that cannot start its spare vector helper still serves, as before rc9, and a spare taken is kept when its replacement cannot start.
- Codex's request for suggestions is recognised by its whole frame, whatever the case and wording around "hyperpersonalized suggestions": it opens with a heading, names them in its first lines, runs to 8,000 characters and more (11,000-15,000 on the pilot) and carries at least three of its own headings. A note of the owner's about the feature, even a long one, stays theirs.
- A work computer's spooled hook gets a name no other hook of the same process can take. The clock can read the same twice in a row, and a second hook would have replaced the first.
- A deletion is erased on disk, not only hidden. The worker read a purge's operation id at the last colon of its work subject, and a v11 scope id holds colons (`workspace:6:hermes|agent:7:default|...`): it found no such operation, marked the purge obsolete as `authority_revoked`, and left the deleted text, its claims' payloads and its vectors on disk while every read of them stayed blocked. The pilot's first delete, of 21 sources and 69 claims, stopped there. The subject now splits at its first colon, in one place. A purge marked obsolete that way after one attempt, whose operation still has a layer to remove, is queued once more by the next pass.
- `doctor` reports each vector store's index (`index_outcome`: `built`, `present`, `below_threshold`, `deferred` or `failed`). It no longer advises an index from 100,000 embedded objects: the scan cost 750 ms at 78,000.

### Scope Recall 3.4.0rc8 - 2026-09-28

- The version moves past the `v3.4.0rc7` tag.
- A remote client's clock more than a minute ahead of this machine's no longer hides its messages. A recall finds nothing dated after its now, so with 3.4.0rc7, which kept the client's time so that a replayed hook stays one source, a message from a fast client clock was hidden from every recall until this machine's clock caught up. Now every time in such a request moves back by the same lead, so its latest is this machine's now and a turn keeps its order; each time clamped on its own would have sorted a reply said early in a turn after the next prompt. Within the minute the client's time is still kept.
- The secret screen's exemptions after a credential word are narrower. A placeholder, a mask, a type or a null, a word that says what the value is, and a dotted name, which must now start from a code root such as `os` or `settings`, are exempt only as all of the value up to its first space, in matching quotes or none, and a placeholder's name has no digits. 3.4.0rc7 let `password: Changed!2024`, `password: <b>Xk9#mP2q</b>`, `letmein(2024)`, `"wrong horse battery staple"`, `<hunter2>`, `my.pass.word`, "the wifi password is now Sunflower2024" and "the password is Strong!2024" through. The name before `token` may run to 128 characters instead of 64; a longer one hid its value. Code it still refused is let through: `input("Password: ")`, `token := os.Getenv("TOKEN")`, `if token == nil`, `password: Yup.string().required()`, `"credentials": {`, "the token is sent in the header", "token是什么意思". Refused again, as 3.3.0 did: `password = data["password"]`, `password = hash_password(raw)`, `credentials: dict[str, str] = {}`, "the password is too short", "the token is only valid once".
- Known gaps: credentials the secret screen lets through, each refused by 3.3.0. As in 3.4.0rc7, nothing after a value's first space is examined (`password: str Xk9#mP2q`, `DB_PASSWORD: str = "…"`, `settings.DB_PASSWORD or "…"`, `config.get("api_key", "…")`, "my password is this: …", "the password is stored in the vault as …"), and `letmein()`, `love()you`, `This.Is.Sparta`, `$UPERMAN99`, `%Hunter2%` and a mask of x's pass. New in this version, which 3.4.0rc7 refused: an opening bracket alone is exempt (`password: { value: … }`), and three exemptions are judged by how the value begins, whatever follows: a call or subscript on a dotted name (`password: Mr.Smith(1985)`, `J.Doe[2020]`, `Auth.builder().withSecret("…")`), punctuation alone up to a space or a line's end (`password: !@#$%^&*`, `password: # sunshine`, `password => …`, a YAML `|` or `>` block, a quote opened at the end of a line) and a Chinese question word (`token是什么Xk9#mP2q`). A `Cookie:` header's value on the next line is no longer caught.
- Four patterns of the secret screen are linear on adversarial text. They took seconds: a cookie header's spacing ran across blank lines (4.3 s for 20 kB), a backslash run with no break after it (1.4 s), repeated "credential" (1.0 s), repeated PEM BEGIN markers (14 s for 100 kB). The whole-block PEM pattern is gone: the BEGIN marker decides, and the block is redacted by itself. A value's new end takes a run of closing punctuation whole, and so does a mask: given back one character at a time, a run of dots after a key word took 2.9 s for 20 kB.
- Redaction replaces the union of every pattern's matches, found on the text before any is replaced. One pattern at a time, a password's match swallowed a token's key and left its value in the output.

### Scope Recall 3.4.0rc7 - 2026-09-28

- The version moves past the `v3.4.0rc6` tag.
- The embedding bound counts every character as one token, whatever the script (#151, reported by panxuewen0101). It counted three ASCII characters as one, which held for prose and let 6,000 characters of a digit-dense body through as 2,000 tokens: digits cost one token each against Zhipu `embedding-3`, so logs, IDs, hashes and JSON were refused with `http_400` and never got a vector. Code and symbols, at 2.0-2.5 characters a token, also passed Gemini's 2,048-token limit. Chinese keeps its 2,000 characters; long ASCII prose is now embedded from its first 2,000 characters instead of 6,000, and all of it stays in the lexical index. Sources that already failed this way are re-opened by `scope-recall retry-failures --config <runtime-config.json> --apply` after the upgrade.
- Claude Code and Codex recall a turn whose message was not stored. A capture that failed, or waited in the inbox because the store was busy, returned before the recall, so exactly when the writer held the lease the turn got no memory at all. A message not in the store has nothing of it a recall could bring back, and one refused as a credential is refused by the embedding request guard too, so its recall stays on the local lexical channel.
- An automatic recall on the shared store takes about half as long. The background profile query started from every claim's evidence link and read each linked source's whole posting list: 1.3 s of a 2.5 s recall (19,000 claim links, 11 million postings), for 2,300 links it could use; it now starts from the preference and constraint claims, 50-60 ms, with the same rows in the same order (checked on the store on five queries). The packet's size estimate counts with string operations instead of one Python step per character (180 ms of that recall, 1.9 million characters), and in linear time however many different symbols a text holds. In a process that has not opened the vector index yet, as every hook is, the recall took 5.2 s, past its deadline, and returned nothing; it now returns in 3.9 s with its lexical results.
- A Claude Code or Codex prompt waits up to 2 s for the writer lease, instead of 1 s, and at most half of what its hook has left, so a 2 s budget still waits 1 s and leaves the recall its time. In the work computer's first day 12 of its 55 prompts waited their one second and were not stored.
- Remote entries: a request without the token is read before it is refused. Answered first, the connection closed with the request unread, and Windows resets such a socket, so the client saw a broken connection instead of the 401 (seen once in CI).
- A worker's source page finds the candidates its source names in a read and links them in a write of its own, each candidate checked again there. The matching held the writer lease 0.5-7.4 s a page on the shared store (median 1.3 s), and a hook that waited its second for the lease meanwhile lost its capture. When another drain linked the page first, the write finds the next page itself rather than close the trigger on an empty one. A capture still matches its own source inside its write (1-2 s for a long one); that is left for its own change.
- The deferred-source probe asks for the newest revision, as the page does: nothing clears an older revision's marker, so one started the page's scan of every source on every pass.
- The secret screen lets through what cannot be a credential after "password", "secret", "token" or "api key": a placeholder (`<your-api-key>`, `${API_KEY}`, `$API_KEY`, `%API_KEY%`, `{api_key}`, `[REDACTED]`), a mask or an empty string, a type or a null in code (`def login(user: str, password: str)`, `Optional[str]`, `None`), a word that says what the value is ("password: reset it from the login page", `required`, `see`), a call or subscript on a lower-case name (`os.environ["KEY"]`) or a dotted name (`settings.DB_PASSWORD`), and after "is" a short list of words ("the password is required", "the secret is out"). Each such message was refused and never stored, and a model request carrying one was refused as `sensitive_request`. Anything else still counts, in any script: `$unshine2024`, `[hunter2]`, "my password is iloveyou", "password: correct horse battery staple". The name before `token` is at most 64 characters, so a long hyphenated line scans in linear time (it took 18 s for 60,000 characters).
- A recall's query is screened whole before it is embedded: the request guard saw only what the input bound keeps, so a key across the cut went out in part.
- Hermes: `post_llm_call` no longer takes the adapter lock. Hermes calls it before it sends the reply, and the reply waited behind whatever held the lock, a capture on a busy store or a recall; a callback Hermes gave up on (30 s) was then skipped for 60 s (Hermes 0.21.5), and for every session of the gateway, since Scope Recall registers one callback per hook. It writes nothing and keeps its copy under a small lock of its own.
- Hermes: a turn writes at most 64 of the messages it showed between tool calls, the first ones, and says `capture_gap:interim_limit` and logs a warning past that. Each is its own write after the reply, under the adapter lock; a turn of 300 tool steps held that lock for minutes.
- Remote entries: a request the server refuses for good (HTTP 400 or 413) is dropped from the client's spool and logged. Kept, it stopped every later flush until 256 newer hooks pushed it out. A store error answers 500 now, not 400, so the client keeps that hook to send again.
- Remote entries: a hook whose message the busy store could not take is kept in the client's spool and sent again; the server answered 200 whatever became of the capture, and the client took that as delivered. The server log says `not stored, to be sent again`.
- Remote entries: a Codex hook is written to the spool before it is sent. Codex ends SessionEnd and Interrupt at 3 s, and with the interpreter's start and a connection that does not open, a hook that waited for the server was ended before it kept anything.
- Remote entries: a message keeps the moment its hook ran on the client, so a hook sent again is the same source, while when it was stored, when its work falls due and a recall's now are the server's clock. The client's time set all of them: a work computer a day fast held its messages' embedding back by a day.
- Remote entries on an IPv6 address: the MCP endpoint's allowed hosts are bracketed, as the Host header is; every `/mcp` call was refused with 421. No install listens on IPv6 today.

### Scope Recall 3.4.0rc6 - 2026-09-27

- The version moves past the `v3.4.0rc5` tag.
- The release tier no longer has a model gate. The gate asked for a P18 formal evaluation receipt: 120 independent core items and 240 paired variants, scored by a party independent of the authors. This project has neither that corpus nor that party, so no release could pass it, and every release from 3.1.0 on shipped with the gate reported missing. `scripts/check.py --tier release` now requires only the suites it runs and exits 0 when they pass, where it used to exit 2. `--model-receipt` is gone, and so are the two scripts that extracted and validated an evidence bundle; the CI, release and PyPI workflows no longer look for a model evidence tag. Recall quality still has no automated check: see *What is not verified* in the README.
- `plan-install`, `apply-install` and `doctor` pass `--python` on as given. The CLI resolved it first, so on POSIX a venv's `bin/python` reached them as the base interpreter, which cannot import the package: the doctor reported `python_package_missing` and `entry_point_missing` on a healthy install, and the installer recorded the base interpreter for the worker, the autostart task and Codex's hooks and MCP launcher. 3.3.0 had fixed this (#87) below the CLI only (#141, reported by panxuewen0101).
- Windows: when a host starts the base interpreter and adds a venv's packages to its path instead of starting that venv, the native vector helper still finds the venv's dependencies. Its launcher hint names the environment that owns the installed package instead of the base interpreter; the helper is still the base interpreter (#139, by JohnYinl).
- Claude Code and Codex on another machine: the hook writes its answer as ASCII-only JSON, as the local hook does. It wrote the client's code page (GBK on a Chinese Windows: the hook runs with `-I`, so `PYTHONUTF8` does not apply), and the host reads UTF-8, so recalled Chinese text arrived garbled or not at all.
- A remote entry's server opens no path a request names. A Claude Code Stop or SessionEnd that sent no record lines made the handler read the payload's `transcript_path` on the server's machine: a file of the client's that does not exist there, or, sent on purpose with the entry's token, one that does. The server now drops that field and reads only the lines the client sent.
- The server's log carries a failed capture's error code (`DEADLINE_EXCEEDED`, `SECRET_DETECTED`, ...); it had only the handler's reason, which left `capture_failed` unexplained.
- Remote client: a missing token file answers nothing and is logged, instead of a traceback; a full spool logs what it drops; a Claude Code hook command refuses paths a shell would split, as the local installer does; Codex's POSIX command is quoted as shell words.
- Claude Code's session record: a `promptId` the store cannot bind (a lone surrogate) is dropped and the message kept, matched by its words and moment; every later Stop of the session had stopped at that line. `docs/remote-entries.md` no longer says a Claude Code client loses no message, and says what the spool drops and that the plugin's `.mcp.json` carries the token.
- Hermes records what the person sends while a turn runs. Hermes delivers it as a "steer" row inside the turn, in its out-of-band marker and, from a gateway, after an origin preamble of chat and user ids; none of it was stored (6 such messages since 09-24 on two agents), and the turn's scan stopped at that row, so what the assistant said before it was lost too. It is stored as the person's words, without the marker or the preamble.
- Hermes: a capture that timed out on a busy store is written again at the next `sync_turn`, on Hermes' memory worker after the reply. It waited in memory for the session's end or a compression, hours on a long chat, and a gateway restart lost it: the answers missing on 2026-09-26, when the worker still held the writer lease most of the time. A capture the store queued durably no longer keeps one of the adapter's 64 pending slots until the session ends; with 64 such, every capture in the session was refused. A capture not stored is logged at WARNING with its source key and code, never its content; nothing was logged before.
- Hermes: a compression mid-turn keeps the turn. It gives the conversation a new session id while the turn goes on, and the switch cleared the turn, so what the turn said on the way was not recorded and its opening message was stored a second time under the new session.
- A worker pass chooses which deferred sources to refill in a read, and writes only for those. With one source deferred and no room for it, or an older revision's marker that is never selected, the choosing scanned every source under the writer lease on every pass and chose nothing.

### Scope Recall 3.4.0rc5 - 2026-09-27

- The version moves past the `v3.4.0rc4` tag.
- Codex's hooks wait as Claude Code's do: 15 s for a prompt's hook, 10 s for Stop, 5 s for SessionStart, and the 3 s Codex allows SessionEnd and Interrupt; PostToolUse keeps 2 s. A Codex entry's prompt hook runs the entry's `hook_processing_seconds` (6 s unless set lower) like Claude Code's. With 2 s, most of Codex's automatic recalls on a large store came back empty, and a remote Codex client spent most of them on the network. These are ceilings: a hook answers as soon as its capture and recall are done. Codex asks you to approve the changed hooks again.

### Scope Recall 3.4.0rc4 - 2026-09-27

- The version moves past the `v3.4.0rc3` tag.
- Two more of a worker pass's start-up checks read without the writer lease. Whether any source waits deferred for queue capacity is a scan of every source (9.8 s on the shared store, with none waiting), and which settled candidates to queue walks every candidate still settling (7.6 s, with none due); both ran inside a write. The pass now reads both first and writes only for what it found. Timed on a copy of the shared store, one pass held the lease 23 s with 3.4.0rc3 and 6.8 s with this candidate, most of it the capped source pages.

### Scope Recall 3.4.0rc3 - 2026-09-27

- The version moves past the `v3.4.0rc2` tag.
- A worker pass no longer holds the store's writer lease for most of a minute, which made hooks fail to capture. Its truncated-source pages each count the pending pages and find the next one inside the page's write, and SQLite answered both from every source of every scope instead of the few triggers: 2.7 s and 3.5 s a page on the shared store (8,819 triggers, 192 truncated), up to sixteen pages back to back. Both now start from the triggers (4-5 ms), the pass waits 20 ms after each page so a writer waiting for the lease gets its turn, and it spends at most 5 s on pages: a page still matches its source against every candidate sharing a term under the lease, 0.5-7 s on that store. While the backlog lasted, a pass started about every 100 s and held the lease about 70% of the time; every capture from the remote entries failed, and so did local ones, with no recall where the capture failed.

### Scope Recall 3.4.0rc2 - 2026-09-27

- The version moves past the `v3.4.0rc1` tag.
- A remote Codex client starts the flush of its spool with a console that has no window instead of detached. Detached, a launcher's `python.exe` child got a console of its own, which Windows Terminal shows as a window on the client machine's desktop.
- `docs/remote-entries.md`: on Windows the server runs from a `pythonw.exe` that opens no console. uv 0.12.1's is a copy of its console launcher, and closing the window its console gets stops the server.

### Scope Recall 3.4.0rc1 - 2026-09-27

- The version moves past the `v3.3.1rc1` tag, to 3.4.0: a client on another machine is a new capability.
- Claude Code or Codex on another machine can be an entry of the shared store, under a name of its own. Its hooks forward each payload to the entry's server here over HTTP (`remote_client`, `remote_server`), the handler here records it under the entry, and the entry's MCP tools are served over streamable HTTP. The server listens on one private address and refuses a request without the entry's token, which the client machine makes and keeps; this machine keeps its SHA-256. A Claude Code client reads its session record on its own machine and sends only what the record shows being said. When the server cannot be reached a hook gives up after 3 s and no hook tries for a minute: Claude Code's record carries its messages to the next Stop, and a Codex hook waits in a spool on the client and is sent with the moment it happened. The client never goes through the machine's proxy. The server logs each hook and each refused request to a file beside its config, the client what did not get through. See `docs/remote-entries.md`.

### Scope Recall 3.3.1rc1 - 2026-09-26

- The version moves past the `v3.3.0` tag.
- Hermes records what the assistant shows between its tool calls. Hermes hands the memory provider only a turn's answer, so the rest is read from the conversation Hermes passes to `post_llm_call` when a turn with an answer ends, and written with the answer. Tool output, hidden rows and the text Hermes strips as thinking are not recorded.
- Claude Code: a message read from the session record is matched to the prompt hook's copy by its prompt id, so the same short words said again are kept as a new message; a message sent while a turn runs carries no prompt id and is still matched by its words and moment. A prompt still waiting in the capture inbox is not stored a second time from the record. A record line holding an unpaired surrogate is skipped instead of stopping every later read, and the check runs within the Stop hook's time, so it never upgrades a store's schema.
- A capture's queue-capacity check starts from the pending work (the `work_ready` index) instead of every finished item of its type: about 0.3 s a capture on a store with 95,000 finished items, which the session-record read spent on each message.

## [3.3.0] - 2026-09-26

3.3.0 lets Hermes, Codex and Claude Code share one memory. In 3.2.0 only Hermes agents could attach to a shared store; now Codex and Claude Code attach to the same store as entries. What you tell any of them, the others can recall; each recalled item says which agent it came in through, and a deletion through any agent applies to all of them. An agent you do not attach keeps its own store. How to set one up: [docs/shared-store.md](https://github.com/410979729/scope-recall-hermes/blob/v3.3.0/docs/shared-store.md).

### Requirements

- Python 3.11 to 3.14. The wheel declares `>=3.11,<3.15` (#135).
- Install the package into the same Python environment as the host. Codex and Claude Code need the `codex` extra, which carries their MCP server.
- Claude Code 2.1.196 or later, the first to send the prompt id a turn is recorded under.
- Hermes Desktop builds the environment it runs plugins in and builds it again on updates, which drops a core installed there by hand. Install its plugin into `<home>\plugins\scope-recall` (`--target-plugin-dir`): Hermes reads the core the plugin declares from there, and setting the provider up again after a rebuild installs it. See section 1 of [docs/install.md](https://github.com/410979729/scope-recall-hermes/blob/v3.3.0/docs/install.md) (#135).

### Also in this release

- Claude Code records each prompt and reply and, at the end of each turn, the text it showed while it worked; a message its hook could not write is recorded at a later turn. Tool calls and their output are not recorded.
- Claude Code's automatic recall gets the entry's `hook_processing_seconds`, 6 s unless set lower, instead of 2 s, which cut most recalls on a large store short. Codex's hooks keep their 2 s.
- A background task's completion notice is no longer stored as your own words.
- An embedding request stays within the ledger's `max_request_bytes`; a group of long sources used to be refused on every pass.
- A Codex installation whose id ends in eight digits no longer has every capture refused as a secret.
- The shared store's commands and `doctor` read a worker config of up to 1 MB. Past 64 KB, a few hundred scopes, `attach` refused the store and `doctor` reported the config invalid.
- An entry whose routes came from another store searches the shared worker's vector table, and an `attach` that changes nothing no longer restarts the worker.
- A Hermes plugin whose core is missing says so; any other import error is shown as it is.

### Upgrading from 3.2.x

1. Stop the hosts and the Scope Recall worker, and take a `backup`.
2. Install the 3.3.0 package, then run `plan-install` and `apply-install` for each host.
3. Start the hosts again and run `doctor`.

The store's schema is unchanged (1110), so a 3.2.x process can still open it. To attach Codex or Claude Code to a shared store, follow [docs/shared-store.md](https://github.com/410979729/scope-recall-hermes/blob/v3.3.0/docs/shared-store.md).

This release ships without the P18 formal acceptance receipt. Every change, with its details, is in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/v3.3.0/CHANGELOG.md).

## [3.3.0 candidates] - 2026-09-24 to 2026-09-26

The changes as they were written when each landed, from the release audit back to 3.2.1rc1, which became 3.3.0rc1.

### Scope Recall 3.3.0, the release audit - 2026-09-26

Two AI reviews of everything since 3.2.0, before the tag. What they found and this release fixes:

- Claude Code's hooks ran 2 s when the entry's runtime config did not name `hook_processing_seconds`, which no installer writes, so most automatic recalls on a large store came back empty. They run the worker's default, 6 s; a value out of bounds still falls back to 2 s.
- `doctor` read a store's `runtime-config.json` only up to 64 KB, the limit 3.2.1rc1 lifted for `attach`, `detach` and `adopt`: a shared worker's config past it was reported as `vector_threshold: invalid` on every entry, and the storage budget went unchecked. It reads what the shared commands write, up to 1 MB.
- The core 3.3.0rc5's Hermes plugin declares was never read by the Hermes it was for. Hermes reads a memory provider's `plugin.yaml` from `<home>/plugins/<name>/` or from the installed core's own directory, and the installer refused a plugin directory inside the home, so once a rebuild dropped the core nothing declared it. For Hermes, `<home>/plugins/scope-recall` is now accepted as `--target-plugin-dir`, the one plugin directory that may sit inside a home.
- The Hermes plugin reported every import error as a missing core, and sent an operator to install a core that was there, older or missing a dependency. Only a missing `scope_recall` is reported as one now.
- The docs still said only Hermes attaches to a shared store, that the install guide covers 3.1 and 3.2, and that security fixes go to 3.1.x; `constraints/runtime-min.txt` and `runtime-max.txt` said CI installs them, which no job does.
- Not changed: six minor edge cases in how Claude Code's session record is read and matched against what its hooks stored, left for a later release.

### Scope Recall 3.3.0rc5 - 2026-09-25

- The version moves past the `v3.3.0rc4` tag.
- Python 3.13 and 3.14 are supported (`requires-python` `>=3.11,<3.15`), once every tier had run on 3.14 (#135: Hermes Desktop now builds its plugin environment on 3.14.7, which 3.2.0's `<3.13` shut out). One thing failed there and is fixed: on Windows, CPython 3.13 added `os.fchmod`, which the sqlite vector store then called on its files and was refused; as the truth database already did, it sets no POSIX mode on Windows. CI runs the in-process tiers on 3.11 and 3.14 on both systems, and `uv.lock`, which still held an earlier `pyproject.toml`'s specifiers, is regenerated.
- The Hermes plugin that `apply-install` writes declares the core it runs on, `pip_dependencies: hermes-scope-recall[lancedb]==<version>`, when that version is a release on PyPI (#135). Hermes Desktop's package manager builds the environment it runs plugins in from those declarations and builds it again on updates; a plugin that declared nothing lost its core then, and Hermes logged only that the provider had no instance. A candidate, development or local build declares nothing, because a requirement Hermes cannot resolve fails its whole build, and is installed by hand. A plugin whose core is missing now says so when Hermes loads it. `docs/install.md` describes both.

### Scope Recall 3.3.0rc4 - 2026-09-25

- The version moves past the `v3.3.0rc3` tag.
- Claude Code's turns are recorded from its session record as well as from its hooks. The hooks carry a turn's prompt and last message, not the text Claude Code shows while it works, and a hook's capture that could not be written was lost: on the pilot on 2026-09-25 one prompt in six and one final reply in two never reached the store, every check before the write having passed. At the end of each turn the Stop hook now reads what the session record (the hook's `transcript_path`) gained since the last read and records the owner's messages, those sent while a turn ran included (the record marks them `origin.kind: human`), and each block of text Claude Code showed. Tool calls and results, compaction summaries, task notifications and meta entries are not recorded. A message a hook already stored is recognised by its words and moment and not stored twice; one that cannot be written ends the read, and a later turn goes on from it. A long session is read over several turns, at most 3 s each, and Claude Code's Stop and SessionEnd now run the entry's `hook_processing_seconds` (Claude Code waits 10 s for them). Codex is unchanged.
- A background task's completion notice is no longer recorded as the owner's words. Claude Code hands it to the model as a prompt opening with `<task-notification>`, and the prompt hook stored it as a message the owner wrote (the pilot's first one, today) and ran a recall on it. The session record marks it `task-notification`, and the record's reader never took it.

### Scope Recall 3.3.0rc3 - 2026-09-24

- The version moves past the `v3.3.0rc2` tag.
- An embedding request stays within the ledger's `max_request_bytes` as well as the provider's hundred texts. The ledger refuses a larger body before it is sent, as `budget_unavailable`, and the worker defers the whole group an hour, so a group of long sources was refused every pass without a word: on the pilot's shared store the last 6,000 sources of a rebuild, a hundred of which made about 600 KB against 128 KB, waited eight hours with nothing sent. They go through as they come due; nothing has to be requeued.

### Scope Recall 3.3.0rc2 - 2026-09-24

- The version moves past the `v3.3.0rc1` tag.
- A digit run glued to other text no longer reads as a Telegram bot token. A Codex source key ends its hex installation id in eight digits for about one installation in 45 and runs on into the session UUID; the key matched the token pattern, and every capture of such an installation was refused as a secret without a word. It was also the intermittent failure of `test_mcp_stdio_all_tools_and_host_thread_bound_mutations`, which draws a new installation id each run. None of the pilot's installations has such an id.
- A client entry's session start reads nothing of the store. It counted the whole store as a local installation does, 7-8 s on the pilot's shared store, past the 2 s Codex gives a hook, at every Codex session start.
- A candidate's evaluation window ranks and names a source by its effective origin, as the promotion rules read it: a person's message that came in with an imported store is first-hand. Ranked by the stored column it sat behind every newer tool output, and the question it posed did not count it (found in the 3.2.0 audit). On the pilot's store no waiting candidate holds such a message yet (80 of 489,203 evidence rows point at imported sources, none a person's), so no evaluation is asked again because of this.
- Claude Code's prompt hook runs the entry's `hook_processing_seconds` (at most 6 s; Claude Code waits 15 s) from the start instead of the 2 s Codex's hooks get. Recall on the pilot's shared store took 2.7-5.7 s, so with 2 s most automatic recalls came back empty. The prompt waits that long before the model answers; lower `hook_processing_seconds` in the entry's runtime config to trade recall for speed.

### Scope Recall 3.3.0rc1 - 2026-09-24

- The version moves past the `v3.2.1rc1` tag, to the minor version in which Codex and Claude Code join a shared store.
- Codex and Claude Code attach to a shared store as entries ([docs/shared-store.md](docs/shared-store.md)). A local client brings no grants of its own: `attach --host codex|claude-code` gives it the owner grants of attached Hermes entries (`--grants-like`) and captures into a scope every owner row reads (`--capture-like`), so what the owner types into it reaches every agent and no other entry's binding changes. The Codex adapter serves both clients; `--home` replaces `--config` for an attached home, whose audience does not depend on the workspace, and its prompts are the owner's, verified by the attach the way the Hermes CLI's are.
- `apply-install --host claude-code` writes a Claude Code plugin (hooks, MCP stdio server, the memory skill) into `~/.claude/skills/scope-recall`, which every session of that user loads. Prompts and final replies are recorded; tool calls are not (from rc4 also the text Claude Code shows while it works, and messages sent while a turn runs). Changing a memory through Claude Code's MCP tools is refused: they carry no conversation id. `doctor --host claude-code` checks it. `--project-root` is needed only by a Codex that keeps its own store.
- An entry's runtime config searches the shared worker's vector table. An entry whose routes came from a store that named its table otherwise searched a table nothing fills, and its recall lost the vector half; one agent's did, from its own 3.1 store, after it joined the pilot store on 2026-09-24.
- An `attach` that leaves the shared worker's config as it was no longer rewrites it: any write makes the running worker restart.

### Scope Recall 3.2.1rc1 - 2026-09-24

- The version moves past the `v3.2.0` tag.
- The shared store's commands read a worker runtime config sized for every scope a store may hold. The worker's config lists each scope twice, about 120 bytes a scope, and `attach`, `detach` and `adopt` read it only up to 64 KB, which the pilot's 221 scopes had nearly reached (58 KB). Rehearsing two more instances on copies of the live stores, the first attach took the store to 332 scopes and 85 KB, and the second was refused with `runtime-config.json is missing or too large`; `detach` and `adopt` would have been refused as well. The limit is now 1 MB, about four times what `MAX_SHARED_SCOPES` (1024) needs, and `attach` refuses before it writes a config it could not read back. The worker and its wake never had the 64 KB limit, so no running store stopped.

## [3.2.0] - 2026-09-24

3.2.0 lets several agents share one memory. Until now each agent had a store of its own, and what you told one of them the others could not recall. Attach each agent to a shared store, and what you tell one agent the others can recall; each recalled item says which agent it came in through, and a deletion through any agent applies to all of them. `import-entry` brings each agent's existing memories into the shared store. Upgrading does not make a store shared: an agent you do not attach keeps its own store. In 3.2.0 the agents that can attach are Hermes agents; Codex and Claude Code attach to the same store from 3.3.0. How to set one up: [docs/shared-store.md](https://github.com/410979729/scope-recall-hermes/blob/v3.2.0/docs/shared-store.md).

### Requirements

- Python 3.11 or 3.12. The wheel declares `>=3.11,<3.13`; Python 3.13 and later are not supported by this release (#135).
- Install the package into the same Python environment as the Hermes host.

### Also in this release, for every store

- A tool's output is still stored and found by its words and meaning, but it is no longer turned into facts.
- Recall searches every scope an agent may read in one vector request, and a capture made while a recall is being read no longer empties the result.
- A memory's time is shown in the host's time zone instead of UTC.
- A name given for the first time ("my cat is called …") is kept as a fact; it used to be filed as an alias that nothing could confirm.
- Embedding is faster and holds up better when the provider refuses a request.
- Fixed reports: long Chinese text refused by the embedding provider (#125); Feishu sessions refused for carrying two ids of one sender (#116); WeChat, Feishu and Desktop-login sessions refused because of their session key (#124); a store refused after a 2.0 plugin had opened it (#117); a watchdog flag that could delete the runtime config (#118).

### Upgrading from 3.1.x

1. Stop the Hermes host and the Scope Recall worker, and take a `backup`.
2. Install the 3.2.0 package, then run `plan-install` and `apply-install` for each host.
3. Start the host again and run `doctor`.

The first time 3.2.0 opens a store it moves the schema from 1109 to 1110, which reads every source row once (about 3 s per gigabyte). The step is one way: a 3.1 process cannot open the upgraded store, so upgrade every process that opens the same store together, and keep the backup to roll back. On a store larger than 100 MB the step runs at the next worker pass, `apply-install`, `upgrade-store` or Hermes session; until then hooks report `upgrade_pending`.

Optional one-off commands afterwards, described in [docs/install.md](https://github.com/410979729/scope-recall-hermes/blob/v3.2.0/docs/install.md):

- `retire-rootless-claims --apply` retires the unconfirmed facts earlier releases derived from tool output alone.
- `retry-failures --apply` retries the embeddings that failed with `http_400` on long Chinese text.
- `repair-claim-frames` turns first-time names that earlier releases stored as aliases into facts.

As with 3.1.2, this release ships without the P18 formal acceptance receipt.

The entries below are the changes as they were written when each landed.

### What the release audit found

Four AI reviews of everything since 3.1.2, none by the agent that wrote it, before the tag. None found a memory reaching a reader outside its scopes; these are what they did find.

- No automatic writer derives a claim from tool output alone any more; the candidate evaluator was still one. An evaluation queued before 3.2.0rc6 for a proposal derived from tool output still reached the model, and a verdict quoting only a tool output made it an active claim (the test that pins this fails before this change, with the proposal resolved `fact_active`); a verdict quoting a tool output's value beside any fragment of a person's message was even written as that person's report. Now a verdict writes a version only when it quotes a person's or a document's words and those words carry what the claim says -- its value, or every step of a procedure (`rooted_verdict`); an agent's echo of a tool output carries nothing. A candidate that neither cites nor holds any such words is answered without a model call (`waiting_evidence`, `no_derivation_root`), whether it comes up at scheduling or was queued before the upgrade, and is asked again when a person speaks. `DERIVATION_ROOT_ORIGINS` has one definition, in `core/evidence_question.py`, read by consolidation, the evaluator and `retire-rootless-claims`. The capture-time confirmation and correction paths already took only a person's words.
- The deferred refill no longer picks a tool output for the consolidation it is not owed. While a scope's embedding queue was full, such a tool output matched the refill's consolidation clause, could not be scheduled either, came first on every pass and held the page, so a person's message deferred behind it was never refilled. It is picked for that clause only once its embedding is queued, to settle a marker an earlier release wrote.
- `retire-rootless-claims` leaves a proposal a person has since restated to its evaluation (`restated_in_evaluation`). `apply_claim` takes a person saying a proposal again for a duplicate, so their words sit in the proposal's evaluation and not in its evidence, and retiring it dropped them. A message that only shares a word with it restates nothing: of the 2,785 proposals the pilot retired, 1,525 had such a message attached, 62 had a person's message containing the value, and the model had already judged 58 of those with that message and not promoted them.
- `upgrade-store` names what a restamp leaves in the file that is not this store's (#117): the tables this release's schema does not create, with their rows, read once the store is at this release's schema (`tables_not_in_schema`, with a `warning` when any holds rows). The 2.0 process that stamped the header may have captured turns into its own tables, and a restamp that said only `restamped` left them in the file unseen. Only a header in the 2.x layouts' numbering (10000 and up) is restamped, never one in 3.x's own range, and the restamp opens the file without ever creating it.
- A Hermes CLI audience row is never relaxed on the session key (#124). The CLI sends none; Hermes reports a relayed `local` gateway session to plugins as platform `cli`, with its key, and the relaxed match gave such a session the CLI's owner scope.
- A name said under a condition or as an example is not re-framed as the thing's name (`name_frame`): "如果我养猫的话，我的猫叫年糕" and "比如我家猫咪叫年糕" stay what the model proposed, as they were before 3.2.0rc5.
- `import-entry` records an imported deletion at the epoch the import moves the store to, not at the old store's own. A deletion's epoch is compared with a read's (`retraction_after`), and an old store's higher numbers read as a deletion after every read in their scopes -- recall emptied, derived work failed with `memory_epoch_changed` -- until the shared store's own epoch passed them. The pilot's imports are past it: their highest was 22, and the store is at 4,178. `docs/shared-store.md` now also says that a deletion an agent's own store made before its import covers that agent's copies only.
- The migration tier opens a store the 3.1.2 release's own code wrote: schema 1109 to 1110, every source kept and marked `local`. The older fixture, a 3.1.0 store, stays for the 1108 step.
- What an upgrade does to a store is stated where it is read: a step is one way (a 3.1 process refuses a store 3.2 has opened, so going back is restoring the pre-upgrade backup, and `AGENT_WORKFLOW.md` says so beside its rollback), on a store above 100 MB any pending step waits for a caller with a minute of budget, and 3.2.0's step reads every source row, about 3 s a gigabyte. `docs/deletion-contract.md` describes delivery as 3.2.0rc5 left it (`retraction_after`).
- Not changed: the embedding bound estimates three ASCII characters a token, from #125's measurements. Text dense in digits and hex -- hashes, UUIDs -- tokenizes shorter, and 6,000 such characters may still exceed a 3,072-token provider; such a source fails with `http_400` and stays found by its words.

### Five reports from other installs

- The embedding input bound is an estimate of tokens, not a count of characters (#125). Providers limit tokens, and a Chinese character is about one: against a 3,072-token provider the 8,000-character bound let through more than that for any text denser than about a quarter Chinese, so ordinary Chinese sources failed with `http_400` for good and never reached the vector channel, while 12,271 ASCII characters passed. The estimate counts one token for every character outside ASCII and one for every three ASCII characters, and the bound is 2,000 of them: 6,000 ASCII characters or 2,000 Chinese ones, under the provider limit for every input the report measured. The cut keeps the longest prefix that fits and still carries the truncation marker. The embedding space is unchanged, so no stored vector is rebuilt.
- A Hermes session that carries both `user_id` and `user_id_alt` binds (#116). Feishu sends its open_id as `user_id` and its union_id as `user_id_alt` on every session, and Signal a UUID beside the number: two stable ids of the same sender in different namespaces, which the host itself keys participants on. The adapter refused any pair that differed with `conflicting user_id and user_id_alt`, before any manifest was read, so every Feishu session ran without memory. The principal stays `user_id`, which every audience row and owner principal written so far is keyed on; `user_id_alt` is used only when `user_id` is empty, as before.
- A store whose SQLite header was overwritten is named and repaired instead of refused without a word (#117). Every step writes a store's schema twice in one transaction, the header (`PRAGMA user_version`) and `instance_meta.schema_version`; after a 2.0 store was migrated, a 2.0 process that opened the new file stamped the header with the 2.0 layout's 10815, and with every 3.x table and row intact each open failed with `SCHEMA_UNSUPPORTED`, `doctor` reported only `storage_read:ContractError`, and `upgrade-store` called the store unsupported. The migration itself stamps the header (its target is created by `SQLiteStorage.initialize`, which refuses any existing file with another stamp). Now the refusal carries `header_stale:run_upgrade_store`, `doctor` reports `schema_header_stale` with the cause, and `upgrade-store --backup-dir` snapshots the store, writes the recorded schema back into the header inside a write transaction that checks it again (`header_restamped`), and brings an older store forward as usual. Only this product's store (its application id) recording a schema this release knows is restamped (`core.schema.stale_header_schema`); anything else is still refused.
- `worker_watchdog --cleanup-config` deletes only the per-pass config copy it was meant for, and `doctor` names a runtime config that was there and is gone (#118). The flag deleted whatever `--config` named; given an operator's real `runtime-config.json` it deleted that without a trace, and every host fell back to basic mode, no worker and no model routes, while `doctor` reported `ok` with no gap, because a missing runtime config was also how a fresh install looks. Now a file is removed only when it is named as `write_ephemeral_worker_config` names a copy, `<stem>-worker-<8 random>.json` (`runtime.worker_launch.is_ephemeral_worker_config`, shared by the writer and the watchdog); anything else is left in place and the refusal is written to stderr. `doctor` reports `runtime_config_missing`, `degraded`, when the file is absent but the store holds finished embeddings or consolidations, work that only a runtime config's routes run; a fresh install is still not a finding.
- A Hermes audience row with an empty `gateway_session_key` matches whatever session key the host sends, and a row that differs only in a plain chat's thread is named (#124). A gateway sends its session key (`agent:main:<platform>:<chat type>:<chat>`) on every session, while the rows installers and operators write leave it empty; matched exactly, every WeChat, Feishu and Desktop-login route failed closed with `audience_unmapped`, writes stopped for as long as nobody noticed, and `doctor` stayed healthy because the CLI route works. The key is built from the platform, chat type and chat, which a row still matches exactly with its user, thread and workspace, so an empty key now means the row does not pin one; a row that names a key matches only that key. A plain chat's thread is not relaxed: the host sends an empty `thread_id`, rows copied from the CLI's `main` stay a different route, as the audience isolation tests require, and such a near miss is reported as `capability_gap:audience_thread_mismatch:row_says_main` for the operator to correct. `docs/install.md` says how a gateway row is written.

### Tool output is kept, not turned into facts

- A tool output is kept, lexically indexed and embedded, but no longer consolidated into claims: it is not a derivation root any more (`DERIVATION_ROOT_ORIGINS`), and admission queues it an embedding only (`wanted_work_types`, shared by capture, the deferred refill and on-demand scheduling). What an agent read or ran is not what it should remember. On the pilot one 2.5-hour task left 787 claims derived from its tool output -- file sizes, paths, ports, creation times -- and 2,946 of the store's 3,175 claims rested on tool output alone; of the owner's 30 real questions, none was answered by one, and hiding all of them lost none of the 30 (and freed a slot that found one more). All 53 consolidations that failed validation that day were of tool output, as was 86% of the consolidation work. How a task was done is distilled by the host into skills; the output itself is still found by its words and by meaning. The cost, measured on the same copy: asked a question worded exactly like one of those claims, recall found the answer 38 times in 40 with them and 12 without. A consolidation queued before the change finishes without a model call, and a deferred tool output settles on its next refill.
- `retire-rootless-claims` retires, page by page and only with `--apply`, the proposed claims no derivation root supports: those an earlier release derived from tool output, and the few resting on an agent's reply or recall output alone. Each gets a retracted version (`no_derivation_root`) and stops waiting for evaluation; its sources and earlier versions stay, and active or disputed claims are not touched. It is separate from `requalify`, which would also have moved 154 claims for every other rule changed since they were written, promotions included. The report names refs and verdicts, never claim text. On the pilot's store the preview lists 2,785 of 2,978 proposed claims.

### What three agents on one store turned up

- Recall searches every scope an entry may read, in one vector request. It searched them one partition at a time in sorted order until its budget ran out, about 150 ms a partition, so an entry holding 110 scopes searched seven of them and one holding 119 searched sixteen; the owner's own scope sorted 105th and 117th and was never searched by meaning. On the pilot the owner told one agent their cat's name and asked two others: the one holding six scopes ranked it first by meaning (0.807; that recall was then emptied for the reason in the next entry), the one holding 110 never saw it and found it only by searching again by words. A store filters by the whole list of trusted partition literals before it ranks (`search_scopes`), so another partition's nearer rows never crowd out the entry's own; a store without it is asked one partition at a time as before.
- A recall, a view and a release are withdrawn only when a deletion or suppression in the reader's own scopes came after they were read, not whenever the memory epoch moved. Every capture moves the epoch, and a recall packet, the hosts' delivery fence and `release_objects` each emptied or refused whatever was read one capture ago. With three entries and a worker writing one store that was most recalls: on the pilot an agent was asked about what the owner had just told another one while the worker was writing it down, and its automatic recall came back empty. Deletions record the epoch they moved the store to, so one recorded after a read is found exactly (`retraction_after`, which the worker's derivation fence already used, now in `core/delete_storage.py` for all of them); every other change is still caught object by object by the fresh reads the release makes. A test that pinned the old behaviour asserted an empty packet for a query that never found anything; it now fails if nothing is found.
- Naming a thing for the first time is a fact, not an alias. Told "我的猫咪叫年糕" with nothing yet known about the cat, the consolidation model filed "年糕" as an alias whose target was the message itself; an alias is held back until its link to an existing fact is proved, so this one never could be, and no agent recalled the name. An alias whose target is no fact, stated first-hand by a verified person in a literal naming form ("…叫…", "…名叫…", "…的名字是…", "… is called …"), is re-framed from the quote's own words (subject "我的猫咪", predicate "叫", value "年糕") and qualified like any fact (`name_frame`, beside the other literal frame repairs). An alias of a known fact, or any other wording, is untouched. After upgrading, `repair-claim-frames` re-frames the aliases an earlier release stored.
- Hermes' own memory tools, `session_search` and `memory`, are captured as memory re-injection, like Scope Recall's own tools: kept as sources and found by their words, but never consolidated, used as candidate evidence or embedded. Captured as tool observations, their output was taken for news: on the pilot an agent searched its past sessions, got a page of unrelated old conversation back, and consolidation made six new facts of it that then took the places of the next automatic recall.
- Claims are embedded the way sources are: a group's claims go a hundred to a request and into one commit. Every claim was its own request, asked after the one before, at about two seconds each, so the 2,000 claims the pilot's import queued took over an hour while the provider allows thousands of requests a minute. The text and its encoding are unchanged, so a claim gets the same vector either way.
- The members a group's commit wrote are recorded together, up to 200 to a write transaction, instead of three transactions each. Measured on the pilot's rebuild: a pass of 500 embeddings took 33 seconds, almost all of it that bookkeeping, and a pass of 1,000 outlived its 60-second lease, so 227 vectors already paid for were dropped as stale and embedded again.
- A group the provider refuses for capacity (429, 502, 503, 504) or budget goes back whole without spending an attempt, and the pass asks for no more embeddings; a group whose request fails for what it carried still falls back to one request per member. On the pilot one refused request of a group of 500 made every member ask for itself: the pass spent its two minutes on 67 of them and the rest of the group's leases ran out.
- A group whose request fails for what it carried is asked again in halves before any member asks alone. One text the request guard will not send -- a message holding something shaped like a key -- failed the whole request, and the pilot's rebuild met one in a group of sixteen: all sixteen then asked for themselves. In a group of five hundred that is the lease spent on a few dozen of them and the same text met again the next pass; halving costs a few requests and leaves that text failing alone.
- A group member that still has to ask for itself is handed back unspent once less than five seconds of its lease would be left, and its request is bounded by the lease as well as the pass. Members are claimed together, and those asked after the lease had run out were dropped as stale with their attempt spent.
- Refusals that started within a second of each other count as one when a refused provider's pause is worked out. The four concurrent requests of one group, refused together, counted as four refusals in a row and paused embedding for eight minutes instead of one.
- `import-entry` no longer queues embeddings that vector retention would expire at once: a tool output's summary left for an output the capture filter withheld, and a repeat of an earlier tool output in the same scope. The intake gate keeps both as sources only, but stores from earlier releases embedded them, and their embed history queued them again: the pilot's import put 12,953 of them in front of the shared worker, 42% of its rebuild, each embedded and deleted by retention within the hour. They are now recorded as expired under retention's own reason (`omitted`, `repeat`), and the receipt counts them (`embeddings_retention_would_expire`); the text and everything drawn from it stay, found by their words. Retention and the import read the same two conditions.
- Editing the runtime config no longer fails the running supervisor: it takes up the new values before its next pass, as each pass, its own process, already did. Only a file that names another store ends it, as `suspended` with the reason `config_changed`. Each edit used to read as `supervisor_failed` to the doctor and the patrol and left the store without a supervisor until the next scheduled wake, up to five minutes later; the pilot's rebuild met that four times in one morning.

### Imported ids, and a queue counted once

- `import-entry` renames a source id wherever the old store names it, whatever the id's form. 3.2.0rc3 renamed the pilot's `event-legacy-<32 hex>` sources (85,112 of 87,033) in every column, but not inside JSON or in the work it queued: 28,155 embeddings named no source, and the shared worker dropped each as it came to it, while 195 JSON fields kept an old id. A token is now renamed only when it is exactly an id the old store holds, so a word shaped like an id is left alone. The pilot's store was repaired in place from the untouched old stores (kit `tools\repair_import_rc3.py`: 28,286 embeddings queued again, 184 fields rewritten, the other 11 already named sources their own store no longer had).
- The memory skill says what a shared memory is: one store that each agent reads through its own chats, not one store per agent. Asked about it, an agent had described several stores sharing parts of themselves.
- A worker pass counts the queue once for all its scopes before refilling deferred captures, instead of once per scope and work type. A shared store's worker binds every entry's scopes (221 on the pilot); with 32,000 embeddings queued after the import the 442 counts took 90 s of a 120 s pass on a copy and longer live, so the watchdog ended every pass before it embedded anything and the rebuild stood still from 09:28Z. The same pass now takes 7.6 s.

### Times in the host's zone, and an agent's memories brought along

- A memory's time reaches the model in the zone its host tells it it is in, offset included: `2026-09-23T02:52:03-04:00`, not `2026-09-23T06:52:03Z`. On the pilot the owner asked one agent when another had been told something, and it answered "a little after 6:50 in the morning" while the computer's clock read 2:54: Hermes gives its model the date and its configured zone but not the hour, and the model read the UTC time as its own. Hermes uses the zone its own prompt names (`timezone` in its config, else the machine's); Codex uses the machine's. This covers the automatic injection and every tool reply. Storage, the contracts and everything compared stay in UTC, and what a memory says is never rewritten.
- A model may write a time back the way it was shown one. `as_of` in a recall and `valid_from` in a revision accept an explicit offset and are read as the same UTC instant, as consolidation output already was; a date alone, a time without an offset and an impossible offset are still refused, naming the field.
- `import-entry` brings an agent's own store, moved aside at `attach`, into the shared store as that entry's memories. The pilot started its store empty on the plan's word that the three agents' memories did not matter; the owner said the next morning that they do. The old store is read-only and copied in one transaction, marked with the entry. Stores migrated from 2.x gave 13,073 different messages the same ids across the pilot's three stores, so every imported source gets an id of its own and every reference to it, in columns and in JSON, follows; keys and sessions take the entry's prefix, local integer ids are renumbered, and the deletion blocks of whole conversations are recomputed under the store's id, so what was forgotten stays forgotten. A fact whose slot is already filled is left out with its candidates and named in the receipt (three unconfirmed procedures on the pilot). No vector is copied; the embeddings the old store had, less those its retention expired, are queued for the shared worker. Rehearsed on copies of the pilot's stores: 87,033 sources in 91 seconds, integrity and every reference intact, every source marked, and the same sampled questions found as many of each agent's own messages as its old store did.
- `doctor` judges each running process of a shared store by the package it loaded. Every entry's host and the shared worker leave their record in the store's one directory, each from its own environment, and the doctor compared them all with its own package: once one entry was upgraded, its doctor called another entry's running host stale, and would have gone on saying so until that host restarted, with the daily patrol paging the owner about it. Found while planning this release's rollout; the pilot missed it only because nobody spoke to an entry before the last one attached. A stale process now names the folder it loaded from.

### The spend ledgers `attach` names

- `attach` creates the spend ledger each runtime config it writes names, the entry's and the shared worker's. A ledger is only ever made on purpose and every model request reserves in it first, so with 3.2.0rc1 the shared worker refused every embedding and consolidation with `ledger_not_initialized`, and an entry had no query vectors. Found by reading the runtime before the pilot, not on an instance. `detach` moves the entry's ledger out with its receipt, whole, so the entry's folder is left empty and its spend record is kept. rc1 is not the pilot's candidate.

### One store, many entries

The owner decided on 2026-09-22 that every agent should read and write one memory store, each marked with the agent it came in through, and that moving to a new machine should mean moving one folder. This is the store, the Hermes side and the operator commands; how to use them is [docs/shared-store.md](https://github.com/410979729/scope-recall-hermes/blob/v3.2.0/docs/shared-store.md).

- Schema 1110. A store records its kind, `local` or `shared`, and every source records its entry. Both columns take a constant default, so a 1109 store upgrades without a single row being rewritten, and every existing row says `local`. SQLite still reads every source row as it adds the column, about 3 s a gigabyte (corrected by the release audit; this entry first said the step cost the same on any size). A new `entries` table holds the name a reader is shown for each entry.
- A shared store is its fixed id, not its directory. Copied elsewhere it opens nowhere, and says `store_moved:run_adopt`; `adopt` checks everything an open checks except the directory, then records the new one. An entry binds a subset of the store's scopes, and the store grows as entries attach, never past the scopes one shared binding can carry, so the shared worker that binds every scope can always be built. That bound is 1024 for a shared binding: our three pilot instances bring 221 distinct scopes between them and all five 369, mostly one per agent-to-agent conversation. A local binding keeps its 128.
- In a shared store every source names its entry. One that arrives without an entry, or with an entry the store never registered, is refused rather than filed under someone else's name; a local store refuses a source that names one.
- A capture that waited in the inbox keeps the entry that made it. The inbox replays with whoever replays it, the shared worker or another entry, and a busy shared store sends more captures through the inbox, not fewer. The entry now travels in the queued payload, only in a shared store, so a local store's queued captures are byte-for-byte what they were.
- Recall items in a shared store carry `entries`: the entry a source came in through, or every entry behind a claim's or an episode's evidence, once each. A local store's recall output is unchanged.
- A Hermes home that holds `scope-recall/attachment.json` binds as an entry of the shared store it points to; any other home binds exactly as before. The store's own `installation.json` keeps every entry's grants, each as that entry's installation had them, so every chat reaches what it reached before, and the owner's chats meet in the scopes the installations already share. Because an entry's binding is its own scopes, another entry attaching changes nothing for one that is running, which re-reads the manifest at every session switch.
- The entry is part of what identifies a capture: its source key, and the session id it is stored under (`<entry>:<host session>`). The owner talking to two bots gives both the same session and turn ids. Hooks are still matched by the host's own session id; only what reaches storage carries the entry.
- An assistant's and a host's source principal include the entry; the owner is one person whichever entry is spoken to.
- A capture that waited in the inbox is re-checked against the grants of the entry that made it, whether the shared worker replays it or another entry does.
- An entry never starts a worker: a shared store has one, with its own credentials, and an entry's process carries the entry's. An entry reads its model routes from its own `runtime-config.json` beside its pointer.
- When an automatic recall brings back something another entry was told, the injected guidance says which entry is reading and that an item from another entry is that agent's experience. Nothing is added when every item is the reader's own.
- New operator commands: `init-shared` makes a store; `attach` makes a Hermes home an entry, carrying over the audience rows and owner principals of the home's own installation (`--grants-from`, its `installation.json` moved aside) and binding its model routes to the store (`--runtime-config-from`); `detach` removes a home's pointer and keeps its record and memories; `adopt` records a copied store's new directory; `entries` lists who is attached and when each was last heard from. Each write keeps a copy of every file it replaces and a receipt under the store's `receipts\`.
- The first entry attached with routes gives the shared worker its `runtime-config.json`; later entries widen its scopes and keep its routes. An entry whose embedding model differs from the worker's is refused (`embedding_space_differs`): its query vectors would search a directory the worker never fills.
- `plan-install`, `apply-install` and `doctor` recognize an attached home. The doctor names the store and the entry, and accepts the store's worker, which binds every entry's scopes, as the home's. An attached home is never purged from its home; `detach` it instead.
- After a move, an entry whose old home no longer points at the store can be attached from a new home under the same id.
- A writer waits its turn for the truth writer lease, within its deadline, instead of failing at once. Writers in separate processes take turns one transaction long; with three entries and the worker writing one store, one capture in ten failed on a turn that ended milliseconds later and was kept only in the host's memory until its next retry. The durable inbox could not catch those, since queueing a capture is a write under the same lease. A single host and its worker met the same, more rarely.

### Maintenance, written down

- `AGENTS.md` says what changes this plugin accepts from 3.1.2 on: a bug with a reproduction, a security fix, a change a host made that the plugin has to follow. Anything else needs the owner's decision before any code, and an open-ended "what else could be improved" is not a task. It also records two things the 3.1.1 and 3.1.2 rollouts taught: a temporary setting on a running install is undone in the same piece of work that made it, and the first commit after a tag moves the version past it. The line about where wheels are built from named a release branch and `v3.1.0rcN` tags; releases are cut from `main` at `v<major>.<minor>.<patch>`. No code changes.

## [3.1.2] - 2026-09-21

3.1.2 is three things that turned up on the day 3.1.1 went out, two of them while rolling 3.1.1 onto our own instances. What is remembered and how it is asked for do not change, and there is no schema step.

**Upgrading from 3.1.1 or 3.1.0.** Install the package and restart the host. Re-run `apply-install` as well if you want the new skill written into the host; nothing else needs it.

- **Work could be failed without ever having been tried.** A pass that ran out of time kept the attempt of every item it had claimed and never looked at. At the default `max_items` of 32 a group fits a pass and it does not show; an instance whose `max_items` had been raised for a drain failed 888 embeddings in an hour that were never sent to the provider. If you ever raised `max_items`, put it back, and see the entry below for the rows already failed.
- **On Windows an upgrade could be refused by a process nobody could see, and the refusal did not say why.** A host that is shutting down launches one last detached wake, which used to read the operator pause only after its start delay and meanwhile held the package folder. `package-upgrade` now prints the reason of a refusal, and that wake leaves at once.
- **A second skill, `scope-recall-memory`,** tells an agent how to answer what an owner asks about their own memory (what do you remember about me, did I say that, is it still true, change it, stop bringing it up, delete it), and says what a deletion takes with it before it asks for the confirmation. The `revise` and `forget` tool descriptions say the same in two sentences.

As with 3.1.1, this release ships without the P18 formal acceptance receipt; the figures here are our own measurements.

The entries below are the changes as they were written when each landed.

### The questions a person asks about their own memory

- A second skill ships with every install, `scope-recall-memory`, and the two write tools say what they do. The capabilities were all there (`profile`, `recall`, `inspect`, `trace`, `revise`, `forget`), but nothing told an agent how to turn them into answers to the four things an owner asks: what do you remember about me, did I say that or did you work it out, is it still true, and what happens if I change it, stop you bringing it up, or delete it. The tool descriptions read "Apply a Core-authorized suppress or delete request", which says nothing about the difference a person cares about, and the core's rules for allowing either were discoverable only by being refused. The skill maps each question to the fields that answer it (`basis` and `origin` for who said it, `temporal_status` and `claim_state` for whether it holds, `evidence_refs` and `inspect` for the sentence it came from), and for the three changes it says what the core will ask of the user's own latest message: the wording it accepts, that the message must name the item, that a negated, hypothetical or quoted request does not count, and what each refusal code means. It is explicit about deletion, because the minimum unit is the whole source message: deleting one fact erases that message and every other fact taken from it, cannot be undone, and does not reach the chat app's history, the host's transcripts or an operator's backup; the agent is told to say so, and what else will go, before it asks for the confirmation. It is as explicit that muting has no tool that undoes it, and that there is no "don't record this" switch: a message is captured before the model sees it, so the honest offer is to delete it afterwards. `revise` and `forget` now carry a two-sentence description of the same facts on both hosts, since a model reads a tool's description whether or not it ever loads a skill. No runtime behaviour changes. `tests/packaging/test_memory_skill.py` checks every phrase the skill quotes against the core's own patterns, so the page cannot drift from what is enforced.

### An upgrade refused by a wake nobody could see

- A supervisor that starts under an operator pause leaves at once, and one that is paused while it sleeps its start delay leaves within five seconds. It used to read the pause only after the delay. Found rolling 3.1.1 onto a live instance: the pause was set and every supervisor had left, the gateway was stopped, and on its way out the gateway launched one last detached wake, as designed, with the usual start delay (`worker_min_interval_seconds`, 120 s there). That process runs from the package folder, so for two minutes a sleeping supervisor held the folder the upgrade was about to replace, and because the gateway runs elevated its children did not appear in the operator's process listing. The package step refused before touching anything, which is what its Windows check is for; the instance went back up on the old build and the second attempt, which waited for the folder, went through. The delay is now slept in steps of `PAUSE_POLL_SECONDS` (5 s) that read the pause, and is kept in full when nothing is paused.
- `package-upgrade` says why it refused. A refusal printed `{"state": "blocked", "error_type": "PackageUpgradeError"}` and nothing else, so the case above, whose remedy is to wait a moment and run the step again, read the same as a wrong wheel or a missing `uv`. Every reason the step raises itself is a fixed code, and that code is now printed as `reason`; for `installed_files_locked_or_not_replaceable` a `next_action` says that nothing was changed and what to wait for. A message that could carry a path (an interpreter that does not exist, an operating-system error) is still reduced to its type. `maintenance/AGENT_WORKFLOW.md` section 1.1 says where the last wake comes from and how long it can take to leave.

### Work that was failed without ever being tried

- A pass that runs out of time hands back what it claimed and never touched, with the attempt unspent. Every claim spends one of an item's three attempts, and a pass claims its embedding group in one page of up to `max_items`. The worker already returned the rest of a group unspent when the group was cut short, except when the reason was that the pass had no time left, which is the usual reason: `_release_group` returned at once if the budget was gone, the untouched items stayed leased until their lease ran out, and the attempt stayed spent. Three such passes and an item was failed as `lease_exhausted` without having been looked at once; the automatic recovery then re-opened it into the same oversized group. Found on a live instance whose `max_items` had been raised to 1000 for a one-off drain and never put back: 888 embeddings ended `auto_retry:4|lease_exhausted|lease_exhausted` within an hour, at seven attempts each, while the provider was answering (554 embedding requests answered 200 that morning, nine met a network error). The hand-back now gets a second of its own (`RELEASE_SECONDS`), inside the margin the runtime keeps behind a drain and the watchdog's grace. With the default `max_items` of 32 a group fits a pass and none of this shows. An instance that was hit needs `max_items` put back and one `retry-failures --apply` per 256 rows; the rows are auto-recoverable in kind but have spent their automatic recoveries. `docs/configuration.md` gave the bounds of `max_items` as 1 to 32; they have been 1 to 1000 since 3.1.0, and the entry now says what a large value does and that it is for a drain, not for keeps.

## [3.1.1] - 2026-09-21

3.1.1 is what running 3.1.0 on real instances, ours and yours, turned up, fixed. What is remembered and how it is asked for do not change, and there is no new concept to learn.

**Upgrading from 3.1.0.** Install the package and restart the host. The store is brought forward by its first ordinary open (schema 1108 to 1109, WAL mode, a smaller lexical index); a store above 100 MB leaves that step to a caller with the time for it, which is the worker's next pass, `apply-install`, or `scope-recall upgrade-store --host <host> --instance-root <home> --backup-dir <dir>`, which does it now with a verified snapshot first. 3.1.0 cannot open a 1109 store, so keep that snapshot for as long as going back is a possibility. Nothing else needs an operator.

**Coming from 2.0.1.** Section 9 of the 3.1.0 notes in [CHANGELOG.md](https://github.com/410979729/scope-recall-hermes/blob/main/CHANGELOG.md) is still the procedure. One thing it did not say: if you talk to Hermes through the Desktop app or `hermes --tui`, 3.1.0 cannot serve you at all, and from 3.1.1 you add `--local-platform desktop` (or `tui`) when you install.

What mattered most, in the order it hurt:

- **Hermes Desktop and `hermes --tui` had no memory on 3.1.0.** Those surfaces name no user without a dashboard login, and 3.x refused every such session. The installer now approves them per installation (`--local-platform`, [#94](https://github.com/410979729/scope-recall-hermes/issues/94)).
- **On Linux and macOS the LanceDB index stayed empty, and a forget never finished.** Off Windows the vector store runs in-process, and that store lacked the one write the worker publishes through, so semantic recall never became available there; its purge refused the keyword the runtime passes, so a forget's vector layer was never acknowledged ([#99](https://github.com/410979729/scope-recall-hermes/issues/99), found and patched in production by panxuewen0101). Both work now, and the SQLite companion can be purged too.
- **Kimi / Moonshot refused every chat** once the plugin's tools were in the request, because one tool parameter had no explicit type ([#89](https://github.com/410979729/scope-recall-hermes/issues/89)). Every property in every contract now declares its type.
- **`pip install -U` left the plugin refusing everything** until someone re-ran the installer, with nothing saying so. The store now upgrades itself.
- **The request guard mistook a line break for a secret** and refused more than a hundred ordinary candidate evaluations a day on one instance (`sensitive_request`). Fixed at both gates; `retry-failures --include-terminal` re-opens what it refused.
- **A busy instance grew without bound.** Tool outputs that repeat an earlier one byte for byte, or that are only a "output omitted" line, are kept as sources but no longer embedded or consolidated; vectors already made for them expire; the lexical index is a fifth of its size; the store runs in WAL mode so a reader no longer fails the writer.
- **Real questions were answered worse for a few days of this cycle**, by a side effect of the change just above, and that is repaired: on our own questions the reply that had answered each is in the top five for 28 of 30 again, as on 3.1.0.
- Voyage embeddings ([#88](https://github.com/410979729/scope-recall-hermes/issues/88)), a consolidation route that speaks the Responses API ([#90](https://github.com/410979729/scope-recall-hermes/pull/90)), and a candidate's evaluation window anchored on its saved quote ([#91](https://github.com/410979729/scope-recall-hermes/pull/91)) came from people who use this. Thank you.

A scheduled (`cron`) Hermes job runs without this memory, as it has since 3.0, and that is now stated rather than discovered: nobody is speaking in a scheduled run, so there is no audience to bind it to.

As with 3.1.0, this release ships without the P18 formal acceptance receipt; the figures here are our own measurements.

The entries below are the changes as they were written when each landed.

### The upgrade applies itself, and a refusal that was never a secret

- A store at an older known schema is brought forward by its first ordinary open. `pip install -U` used to leave every capture, recall and worker pass failing with `SCHEMA_UNSUPPORTED` until someone re-ran the installer, with nothing saying so. The first transaction that opens such a store now runs the same identity-verified, single-transaction upgrade the installer runs; `doctor` reports `schema_upgrade_pending` until then and never applies it itself.
- Hermes Desktop and `hermes --tui` can use their owner's memory again: `apply-install --local-platform desktop` (or `tui`) ([#94](https://github.com/410979729/scope-recall-hermes/issues/94), reported by tutan0558; the host gap is [#41](https://github.com/410979729/scope-recall-hermes/issues/41)). Both surfaces reach the adapter as platform `desktop` or `tui`, and the host passes a dashboard login as `user_id` there and nothing when nobody logged in, which is the ordinary case for a local profile. 2.x minted a Desktop principal for that case (`desktop_principal.py`, 1.9.1). The 3.x identity layer binds only what the installation manifest approves and accepted a session that names no user on the CLI alone, so every Desktop and TUI session failed with `user principal required for non-cli platform`, the provider never initialised, and neither the 3.1.0 notes nor the migration guide said so. The approval is now the installer's, per installation and per surface: the flag adds the owner principal `(desktop, local)` and one grant of the owner's private scope on that route to `installation.json`, on a fresh install or in place on an existing one (the previous manifest is kept under `.scope-recall-backups`, a failed install restores it, and the scopes, the installation id and the store are unchanged), and a session there that names no user then binds as the local owner, the way the CLI's does, with the memory the CLI has. Nothing is minted, no manifest field is added, and an older package still reads the file. It stays an explicit choice because only the owner knows whether everyone who can reach that surface without logging in is the owner; without it the refusal now names the flag. A session that carries a login is a named user like any gateway user and is not covered. `cron` is not a local surface and cannot be made one: nobody is speaking in a scheduled run, a job can be created from any chat, and its prompt would be captured as the owner's own words. A scheduled job therefore runs without this memory, as it has since 3.0; that is a boundary, not a fault.
- An object reached by relation no longer outranks a direct hit, which repairs a loss of recall this release's own lineage change had caused. Expansion scored every related object by its place in its own seed's list, so the first object related to the thirteenth hit scored 1/62, above every direct hit but the first. It did no harm while an episode's lineage was copied onto each of its revisions: the rows of one episode at dozens of old revisions spent the relation bound of 24 without becoming candidates, and about one related object per query got through. Writing that lineage once (above) freed the bound. Measured on one instance's real questions, where a case passes when the reply that answered the question is in the top five: 28 of 30 on 3.1.0, 20 of 30 after the lineage change, with fact recall (60/60), no-match (20/20), supersession (2/2) and question recall (58/60) unchanged, which is why nothing else noticed. The last good code on a store where only the lineage rows had been removed also gives 20, so the cause is the freed bound and not the new readers; a traced case went from one related candidate to sixteen, and its six packet slots from the expected reply first to six imported events. A related object is now held to the smaller of its own score and its seed's, times `RELATION_WEIGHT` (0.5), so with the default pool it sorts after every direct hit; a turn's replies are not weighed. That is 28 of 30 again on the same stores with the other four figures unchanged. It is 3.1.0's behaviour by rule, and it costs what the accident had gained: on a second instance the freed bound had answered one question of 25 that 3.1.0 missed (19 where 3.1.0 had 18), and that one is given back. A store already upgraded needs nothing but the new package.
- The secret guard no longer refuses a request for a line break, and this entry corrects an earlier one that said so too soon. Serialised into a model request, a source's line breaks become the two characters `\n`, which are not whitespace, so a document template with an empty credential slot had the next line swallowed as its "value" and the request was refused as `sensitive_request`. The first repair taught the scanner to read an escaped break as a break and was verified by calling the scanner on a string. The adapter applies the guard twice, to each message content and then to the whole serialised body, where a content is escaped once more and the break reads `\\n`; the scanner's rule restored the break and left one backslash behind, and the empty slot took that as its value. Measured with one instance's installed code on all 493 evaluations it had refused: the contents gate passed every one and the body gate refused every one, none holding a secret, and the repaired build went on refusing about 125 a day. Each text is now scanned once, in the form it was written in: the contents gate is unchanged, and the body gate scans the same body with the contents blanked, so the model, the route's fields and the message roles are still covered. A break escaped more than once is also read as a break, for a tool output that is itself JSON holding JSON. The same 493 pass both gates with this change; the tests go through `propose` on both consolidation routes and fail on the previous code. `sensitive_request` stays terminal and is never retried; after upgrading, `retry-failures --include-terminal` re-opens the rows earlier builds left behind.
- The migration tier upgrades a store written by the previous release's own code (`git archive v3.1.0`), not a fresh store downgraded by hand, which is how a wrong version stamp in the 1107 step went unnoticed.
- The 1109 upgrade also moves the migrated `scope_authorization` records out of every source row into `authorization_payloads` and `source_authorizations`: the 2.0 conversion wrote the same 600-byte record into 167,000 sources on one instance, 102 MB for 97 distinct payloads. The record is kept once per distinct payload and linked (`Transaction.source_authorization`); the conversion writes the compact layout from the start.
- Two kinds of tool output are kept as sources only at capture: one that repeats an earlier tool output in the same scope byte for byte (`tool_output_repeat`; the earlier copy carries the vector and the lexical index lists both), and the summary the capture filter leaves in place of an output it withheld (`tool_output_omitted`; the 2.0 release's form of it too). On one instance 77% of 132,000 tool outputs were exact repeats and almost all of the rest were such summaries, each embedded as a 12 KB vector. The retention pass deletes the vectors older releases gave both kinds at once, whatever their age, and records the reason in `expired_vectors`; the doctor reports the ledger by reason. A person saying the same thing again is never a repeat.
- The changelog and `docs/` are curated for a public reader: the forty 3.1.0 release-candidate entries and the internal closeout, assessment and test-walkthrough notes moved to `docs/implementation-history/`, so `[Unreleased]` holds only what is unreleased and `docs/` holds the guides and contracts. Nothing shipped in the wheel changed: the sdist lists its docs by name.
- The store runs in WAL mode. Under the rollback journal a two-second read left the writer with `database is locked` after its whole timeout, so any operator query could fail a worker pass, and a hook has six seconds. Under WAL readers and the writer coexist (a writer committed in 20 ms beside the same read). The mode is switched when a store is created or brought forward, and rechecked on every writable open; backups already write their snapshot in rollback mode, and a read-only open reads a WAL store in every file state, a stale `-wal` with no `-shm` included. The doctor reports `journal_mode`.
- The lexical index is a term dictionary and integer postings. `lexical_projection` stored every (term, source) pair as text and a mirror index doubled it: on one instance 5.2 million rows took 713 MB, half the store, for 258,000 distinct terms. A term is now one row in `lexical_terms` and a posting two integers in `lexical_postings` (`term_id`, `source_id`) with a reverse index: 135 MB for the same rows on that store, and a document-frequency lookup in 18 ms. Every source version now carries an integer identity (`source_events.source_id`), assigned at insert. The 1109 upgrade rebuilds the index from the old projection (95 s on that 1.4 GB store); on a store above 100 MB that step waits for a caller with the budget for it, the worker's pass or `apply-install`, so a hook's six seconds are never spent on it, and the store is switched to WAL before the upgrade so reads keep working while it runs.
- `upgrade-store` brings one store forward to this release's schema now, with a verified snapshot first and the budget no hook has, and leaves a store a running worker holds untouched. It is the caller a large store waits for when the operator installed the wheel and does not want to wait for the worker's pass.
- `upgrade-store` no longer refuses its own wait. It handed storage the time left until its deadline, and while the clock has not ticked since the deadline was built that is `(started + 30.0) - started`, which is 30.000000000000014 for some clock values; storage refuses a timeout above 30, so the command took its snapshot and answered `INPUT_INVALID / storage_timeout`. Before Python 3.13 `time.monotonic()` ticks every 15.6 ms on Windows, so the first reading is always the one the deadline was built from, and the rounding goes wrong for one clock value in four during the last 30 s below a power of two of uptime: a CI run on `main` failed this way while the pull request's run on the same commit was green. The wait is now held to what was asked for. A refused run had touched nothing, and a second run worked.
- The doctor says how many candidates it will evaluate, not how many carry a state. `pending_evaluation` is a lifecycle state, not a queue: a candidate whose evidence has settled and was already put to the evaluator keeps the state until new evidence arrives, and the sweep schedules nothing for it. On one live store that was 1,031 of 1,032 candidates, and `candidate_processing: pending 1032` read as a backlog that never drains. `candidate_settling` gains a fourth figure, `settled_nothing_to_ask`, beside `queued`, `collecting` and `settled_waiting_sweep`, and the line now reads `due=1,nothing_new_to_ask=1031,pending_evaluation=1032`. Nothing about scheduling changed.
- A status file is read and replaced beside another process without failing either one. On Windows a file cannot be opened for the instant `os.replace` swaps it in, and cannot be replaced while a reader holds it open; either side gets `PermissionError`. The supervisor reads its control file outside the control lock, beside the wake that rewrites it, so the refusal now and then ended a supervisor (the five-minute task starts another) and, where it was seen, a nightly CI run on `main`: `control.read()` raised `[Errno 13]` while the real supervisor process of `test_real_detached_supervisor_…` wrote the same file. Both sides try again for up to a fifth of a second; a file that is really forbidden still fails.
- A timed-out Codex CLI turn is ended through a job object. `taskkill /T` walks the process table through WMI and took 3.2 s on a host with 800 processes, past the adapter's 3 s bound, which reported the turn as `codex_start_failed` instead of `timeout` and left the CLI's process tree running; a job ends every descendant at once and needs no enumeration, and the taskkill fallback no longer replaces the real error with its own.
- An episode read judges its members in one query instead of loading each source, JSON and all: a 200-member episode cost 200 loads per read, on every listing. Once an episode has a resume, its gaps are those of what the resume cites, so an uncited member that changed no longer marks the resume stale, and the resume is delivered on its cited evidence: the packet refuses an object with more than 32 evidence refs, and an episode's evidence used to be every member, so a long episode's resume never reached a packet.
- Every property in every contract declares its type. Kimi Code's `k3` endpoint validates a tool's parameters as "moonshot flavored json schema" and refuses an enum or const with no explicit `type`; since every tool travels with every request, the trace tool's untyped `direction` enum failed all chat on that route after the 3.1.0 upgrade (#89, reported by JohnYinl). The forty-odd enum and const properties across the contracts now carry the type their values have, and two tests keep it so: one over the contract files, one over the tools a host receives. This is unrelated to a model's thinking mode.
- An `openai` embedding route may name the field that carries the width: `dimensions_field`, default `dimensions`. Voyage AI's `/v1/embeddings` is OpenAI-shaped in every other respect but calls it `output_dimension` and refuses `dimensions` outright, and a request that omits the width silently gets the model's default geometry (#88, reported by 849506054). A wire detail: the space digest does not move, and the response length is still checked.
- A candidate's evaluation window is anchored on the quote that was saved with it ([#91](https://github.com/410979729/scope-recall-hermes/pull/91), by JohnYinl). A long evidence source reaches the evaluator as a 3,000-character window, and the window was placed around the first occurrence of the candidate's value, which in a long tool output is often an unrelated earlier mention. The first saved quote that matches the source version verbatim is now the anchor, with the value and subject as the fallback they were. Measured on one instance before the change: for 85 of 1,791 long evidence sources that held the saved quote, the evaluator's window did not contain it, across 83 candidates of which 80 were still waiting.
- Pull requests run what they can break. The storage, capture, claims, deletion, episodes and retrieval tiers ran in no workflow, and the nightly integration baseline does not select their newest files, so the storage-growth and vector-retention tests had only ever run on a developer's machine; they now run on every pull request on both systems. One job per system runs the in-process tiers on Python 3.11, the interpreter the instances run on, where every job had been 3.12. `ruff check .` gates a pull request with the rule set `pyproject.toml` already declared (tests and probes stay out; five findings in shipped and gate code are fixed). A push to `main` runs CI: with `tags-ignore` as its only filter the push trigger had never fired for a branch, so a merge was unchecked until the nightly run.
- `SECURITY.md` names the supported line, 3.1.x. It still named 1.x. The 2.0.x line and everything before it are no longer maintained.

### Memory growth on a busy instance

- An episode's lineage rows are written once, at the revision the source entered. Every attach copied the previous revision's evidence links and object dependencies onto the new revision, so a 200-event segment held 20,100 link rows for 200 sources, and one instance wrote a hundred thousand such rows for eight episodes in a single day. Readers take every row at or below the revision they ask for, and relation expansion names an episode at its head. Schema 1108 becomes 1109; the upgrade keeps the earliest copy of each row and runs when the plugin is installed over the existing data directory, as the 1108 upgrade did.
- Tool-output vectors have a retention window. `vector.tool_output_retention_days` (default 180; `0` keeps everything) is the number of days a tool output's vector is kept after its source entered the store. A worker pass deletes the expired vectors, up to 2,000 per drain and hourly once caught up, records them in the new `expired_vectors` table so nothing counts them as missing or embeds them again, and the compaction that follows reclaims the space. The source text, its lexical index and everything derived from it stay: an expired tool output is still found by its words and through what cites it, never again by meaning alone. On a busy instance four in five captured sources were tool output, each with a 12 KB vector, while a few hundred of 170,000 sources ever became claim evidence.
- The doctor reports the footprint and the growth: `store_bytes`, `vector_bytes`, `sources_last_24h`, `sources_last_7d`, the `expired_vectors` count and the retention window under `index_metadata`, and, when `storage_budget_bytes` is set in `runtime-config.json`, the gap `storage_budget_exceeded` once the store and its vectors outgrow it. Nothing is deleted for the budget.

### A consolidation route can speak the Responses API

- A consolidation route may now name `"kind": "openai_responses"` and be reached through an endpoint that speaks the OpenAI Responses API instead of chat completions. It targets the documented DeepSeek `POST https://api.deepseek.com/responses` contract with `model: deepseek-flash`: one message list in, all `input` items in their original order (including system messages), `reasoning.effort`, `text.format`, `store: false`. Streaming is refused rather than half-implemented, and nothing is claimed for another provider or for OAuth. The route shares the existing budget ledger, HTTP transport, request deadline, response byte cap, usage settlement and proposal validator with the chat route -- a second dialect that quietly stopped metering would be worse than no second dialect, so the shared boundary is asserted rather than assumed. (#90, by JohnYinl.)
- Only a `completed` response holding an assistant `message` of `output_text` parts is an answer. `incomplete` is the same named `model_output_truncated` derivation the chat route raises for a length-limited finish, `failed`, a refusal part, a user or tool item in the output, and an empty answer are all failures rather than a proposal. Reasoning items are never answer text: `output_tokens` already counts the reasoning tokens, so they are not billed twice.
- A `usage` block is read as `input_tokens`/`output_tokens` with `input_tokens_details.cached_tokens`; a missing or malformed input/output token pair keeps the charge the ledger reserved, as before, and an HTTP 200 whose body is an error is still settled conservatively.


### Reports from Linux installs

- The in-process LanceDB store can be published to and purged, which is every LanceDB install off Windows ([#99](https://github.com/410979729/scope-recall-hermes/issues/99), reported by panxuewen0101 with the patch their production had been running). `build_vector_store` selects the helper-process store on Windows and the in-process `LanceVectorStore` everywhere else, and the runtime reaches either through two calls: `fenced_upsert_records(rows, guard=, remaining_seconds=)` to publish and `purge_governed_members(..., remaining_seconds=)` to forget. The in-process store had no `fenced_upsert_records`, so every publication failed with `STORAGE_UNAVAILABLE / fenced_upsert_unsupported`, the index stayed empty and semantic recall never became available, while SQLite truth and keyword recall went on working and hid it. Its `purge_governed_members` took `budget_seconds`, the name the Windows helper passes, and refused the purge port's `remaining_seconds` with a `TypeError` the port reads as "not purged", so a forget's purge work retried for good. Every fence and purge test drove the helper-process store, and the native tier runs on Windows only, so nothing on Linux ever made either call. The in-process store now does what the helper does, in the helper's order (native lock, guard, one merge), and takes the budget under either name. The SQLite companion, publishable since the entry below, had no purge at all and gets the same one from the same code: opaque identities only, a row that cannot be classified makes the inventory unknown, and an unknown inventory is never acknowledged as empty. `tests/contract/test_every_store_meets_the_runtime.py` asks every store class for both calls by signature, which runs on any platform, and drives both seams against the in-process stores on the Linux leg of CI.
- The `sqlite-bruteforce` companion can be published to. The worker writes every embedding through the fenced form of the index writer, and only the LanceDB driver implemented it, so on the documented no-extra fallback -- and on every host where LanceDB cannot load -- each embed item failed with a bare `storage_unavailable` and semantic recall never became available (#85, reported by 849506054). `SQLiteBruteForceVectorStore.fenced_upsert_records` evaluates the guard under the store's own lock and commits the group once. A port's refusal now also carries its field into `work_error_details`, so a `fenced_upsert_unsupported` is visible to the operator instead of collapsing to the code.
- An interpreter path is executed as given, never as resolved. On POSIX a venv's `bin/python` is a symlink to the base interpreter; the watchdog, the installer (hook and MCP launchers, receipts), the autostart control and the doctor's package probe all resolved it, so the worker started outside the venv and died with `ModuleNotFoundError` on every wake, and the only trace was `worker_process_failed` (#87, reported by 849506054). Validation still follows the link to check the chain. The last line of a child's traceback now reaches `runtime-worker-status.json` as `worker_error`, bounded and secret-screened, and the doctor reports it.

## [3.1.0] - 2026-09-18

We are sorry this took so long. The last release, 2.0.1, went out at the end of August, and it has been quiet here since, because we did not keep patching 2.0. We rebuilt the whole project. Production code went from 141,044 lines down to 48,289.

If you are on 2.0.1 today, read section 9 first. Your old memory database cannot be opened directly. It has to go through a migration, and there are a few places where that can go wrong, so we have written it out in detail.

---

### 1. What gets remembered

Here is a concrete example.

You tell the agent: "Let's use PostgreSQL for this project."

2.0 would decide right then whether that counted as a fact about you. If it decided yes, it stored it: this person uses PostgreSQL. Next time you started a different project, it would assume the same thing. But what you actually said only applied to that one project.

3.1.0 works differently. The sentence is stored word for word first, and nothing judges it yet. If you mention it again elsewhere, or something else supports it, only then does it get written down as a preference. If you change your mind later and say "actually, let's switch to SQLite", the new statement becomes the current version, and what you said before stays in the record where you can still look it up.

What decides it is whether the supporting passage itself is enough. When one source is enough on its own, that is all it takes. When one source falls just short, another independent source saying the same thing can carry it, and two sources are needed.

There are cases where adding sources does not help: the sentence is a question, it is hypothetical, it is repeating what somebody else said, or the thing it is about does not actually appear in the text. Those are not short on weight, they are the wrong kind.

If something new turns up later, the fact is judged again. If two records turn out to be about the same thing, they are merged into one.

You also do not have to ask for any of this. Relevant memory shows up in front of the model by itself. You do not search your own memory before answering somebody's question, and the agent should not have to either.

---

### 2. What a fact looks like now

This is what used to be stored:

```
The user likes black.
```

This is what is stored now:

```
Fact:     this person's visual preference is a black palette
From:     which sentence, in which design discussion
When:     when that sentence was said
Version:  which revision this is, and what the previous one said
```

When the information changes, the new version takes over from the old one, instead of leaving two records that contradict each other.

Two other kinds of thing get stored alongside facts.

One is the source. What you said, what the agent said, what a tool printed, documents you gave it — all kept as they were. Every fact knows which source it came out of.

The other is task history. It records how a whole piece of work went, not just how it ended: what the goal was, what was done along the way, how many times the plan changed, where it stands now, what to watch out for next time.

---

### 3. Making sure it has not remembered wrong

Every important memory keeps a line you can follow back: which source it came from, what the original words were, how many versions it has been through, what state it is in, whether it was deleted.

One more thing that matters: what the model itself says does not become a fact directly.

Before a fact is stored, the plugin checks whether the sentence it quotes really does appear in the source it claims. If that check fails, the fact is thrown away.

---

### 4. Six ways of looking, used together

It does not rely on any single kind of search:

- by exact reference
- by keyword
- by facts already confirmed
- by how recent something is
- by how things relate to each other
- by meaning, which is the vector search

What the vector search turns up are candidates, not memories you can use directly. Four more checks happen before anything reaches the agent: are the permissions right, is this the current version, has it been deleted, does it still hold as of now.

If the answer genuinely is not there, it says it does not know, rather than giving you something that sounds about right.

---

### 5. You can correct it, and you can really delete things

Over a long time the hard part is usually not remembering. It is what to do when something is wrong, and how to get rid of what you no longer want.

When you correct something, the new content becomes the current version and the old version stays in the record.

Deletion comes in two kinds. You can stop something appearing, or you can really remove it along with all the data attached to it. A deletion is a recorded operation, not a row quietly disappearing from a table. What you delete does not come back after the vector index is rebuilt.

Also, incoming content does not become permanent memory straight away. Sources, candidates and confirmed facts are three separate layers, so what the agent said itself does not automatically turn into a fact about you.

---

### 6. It is not only Hermes any more

2.0 was called Scope Recall for Hermes, and at the time it really could only work with Hermes.

Hermes and Codex both work now, reading and writing the same memory. Something you said in Hermes you can ask about in Codex.

The shape of it:

```
agent
  │
adapter
  │
Scope Recall core
  │
memory store
```

The DeepSeek harness is next, and we will keep adding after that. Supporting a new program now means writing an adapter, not changing anything in the memory layer.

If you normally have more than one agent tool on the go, this is probably the change in this release that affects you most.

---

### 7. It holds up when left running

Background processing moved out of the agent. In 2.0 it ran as threads inside the host program, so if the agent got stuck, memory processing stopped with it. Now it is a separate process, with a queue ceiling, leases, timeouts and a limited number of automatic recoveries, so it cannot pile up without bound. When the agent gets stuck, memory carries on.

The vector index can be deleted and rebuilt. Nothing is stored only inside the index any more. The text is in SQLite and the index just points at it. In 2.0 the index held its own copy of the text, so deleting it meant losing content. Now you can wipe the index, change the embedding model, or move to another machine, and after a rebuild nothing is missing.

Spending has its own ledger. Auxiliary model calls, token usage and charges are all recorded in it, so a long-running instance cannot spend an amount you never see.

Permissions cannot be changed by chat content. Identity comes from host authentication, the installation configuration and the scope mapping, not from a guess by the model. Nobody can get it to see something it should not by typing a sentence.

---

### 8. The model's tools went from about 37 down to 8

Half of 2.0's tools for the model were chores: remove duplicates, clean up, repair, purge, a set of playbook tools, and something that turned playbooks into skills automatically.

3.1.0 gives the model eight tools, all to do with recalling and correcting memory: `recall`, `trace`, `inspect`, `profile`, `entity`, `revise`, `forget`, `status`. The chores became commands you run yourself.

There is no "remember this" tool in that list. The model does not get to decide what should be stored. Storing happens automatically, facts form in the background afterwards, and the model can only recall, correct and delete.

That automatic skill generator had to go. We checked what it produced, and about half of it just restated a skill that already existed with nothing behind it. Also, a tool that lets the model empty its own memory store is a tool that can lose your memory.

There is a real downside: the model can no longer tidy up its own store. Removing duplicates and repairing things are yours to do.

---

### 9. Migrating a 2.0.1 memory database to 3.1.0

Please read this whole section before you start. The migration itself is safe, and your old database is never modified, but there are a few places where things easily go wrong, and the results are easy to misread afterwards.

#### 9.1 This is not an upgrade, it is a move

There is no in-place upgrade, and that is on purpose. The two schemas are too different. A silent automatic conversion is the kind of thing you only discover was wrong months later, and by then you no longer have a clean old database.

So migration is a separate, offline job that you can interrupt and continue. It reads from the old database and writes into a newly created empty instance. It never touches your old database, never goes online, and never calls a model.

The 3.1.0 plugin does not read the old format at all. The code that reads it exists only inside this one-time migration tool. Once you have migrated there is no way back. The old database stays where it is as your own backup, but the new one cannot be turned back into the old format.

#### 9.2 What you need before you start

First, where the old database file is. Usually `memory.sqlite3`. If you changed the configuration, go by the file your host configuration actually points at.

Second, an empty directory for the migration job. Job state, reports and receipts are written there. **Every fresh migration needs a new empty directory.** If the tool finds an existing report or receipt in there it refuses to run, saying it will not overwrite existing evidence. That is to stop you destroying the results of the previous attempt.

Third, enough disk space. The job keeps an isolated copy of your old database inside the job directory, so allow at least twice the size of the old database.

Fourth, time. How long it takes depends entirely on how much data you have, and we cannot promise a number here. If that matters to you, run it once on a copy first and see.

#### 9.3 Step one: stop the old database being written to, and get a clean file

**This is the step that most often goes wrong. Please read it carefully.**

Shut down the agent, or at least stop the memory plugin. Migrating from a database that is still being written to is refused outright.

But killing the process is not enough. In WAL mode, SQLite leaves `memory.sqlite3-wal` and `memory.sqlite3-shm` next to the database. If you just end the process, committed data may still be sitting in the WAL file and not yet written back into the main database. When that happens the migration tool refuses with:

```
offline source has a nonempty WAL or journal; use a consistent SQLite backup
```

This does not mean your data is damaged. It means this file cannot be used as offline input, because the tool will not risk missing committed content sitting in the WAL, and will not pretend that content is not there.

**The right thing to do is make a consistent copy yourself.** Either let the old plugin shut down properly and write the WAL back, or use SQLite's own command:

```
sqlite3 <old memory.sqlite3> "VACUUM INTO '<copy path>'"
```

The copy this produces has no WAL and is internally consistent, and can be used as migration input directly. Use that copy for every step from here on, not the original file.

Once you have the copy, **do not open the old database again, and do not let the agent start back up.** The reason is in 9.6.

#### 9.4 Step two: install 3.1.0 fresh

Install 3.1.0 the normal way for your host, and let the installer create an **empty** instance along with its installation manifest.

**Do not point the new plugin at the old directory.** The old and new instances are two separate things and cannot share a directory.

**If you use Hermes Desktop or `hermes --tui`, 3.1.0 itself cannot serve you, and these notes should have said so when it shipped.** Those surfaces name no user unless a dashboard login exists. 2.x minted a Desktop principal (`srdesk_…`) for that case; 3.1.0 refuses the session (`user principal required for non-cli platform`) and the provider never initialises, whatever you migrate. Use the first release after 3.1.0 and add `--local-platform desktop` (or `tui`) to `plan-install` and `apply-install` ([docs/install.md](docs/install.md)); in step three, map the old Desktop private scope to `owner_private`.

The migration tool reads the target directory, the agent identity and the installation identity out of that manifest, so you do not fill those values in by hand. For the same reason, the manifest has to be the one this new instance actually generated. It cannot be copied from another machine, and it cannot be hand-written.

#### 9.5 Step three: prepare the job

```
scope-recall migrate prepare \
    --source <the copy you made in 9.3> \
    --job <empty job directory> \
    --installation-manifest <installation.json> \
    --host hermes
```

`--host` is either `hermes` or `codex`, whichever host you are actually going to use.

This step changes no data. It reads the old database, takes inventory, and writes out a catalogue and the job state. It also computes and records digests of the source and the catalogue, which have to match later when you run it.

**About scope mapping.** If your old database has only one scope, you usually do not need to supply anything. Scope identifiers that match exactly are mapped across automatically.

If the old database has more than one scope, you need to supply a mapping file and point `--scope-map` at it:

```
scope-recall migrate prepare ... --scope-map <mapping.json>
```

The mapping file is a flat JSON object. Keys and values must all be strings. Each old scope maps to an audience the new instance **has actually been bound to**:

```json
{
  "old scope identifier": "audience name on the new instance",
  "another old scope": "another audience name"
}
```

There are three things the tool will always refuse to do rather than decide for you:

- It will not guess. If an old scope is left unmapped, it blocks.
- It will not merge two old scopes into the same audience. Write it that way and it blocks.
- It will not widen a permission it cannot read. If the old database has a permission meaning it cannot translate with confidence, it blocks and leaves you on the old installation rather than handing you something broader.

Please do not edit the metadata in the database to get the migration through. The consequence of that is a permission quietly widened, and the report will not tell you.

#### 9.6 Step four: run it

```
scope-recall migrate run --job <job directory> --source-quiesced
```

`--source-quiesced` is you telling the tool that the old database has stopped being written to. You do actually have to have done that.

While it runs, the tool first makes an isolated backup of the old database inside the job directory, keeping it as it was, and only then starts converting.

**There is an easy trap here:** between `prepare` and `run`, the old database file must not change. The tool recorded digests during `prepare` and checks them again during `run`. If they do not match, you get:

```
source or catalog digest does not match the current files
```

The usual reasons the digest changes: you started the agent once more after preparing, you opened the database by hand, or you used the original file instead of the copy from 9.3 and its WAL got written back. The cleanest way out of this error is to make a fresh copy, use a new job directory, and start again from `prepare`.

**An interruption is not a problem.** The job can be resumed and is idempotent. Power cut, manual interruption, machine restart — run the same job directory again and it picks up where it left off without producing duplicate data.

#### 9.7 Step five: check the result

```
scope-recall migrate verify --job <job directory>
scope-recall migrate status --job <job directory>
```

Please actually read the report rather than skipping past it. There are three things to confirm.

First, the status. If it says `blocked`, **that is a conclusion, not a failure you can retry your way past.** Your old database and the full backup are both still there, and the new database is explicitly in an unfinished state. **Do not start using the new instance until the cause is resolved.** An unfinished database will not pretend to be finished, and the installer will not treat it as migrated, but if you force your way into using it anyway you will get a memory store with content missing, and that is not easy to notice.

Second, what went into the archive. Some old data cannot be expressed in the new format without loss. That content goes into an archive rather than being approximated into something roughly similar. The report lists what. **Content in the archive does not mean the migration finished.** We would rather tell you a memory is in cold storage than quietly change what it means.

Third, spot-check some memories yourself. Pick a few things you remember clearly and see whether they are still right in the new database, including both the current statement and the older statements that were corrected.

#### 9.8 What gets blocked, specifically

These are the situations that make the migration report `blocked`. They are listed so that you know what the report is talking about when you see one:

- The old database has tables or columns the tool does not recognise. This usually means you are not on the publicly released 2.0.1 but on something you modified or an intermediate build. The old formats we publicly support are only the ones actually verified.
- The old database is missing a column the tool needs.
- A memory cannot find its source.
- A scope in the historical records cannot be resolved.
- A fact is marked as current but is not the latest version on record.
- A deletion record points at something that was not mapped across, or a deletion did not finish.
- The old database still has unfinished outbound work queued.
- Attachment metadata or contents cannot be carried across without loss.
- Aliases or reference relationships cannot be carried across without loss.

One more word on attachments: if an attachment file is no longer where it used to be, the report says it is missing. **It does not fabricate an attachment**, and it does not move the host's own original session files.

#### 9.9 Step six: queue the vector index

Only once everything above is in order:

```
scope-recall migrate queue-index --job <job directory>
```

This only queues the indexing work. It does not finish it on the spot. Embeddings are generated in the background, and how much that costs is bounded by your own budget.

**Until indexing finishes, searching by meaning does not work.** The other ways of looking all do: exact reference, keyword, confirmed facts, recency, and how things relate. Please expect this, rather than assuming semantic search works the moment migration ends.

On whether old vectors can be reused directly: only if the embedding model and version, the dimensions, the input encoding, the segmentation, the text hashes and the source mapping can all be verified as identical. If any one of those does not match or cannot be accounted for, the old vectors stay in the old snapshot and the new index is built from scratch. **Matching row counts do not prove matching content**, so we do not accept row counts as evidence.

Also, deleted content and everything that depends on it is handled before anything becomes readable or indexable. What you deleted in the old database does not reappear because of an import or an index rebuild.

#### 9.10 What to do when something goes wrong

The old database is always intact. That is the most important thing in this whole design. When a migration fails, no partial new data is written over your old database.

If you have already switched to the new database, used it for a while, and then want to go back to 2.0.1: **there is no path for that.** Sources and deletion records created after the switch live in the new database, and the old format cannot express them without loss. What we do in that case is keep the new database and stop dangerous writes, rather than copying the old snapshot back over the top and calling it a lossless rollback. So please take the checks in 9.7 seriously. That is where your real decision point is.

If you need to start over: leave the copy of the old database untouched, use a new empty job directory, and begin again from `prepare`. Do not reuse the previous job directory.

#### 9.11 A checklist

```
1. Shut down the agent and let the old plugin exit properly
2. sqlite3 <old db> "VACUUM INTO '<copy>'"       <- a consistent copy with no WAL
3. Install 3.1.0 fresh, get installation.json
4. scope-recall migrate prepare --source <copy> --job <new empty dir> \
       --installation-manifest <installation.json> --host hermes
   (add --scope-map <mapping.json> if there is more than one scope)
5. scope-recall migrate run --job <job dir> --source-quiesced
6. scope-recall migrate verify --job <job dir>
   scope-recall migrate status --job <job dir>
   <- only continue once the status is not blocked, you have read the
      archive list, and you have spot-checked some memories
7. scope-recall migrate queue-index --job <job dir>
8. Enable the new plugin per your host's instructions
```

`scope-recall setup --workflow` also prints the full built-in procedure, which you can follow along with.

---

### 10. About cost, please read this before turning the model routes on

3.1.0 spends money differently from 2.0, and this is where you are most likely to get caught out.

2.0 only paid to embed the memories it had already selected, a few thousand over an instance's entire life. 3.1.0 embeds everything that comes in. That is exactly why searching by meaning is genuinely useful now, instead of depending on whether a summary happened to mention the thing. But it means **your bill follows how much you talk, not how many memories you keep.**

Look at your usage in the first week, not at the end of the month. Every call is priced and recorded locally before it goes out, and `scope-recall doctor` shows the total.

We suggest MiniMax M3 as the model the plugin uses. Same forty jobs, two runs per model: its quality matched a well-known alternative, and the cost per call was clearly lower.

Turn the model's thinking mode off. We measured it: with it on, a pass took six times as long, three calls timed out, and three more came back with broken JSON. When you need the model to fill in a fixed format, having it think first makes things worse. It ships turned off. Unless you have tested it yourself, leave it off.

Prompt caching matters more than the per-token price. Our prompts repeat heavily, so a provider that caches them well can cost a third as much at the same advertised rate.

The daily work limit will not cap your spending. It limits how many queued jobs are attempted in a day, and it knows nothing about what any one of them costs. Set it below the rate you actually generate work and you do not save money, you just build a backlog that never clears. Spending is bounded by the ledger, not by this number.

Nothing is sent anywhere you have not configured.

---

### 11. What is not finished

We would rather write these down here than let you run into them.

#### Things 2.0 could do that 3.1.0 cannot yet

**There is no automated test for recall quality. This is the bad one.** 2.0 shipped 33 benchmark files, sets of questions with known correct answers. 3.1.0 has none. This is first on the list of what we have to put back.

The main cause of lost memories is tool output. When a source is something a tool printed — a file listing with line numbers down the left, an escaped API response — the model usually cannot copy a sentence out of it exactly, so the fact gets thrown away. The text is still stored and still searchable, it just does not become a fact. This is the first functional problem we are going to fix.

There is nowhere to sit down and read through your own memory. 2.0 had a browser interface and reports. Now there is only the command line. A review interface is planned.

It will not tell you why one result ranked above another. 2.0 had a tool for exactly that. 3.1.0 computes the information internally but does not show it to you.

The model cannot deliberately stop and reflect. 2.0 could. In 3.1.0 memories only form in the background. This mechanism needs redesigning rather than simply restoring, but the capability is missed.

Switching sessions does not trigger anything. 2.0 had two hooks for it and there is no equivalent now.

The chores are command-line only.

The vector index is not a real search index; it relies on LanceDB's defaults. At our data volumes this is fine, but a much larger store should have one properly built.

The task history layer is still thin. The mechanism works, but not much history has actually accumulated. The attachment tables are empty for now.

Automatic scheduling and our CI are Windows-only. The plugin itself runs on Linux and macOS, but you will start the background process yourself.

#### Things we removed on purpose and do not plan to bring back

The automatic skill generator, for the reason in section 8.

Chore tools the model could call itself.

`fact_evolution`'s auto-apply path. Versioned facts plus an explicit correction do the same job with far less machinery, and nothing rewrites itself.

Sixty-eight operations scripts, now ten commands. Most of the difference in code size was here.

The layer that reads the old 2.0 format is used once, during migration. It is not a long-term bridge, and there is no way back.

---

### 12. What this is good for

A long-running assistant, remembering your preferences, the way you like things done, and what you are working towards.

An agent that writes code, remembering the background of a project, why it was designed that way, what went wrong before, and how it was fixed.

A personal knowledge assistant, collecting the decisions, conclusions and experience scattered across many conversations into something you can look up.

---

### 13. What comes next

The first thing is putting the recall quality regression tests back. That is the biggest gap right now.

After that: fixing the problem where tool output cannot be quoted exactly, building a review interface, redesigning the mechanism for deliberate reflection, and making memory across long-running tasks more useful.

What we want is an agent that does not merely have context, but genuinely becomes better to work with over time.

---

### Thanks

Thank you to everyone who filed an issue against 2.0.1 and then waited. The reliability work in this release started from your reports.

Thank you also to the people who sent code: the embedder connection retry and backoff, the configurable retry delays, the vector admission floor, the word boundary in the secret-scanning pattern, and the MiniMax embedder this release now recommends.

The forty release candidates that led to 3.1.0 (rc2 to rc42) kept their own changelog entries; they are in [docs/implementation-history/3.1.0-release-candidates.md](docs/implementation-history/3.1.0-release-candidates.md).

## [2.0.1] - 2026-08-30

This patch is cumulative since the last public release, `2.0.0`. It completes the production managed upgrade path for ordinary users and hardens the 2.0 memory runtime: one fixed official stable source, an external resumable idempotent operation journal, strict state transitions, exact-Hermes-home restart control, zero-signal recall admission, candidate isolation, and explicit observability ownership.

### Added
- Added `hermes-scope-recall update --hermes-home <path>` and `hermes scope-recall update` as zero-choice stable update commands. Users do not supply a repository, URL, archive, candidate path, checksum, migration policy, vector policy, or rollback decision; rerunning the same command resumes the sole incomplete operation before any network request.
- Added a fixed-repository stable release stager with bounded HTTPS downloads, a strict release manifest, deterministic canonical tree identity, a custom link-free USTAR extractor, atomic reusable cache bundles, and content-free failures.
- Added `managed-upgrade` auto/prepare/worker/status/resume with a frozen external runner and a private activation handle under `<HERMES_HOME>/scope-recall/upgrades/operations/<id>`. Sealed plans, fsynced append-only transitions, OS locks, exact-home gateway identity, and bounded restart retries make power-loss and process-crash recovery idempotent.
- Added deterministic GitHub Release source/manifest production and exact PyPI asset separation. Stable update assets are checksum-verified but can never be mistaken for PyPI distributions.
- Added the H1 zero-signal query contract across Search, Context, and Prefetch. Opaque UUID/SHA/base64/high-entropy queries require an exact lexical identifier match, while vector-only candidates require positive semantic evidence, an absolute score floor, and separation from a real background neighbor.
- Added H2 candidate isolation metadata and maintenance evidence: Event Digest candidates retain explicit origin, lifecycle, automatic-admission, and review state; transport wrapper text is rejected again at the storage boundary; ordinary recall remains candidate-blind while explicit Profile/Review inspection remains available.
- Added O1 Fact adoption observability that separates feature enablement, claim/projection/evidence coverage, fact-owned memory coverage, shadow-backfill state, and last apply evidence without creating a new fact authority.
- Added O2 `curation owner` state for internal, external, and manual ownership, with distinct journal, legacy-nightly, and external-Hermes observations instead of conflating those execution chains.
- Added deterministic negative-retrieval and candidate-isolation evidence runners. The release checker executes current code, validates every scalar field, and requires an exact match to the frozen evidence rather than trusting `passed=true`.

### Changed
- Managed activation classifies Doctor checks explicitly: storage/config/runtime safety failures roll back, while memory-quality and rebuildable-companion debt remain visible maintenance advisories instead of asking an end user or a weak model to adjudicate memories during upgrade.
- Invalid, stale, or manifestless vector companion state is preserved as rebuildable debt and automatically disabled for activation without deleting companion files or sending memory content to an embedding service. SQLite truth and lexical recall remain available.

### Fixed
- Persisted the installer activation snapshot, plugin replacement phase, rollback capability, and commit result outside the replaceable plugin tree so a crash cannot turn a known transaction into a guessed restart.
- Refused symlink, junction, reparse-point, special-file, path-collision, oversized archive/tree, unsafe redirect, cache overlap, current-state drift, and ambiguous gateway/installer boundaries.
- Refused unrelated nearest-neighbor winners when no admissible query-side evidence exists, including random opaque input that previously returned the least-bad memory.
- Refused unreviewed Event Digest candidate promotion and transport-wrapper persistence without deleting or rewriting existing candidate debt.

### Compatibility
- Preserved SQLite truth, stable V1 identities, and the N-1/N/N-1 window. Managed upgrade performs no hosted embedding rebuild or memory-content egress. A provably committed candidate is started; a provably compensated failure restarts N-1; an ambiguous state remains stopped and fail closed.

## [2.0.0] - 2026-08-27

This release candidate is cumulative since the last public release, `1.10.3`. It completes the Scope Recall 2.0 product contract while preserving SQLite truth, stable V1 provider/tool identities, additive migration, and the N-1/N/N-1 compatibility window.

### Added
- Added strict Fact authority on the existing Fact Ledger with atomic legacy projection dual-write, explicit split planning, evidence checks, and fail-closed conflict handling.
- Added finite relation generation and shared DurableWork terminal-state/Doctor contracts without creating a second scheduler or durable work authority.
- Added one production Recall Packet compiler with current truth selection, conflict exposure, evidence ordering, deterministic diversity, and bounded token budgeting.
- Added deny-first two-phase Purge, governed tool profiles, optional extension boundaries, and a developer-only read-only Recall Inspector over the exact production packet.

### Changed
- Made current-truth selection, conflict exposure, and Recall Packet rendering the coherent 2.0 recall defaults while retaining independent rollback switches; token budgeting remains independently opt-in and default-off.
- Kept the default core tool profile within the historical compact schema budget; compatibility, maintenance, developer, and extension surfaces remain separately governed.
- Canonicalized historical construction-phase test names and regenerated repository governance evidence without deleting coverage or lowering release gates.

### Fixed
- Declared Windows time-zone data as a direct runtime dependency so a clean wheel installation can resolve `ZoneInfo("UTC")` without relying on optional vector dependencies to supply it transitively.
- Closed issue #51 with an accident-scale regression for the retired relation rebuild queue, including zero-write idle behavior, exact bounded focus planning, backup-first cleanup, CAS, receipts, and idempotent replay.
- Closed issue #58 by adding a default-on, process-wide idle writer handoff: every same-store Provider, capture queue, transaction, digest, named holder, and connection pin must quiesce before the OS lease is released, and uncertain teardown remains fail-closed instead of reporting a healthy reader.
- Corrected legacy hard-delete companion reporting so archive, merge, dedupe, nightly cleanup, and direct deletion classify only the exact Vector outbox intents created by that committed truth mutation; unrelated replay progress can no longer clear a pending deletion.

### Compatibility
- Preserved legacy projection reads and writes for N-1 interoperability; no claim-only durable user data is allowed in 2.0.x.
- Preserved stable V1 tool names and aliases, scope isolation, current-turn recall, read-only followers, one-writer authority, and rebuildable vector companions.
- Kept all migration IDs immutable and additive. Normal rollback disables product switches and reverts code without restoring the whole database; purge tombstones remain deny-authoritative.
- The retired standalone visual-console writer is not distributed in 2.0; no separate process may open the truth database for mutation outside the production command and writer-authority boundary.

## [1.10.6] - 2026-08-26

This patch candidate is cumulative since the last public release, `1.10.3`, and supersedes the unpublished `1.10.4` and `1.10.5` source checkpoints. It completes Scope Recall 2.0 Program 0A/0B without crossing G0: release controls are deterministic, Vector status has one public contract, and legacy relation fan-out is replaced by finite relation containment.

### Added
- Added the stable `ci-required` aggregate job and made release provenance depend on that single branch-protection check.
- Added one four-state Vector status contract (`ready`, `degraded`, `needs_repair`, `disabled`) with stable reason, debt, recoverability, repair, and query-usability fields across runtime, Doctor, and stats.
- Added additive relation containment state, generation-bound focus work, terminal dispositions, content-free health, and backup-first exact operator cleanup receipts.
- Added source/AST retirement gates, 2k/10k bounded regressions, a 100k analytical upper-bound gate, and cleanup dry-run/apply/replay coverage.

### Changed
- Replaced optional dependency extras in the release lock input with explicit direct pins and regenerated hashed constraints for reproducible Windows/Linux resolution.
- Made the CJK lexical latency gate portable on fast SQLite hosts by flooring the paired denominator at the declared target divided by the ratio budget. The hard bound is now equivalently `shadow_p95 <= max(100 ms, 4 * legacy_p95)`, preserving the 4x guard on slower hosts while preventing a near-zero legacy baseline from rejecting a target-compliant shadow path.
- Increased the CJK release benchmark default from 3 to 20 rounds, giving nearest-rank p95 one hundred timed query observations instead of fifteen while leaving the 100 ms target, 4x latency guard, and 2.5x page-growth guard unchanged.
- Moved CJK document-frequency filtering ahead of FTS rank evaluation and bounded every postings probe at `df_cap + 1`, so a corpus-wide trigram cannot force all matching rows through the ranking window before being discarded.
- Raised the default graceful-shutdown budget from 3 to 10 seconds while retaining one absolute deadline and every explicit timeout override, so legitimate cleanup on a loaded Windows host is not misclassified as a stuck teardown.
- Retired all executable full-scope relation rebuild enqueue/claim/drain paths. Affected-work planning now uses cap+1, performs no partial mutation when the cap is exceeded, and excludes stale generated relation signals while ordinary lexical/SQLite/vector recall continues.
- Bounded foreground-idle relation maintenance by configurable interval, shared wall-clock budget, finite batch limits, contention backoff, and maximum attempts; poison work becomes terminal and does not resurrect automatically.
- Exposed relation pending/retry/poison/operator-action health through Doctor, `scope_recall_stats`, and the dashboard while preserving the query zero-write contract.

### Compatibility
- Preserved SQLite as truth, stable provider/tool identities, package/install shape, scope routing, and ordinary recall semantics.
- Added only additive schema migration `0013_relation_containment_v1_10_6`; the retired legacy relation tables remain readable for exact backup-first cleanup and downgrade evidence but are never executed by the runtime.

## [1.10.5] - 2026-08-25

This patch candidate is cumulative since the last public release, `1.10.3`, and supersedes the unpublished `1.10.4` source checkpoint. It closes the remaining bounded-concurrency, release-provenance, and distribution-scanner defects found by exact-epoch review while retaining the issue #50 contract, without changing SQLite authority or stable provider/tool identities.

### Fixed
- Bound public shutdown, worker quiescence, and cleanup to one absolute deadline while retaining one tracked retryable cleanup worker instead of duplicating close attempts.
- Made Windows pinned-source checkout fail closed when process tree termination or bounded pipe collection cannot be confirmed after a Git timeout.
- Required the PyPI origin gate to verify that the exact release workflow run completed successfully, while keeping source-executing jobs on read-only contents permissions.
- Serialized queued capture with merge mutations so an accepted delayed write cannot recreate a merged source row.
- Preserved the current and remaining L4 candidates when the second fresh-evidence lookup fails, publishing retry context instead of a false completion.
- Resolved contradiction chains as a deterministic conflict graph so non-conflicting endpoints remain recallable while authoritative and two-node behavior stays stable.
- Restricted synthetic source-fixture exemptions to source scanning; wheel and sdist secret/path scans no longer mask matching distribution content.

### Compatibility
- Added no database schema migration and changed no public tool name, provider identity, package layout, or default scope mode.
- Preserved the cumulative `1.10.4` rollback metadata, governance receipt, Experience `run_id`, and `memory_auto_adjudication` throttle fixes on the last packaged `1.10.3` line.

## [1.10.4] - 2026-08-23

This patch candidate is cumulative since the last public release, `1.10.3`. It closes post-release governance and scheduling gaps around issue #50 without changing SQLite authority or stable provider/tool identities.

### Fixed
- Restored rollback metadata from the recorded before-snapshot instead of merging it with archived state, and rejected missing or malformed rollback snapshots instead of guessing an active record.
- Counted archive coverage only for explicit trusted event/action pairs whose latest receipt still matches the current archived row, so an old receipt or unknown writer cannot mask a later unaudited mutation.
- Kept Experience preflight runs pending with an empty `finished_at`, carried optional `run_id` feedback through the public tool path, and allowed one pending run to close after its playbook becomes terminal without mutating terminal playbook counters.
- Persisted the successful `memory_auto_adjudication` throttle marker in the governance ledger, so provider recreation cannot bypass the configured interval and failed runs remain retryable.

### Compatibility
- Added no database schema migration. Existing governance receipts, rollback event types, package/install shape, and V1 memory semantics remain supported.
- The feedback `run_id` field is optional; callers that do not use preflight run receipts keep the existing feedback behavior.
- Declared Python support is the tested 3.11–3.12 range. Windows CI covers both minors plus a no-symlink-privilege product lane. GitHub Release remains the sole artifact source for the one PyPI publish path.

## [1.10.3] - 2026-08-23

This patch is cumulative since the last public release, `1.10.2`. It fixes issue #50 by recognizing the official `memory_auto_adjudication` + `archive` receipt in governance coverage and cleanup rollback without trusting arbitrary archive writers. SQLite remains authoritative and stable provider/tool identities are unchanged.

### Fixed
- Counted the exact `event_type=memory_auto_adjudication` and `action=archive` pair as an audited archive mutation in the governance coverage report, so Doctor no longer reports a false missing-audit row after official automatic adjudication.
- Added that same exact event/action pair to default batch rollback selection. Rollback still verifies the recorded after-snapshot and refuses a row whose lifecycle or metadata changed after the receipt.
- Kept unknown event types fail-closed: a generic third-party `archive` action is neither governance coverage nor a rollback authority.

### Compatibility
- Preserved the existing `memory_cleanup`, `forgetting`, and `scope_recall_forget` soft-archive rollback contracts.
- Added no schema migration and changed no default adjudication policy.

## [1.10.2] - 2026-08-21

This patch source candidate is cumulative since the last public release, `1.9.2`. It incorporates and supersedes the untagged `1.10.0` public source candidate and the `1.10.1` public source candidate that reached the public tree. It is two CI fixture corrections and does not change production runtime behavior: the simulated external staging replacement no longer enters this process's truth-connection hardening cache, and Windows recovery-command test diagnostics decode CP936/GBK before permissive OEM fallback. It does not weaken descriptor hardening. SQLite remains authoritative and the stable provider/tool identities are unchanged.

### Fixed
- Stopped the verified online-backup cleanup fixture from writing the simulated external owner replacement through `connect_truth_database`, so POSIX descriptor-hardening identity checks no longer fire before cleanup ownership can preserve the replaced staging DB and sidecar.
- Decoded Windows recovery-command test diagnostics as CP936/GBK before host-dependent OEM or cp1252 fallbacks, so localized cmd.exe stderr is not silently mojibaked on en-US CI. Production recovery command generation is unchanged.

### Compatibility
- Preserved Fact Evolution, temporal queries, bounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, journal checkpoint ownership, and release-identity checks.
- Preserved the `1.10.1` POSIX owner-only descriptor-hardening contract and journal deferred-metric doctor fixtures. Identity replacement or permission drift after the cached hardening event still fails closed. Windows inherited-ACL behavior is unchanged.

## [1.10.1] - 2026-08-20

This patch source candidate is cumulative since the last public release, `1.9.2`. It incorporates and supersedes the untagged `1.10.0` public source candidate that reached `main` without a tag, GitHub Release, or PyPI artifact. It covers cross-platform SQLite lock hardening and deterministic journal health fixtures. SQLite remains authoritative and the stable provider/tool identities are unchanged.

### Fixed
- Cached POSIX owner-only descriptor hardening so a process raw-opens each live truth-database identity at most once, including when the same file is imported under top-level and `scope_recall.*` aliases; later writable connections cannot cancel same-process SQLite advisory locks. Identity replacement or permission drift after that cached event fails closed instead of raw-opening while locks may be held. An incompatible or foreign process-wide hardening marker fails closed and requires a process restart instead of being repaired into trusted cache evidence. Windows inherited-ACL behavior is unchanged.
- Isolated deferred-metric and pending-retryable doctor fixtures from the default 72-hour backlog-age failure policy so those tests stay deterministic without weakening production age checks.

### Compatibility
- Preserved Fact Evolution, temporal queries, bounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, journal checkpoint ownership, and release-identity checks.

## [1.10.0] - 2026-08-19

This minor source candidate covers public journal restore, backlog fairness, vector inventory, and runtime-module convergence since the last public release, `1.9.2`. The `1.9.3` writer-lease and digest-transaction work reached `main` as a source interval only: it was never tagged, given a GitHub Release, or uploaded to PyPI, and is incorporated here. This task creates a source candidate on `main` only. SQLite remains authoritative and the stable provider/tool identities are unchanged.

### Added
- Added dry-run, epoch, backup, ledger, and idempotent journal source restore for a trusted snapshot window.
- Added bounded unresolved-journal retry/quarantine and fair per-session budget deferral (issues #45/#48/#46).
- Added a structured non-activatable inactive READY vector inventory (#44).
- Assembled one production command port and converged internal runtime modules behind thin provider/tooling entrypoints.

### Fixed
- Kept the shutdown barrier so a non-acknowledging journal or capture worker leaves connections, vector resources, and the writer lease held for a later retry.
- Preserved WAL reconciliation and epoch fencing on the writer-owned truth path.
- Incorporated the unpublished `1.9.3` source interval: one writer per truth database, read-only followers, digest model calls outside write transactions, and idle same-process peer recovery.

### Compatibility
- Preserved Fact Evolution, temporal queries, bounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, journal checkpoint ownership, and release-identity checks.

## [1.9.3] - 2026-08-14

This compatibility-preserving source candidate covered the highest-priority open SQLite contention and writer-ownership risks since the last public release, `1.9.2`. It reached `main` without a tag, GitHub Release, or PyPI artifact. SQLite remains authoritative, additional processes fail closed to read-only follower mode, and the stable provider/tool identities are unchanged.

### Fixed
- Enforced one write-capable Scope Recall process per truth database across gateway, CLI, and other runtimes. Provider instances in the writer process share its lease; a provider in another process opens as a read-only follower, refuses mutation tools, and may take over only after the writer exits and the operating system releases the lease.
- Made same-process lease reuse atomic across threads and import aliases, normalized Windows case and junction paths, and released lease handles on journal/nightly configuration failures and every provider shutdown path.
- Moved journal and nightly model/network work outside authoritative SQLite write transactions. Per-scope results and checkpoints now commit in short bounded transactions so vector retention and other writers are not blocked for the duration of a model call.
- Recovered one idle same-process dirty peer during initialization only after a real SQLite lock error, while preserving non-lock failures, active work, cross-process ownership, and read-only follower boundaries.
- Sanitized writer-owner sidecars, status output, and busy diagnostics before they reach operator-visible surfaces; unknown tool names can no longer inject path- or credential-like text into lock errors.

### Compatibility
- Preserved the stable V1 provider ID, public tool names, package/install shape, SQLite truth-source contract, rebuildable vector/graph companions, scope routing, evidence authority, provenance-root validation, deterministic idempotency, release-identity checks, Fact Evolution, temporal queries, Reflection, and existing journal checkpoint semantics.

## [1.9.2] - 2026-08-09

This cumulative patch release covers runtime reliability and recall-precision fixes since the last public release, `1.9.1`. SQLite remains authoritative, derived vector state remains replayable, and the stable provider/tool identities are unchanged.

### Added
- Added explicit `query_variants` evidence-set retrieval with bounded per-query search, round-robin specialist evidence slots, global RRF fill, per-query rank provenance, an opt-in `evidence_diversity_depth=1..6` (default `3`), and an opt-in Top-50 public search ceiling while preserving the compact default. Indexed OpenAI-compatible batch responses are restored to input order, and the standard funnel trace remains bound to the primary query.
- Added a resumable isolated LoCoMo runner that preserves dialogue/image/time provenance, records source/config/dataset hashes and Recall@K evidence, separates invalid model or judge calls from wrong answers, and always shuts providers down before advancing. External dataset, Hermes source, and auth paths must be supplied explicitly. Path-free source receipts bind the HEAD tree, index entries, raw tracked worktree bytes/modes/symlinks, and untracked bytes without depending on Git diff rendering; execution receipts also bind workers, model rounds, timeout, and a secret-free model route. Retrieval, query-plan, and result checkpoints must match canonical identity and exact row types before resume, scoring, or official reporting, and every model call revalidates route identity while allowing same-route token refresh. Judge labels accept only exact-case JSON/token contracts without undeclared or duplicate fields, and the official-comparability flag additionally requires the canonical dataset/questions/category composition, retrieved rather than oracle evidence, validated checkpoint sets, complete scoring/retrieval metrics, and a valid model-route receipt.

### Fixed
- Replayed committed event-digest candidate vector intent immediately after the SQLite transaction and outside the provider database lock. Replay targets the causal outbox event IDs rather than allowing unrelated older backlog to consume the bound, reports pending/failed companion work explicitly, and preserves durable outbox recovery when embedding is unavailable.
- Replaced live reconciliation's raw `open()/close()` header read with a pager-native `PRAGMA schema_version` probe on the provider-owned connection. Raw file-header probes now require an explicit quiesced-connection declaration, preventing same-process POSIX advisory-lock cancellation while preserving fail-closed corruption receipts.
- Prevented curated source and target priors from manufacturing lexical relevance for unrelated queries; pure-noise queries now return no curated fallback unless lexical, phrase, intent, or independently qualified vector evidence exists.
- Rolled back failed journal transactions before persisting error receipts, sanitized the full exception before applying the receipt length cap, preserved the triggering exception when receipt storage is also contended, recovered only idle same-process SQLite peers without waiting on active work, quarantined connections whose rollback fails, retried one bounded background digest, and retried optional completed-outbox retention once without weakening truth-write failure semantics.
- Downgraded database URI examples to manual review only when username, password, and host are all explicit placeholder values; production-like hosts remain actionable even with weak `user/password` credentials. Canonical URI scanning no longer depends on a leading word boundary, and capture/durable-store filtering remains fail-closed.
- Made funnel, evidence-set, rejected-candidate, and temporal diagnostics request-local via context variables, so concurrent calls on one provider cannot return another request's trace.
- Stopped treating the first two characters of arbitrary CJK prose or common polite query prefixes as hard entity declarations. Declared entities and factual claim subjects are now case-folded and own scope before incidental prose or `Project` mentions; explicit proper-name conflicts outrank shared generic terms such as `recovery`, and unrelated Latin names cannot suppress a matching Chinese subject.

### Packaging
- Added the shared SQLite contention/recovery module to source, wheel, sdist, and Pyright coverage, and advanced package, plugin, benchmark, readiness, and release-gate identity together.
- Made GitHub Release publish hand PyPI delivery off through an explicit `repository_dispatch`, with tag/version revalidation, the existing OIDC `pypi` environment, and fail-loud duplicate uploads; manual tagged recovery remains available.

### Compatibility
- Preserved Fact Evolution, temporal queries, bounded Reflection, scope routing, evidence authority, provenance-root validation, idempotency, journal checkpoint ownership, release-identity checks, stable tool names, and the SQLite truth-source contract.
- WAL runtime safety depends on the SQLite library linked into Python: use `3.51.3+` or fixed backports `3.50.7`/`3.44.6`. The plugin now avoids same-process raw file probes on live truth databases but does not replace the host SQLite runtime.

## [1.9.1] - 2026-08-08

This cumulative public release covers all changes since the last public release, `1.8.7`. The version path is documented explicitly because `1.8.8` and `1.8.9` were development intervals rather than tagged package candidates, and `1.9.0` reached `main` as a source candidate but was never tagged, released, or uploaded to PyPI. SQLite remains authoritative and the stable provider/tool identities remain unchanged.

### 1.8.8 — delivery-pipeline interval (not cut)
- Immediately after `1.8.7`, release commands were scoped to the repository and PyPI delivery was moved onto the trusted GitHub Actions publishing path, with a manual fallback retained.
- No `1.8.8` runtime package was cut: this interval repaired release delivery machinery and was carried forward into the next product release instead of publishing another package with unchanged runtime behavior.

### 1.8.9 — minor-upgrade interval (not cut)
- Development then expanded beyond patch-only maintenance into a new CJK lexical shadow generation, indexed two-character postings, Windows long-path-safe rollout and rollback, and a unified fail-closed endpoint policy.
- No `1.8.9` candidate was cut: that user-visible feature scope warranted a SemVer minor transition, so the work became the `1.9.0` source line rather than another `1.8.x` patch.

### 1.9.0 — source candidate (not published)

The `1.9.0` candidate was pushed to `main` but received no tag, GitHub Release, or PyPI package. It established the feature line below and was superseded after cross-platform CI exposed a POSIX-only release-fixture permission mismatch.

#### Added
- Consolidated the cumulative Fact Evolution, temporal query, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, and release-identity guarantees required by the 1.9.1 public release line.
- Added a release-gated CJK lexical benchmark that records high-interference recall, English non-regression, requested-limit enforcement, p50/p95 latency, and SQLite page growth.
- Added a backup-first CJK lexical shadow index with resumable bounded backfill, truth-table maintenance triggers, synthetic/live dual-read quality evidence, explicit compare-and-swap activation, read-only doctor health, and pointer-only rollback that retains the legacy index.
- Added an indexed CJK bigram postings channel for two-character concepts that SQLite trigram FTS cannot represent: postings are keyed by the truth rowid, maintained by the same generation triggers and bounded backfill, and queried through a covering-index document-frequency prefilter that drops corpus-wide terms instead of scanning truth rows; the active read path permanently retains legacy FTS/LIKE/alias candidates, and the release gate rejects English candidate regressions.
- Added Windows extended-length filesystem primitives with complete destination preflight, short collision-resistant backup/staging roots, public-path receipts, repeatable rollback, and automatic compensation when final replacement fails.
- Added one endpoint-policy configuration gate across capture, journal/nightly, reflection, OpenAI-compatible embedding, and MiniMax embedding, with explicit CLI opt-in for trusted non-loopback HTTP endpoints.
- Added a read-only `doctor` endpoint-policy check for enabled capture, LLM journal, reflection, and hosted primary/fallback embedding transports; it resolves the same inherited provider routes and embedding base-URL environment aliases as runtime without reading API keys, and reports only origins plus recognized public API suffixes.

#### Fixed
- Bound READY/ACTIVE lexical quality receipts to a strict privacy-safe schema, fixed provenance, source revision, integrity report, and canonical evidence fingerprint; stale or forged receipts now fail closed.
- Acquired the durable maintenance lease and SQLite DML guard triggers for lexical build/activate/rollback, with backup-first source fencing and explicit release evidence.
- Mapped shadow FTS rows and bigram postings to truth-row integer docids so trigger and backfill identity maintenance is an indexed rowid operation, and restricted the shadow FTS query to a bounded `rank` candidate window ordered before the outer recency tie-breaker; the strict release gate now proves the 50,000-row shadow contract with hard gates on relative p95 ratio (`<= 4`), page growth (`<= 2.5`), CJK/English correctness, and result caps, while `shadow_p95_ms <= 100` remains a cross-host target recorded via structured `target_misses` rather than a universal hard fail.
- Held the lexical maintenance `BEGIN IMMEDIATE` fence across source binding, the online backup copied through a separate reader connection, the post-backup binding compare, and guard-trigger installation, so a raw writer can no longer commit between the compare and guard boundaries and leave the backup inconsistent while the receipt reports `stable`; the backup itself remains free of temporary guard triggers, and all four raw-writer injection boundaries are covered by permanent race tests.
- Normalized credential query/header keys until percent-decoding is stable (bounded against obfuscation bombs), failing closed on malformed, invalid-UTF-8, or residual escapes and on keys that stay encoded past the decode bound; deeply encoded aliases such as depth-4+ `api_key` are rejected in HTTPS queries and stripped at plaintext-HTTP sinks, while non-credential metadata keys remain allowed.
- Made the release gate fail closed with structured prerequisite output when Git is missing from `PATH` instead of raising a bare `FileNotFoundError` traceback.
- Covered held-out Chinese recall quality with a dedicated golden set spanning synonym rewrites, typos, homophone and near-shape confusions, high-frequency interference, negation, lifecycle-hidden rows, scope isolation, and forbidden IDs, reporting MRR, nDCG, Precision@k, and false-positive rate with explicit legacy-versus-shadow channel attribution in both vector-off and vector-on configurations.
- Enforced requested limits for direct vector retrieval after stable score ordering and ID deduplication.
- Extended Windows long-path handling to profile enumeration, manifest/config reads, rollback receipt reads, and atomic receipt publication.
- Required durable pre-mutation rollout receipts and compensated installer failures, `ok=false` results, and post-install receipt publication failures before stopping further profile changes.
- Applied final relevance ordering before enforcing the requested SQLite lexical result limit, so direct storage-view callers no longer receive the larger internal candidate pool.
- Fixed cross-profile and installer backup/restore failures when deep profile homes pushed copied descendants past the legacy Windows path limit; failed copies now clean partial destinations before active plugin mutation.
- Rejected non-HTTP(S), credential-bearing URL authorities and query parameters, fragments, cross-origin redirects, and HTTPS-to-HTTP downgrades before memory-bearing requests can leave the process. Loopback HTTP remains compatible for local model servers, while every HTTP path strips authorization, API-key, cookie, and proxy credentials; OpenAI SDK embedding calls no longer auto-follow redirects, only a literal boolean `true` can opt into plaintext HTTP, and endpoint-policy failures cannot degrade into heuristic fallback.
- Kept ordinary feature-flag compatibility separate from endpoint permission: quoted `"true"`/`"false"`, numbers, arrays, objects, and every other non-boolean endpoint opt-in fail closed at config, public-option, custom-hook, and direct transport boundaries.
- Kept public journal overrides and capture-provider callers fail-closed: malformed insecure-endpoint opt-ins cannot be truthified downstream, and endpoint-policy blocks suppress journal heuristic plus per-turn regex/raw durable fallbacks without changing fallback behavior for ordinary provider outages.
- Bound forget and merge memory-ID arrays to 1,000 items and each ID to 512 characters at both schema and runtime boundaries; affected SQLite truth, fact-ownership, lifecycle, merge, and delete paths now chunk against the live connection variable limit without committing between chunks.
- Unified URL-query rejection and plaintext-HTTP header stripping behind one normalized credential-key registry, including Azure APIM, OAuth assertions, Google signed requests, AWS signed requests, generic `x-token`/`access_key_id`, auth/bearer tokens, and provider API-key aliases while preserving non-credential metadata such as `api-version`, `model-version`, `page_token`, and `token-estimate`; insecure-endpoint warnings now expose only the origin plus a recognized public API suffix.
- Preserved raw `allow_insecure_endpoint` values through OpenAI-compatible and MiniMax embedder builders until strict constructor transport validation, so strings, numerics, arrays, and objects are rejected even for HTTPS and loopback endpoints instead of being silently coerced to `false`.
- Reworked Experience statistics as scoped relational aggregation instead of expanding every accessible playbook ID into one `IN (...)` list, preserving playbook/run scope checks below reduced SQLite host-parameter limits.
- Made the release runner force UTF-8 for Python subprocesses and decode captured output explicitly, so non-UTF-8 Windows system locales cannot lose benchmark or package-stage JSON to reader-thread decode failures.
- Made primary and fallback embedding `base_url_env` valid runtime configuration and ensured a configured non-empty environment value overrides the packaged URL fallback in both runtime construction and doctor checks.

### 1.9.1 — public release finalization

#### Added
- Added a stable profile-local opaque Desktop principal fallback when Hermes Desktop omits `user_id`; it persists across restarts, remains distinct across profiles, avoids host-account/path PII, permits an explicit override, and leaves non-Desktop runtimes fail closed.
- Added `vector.startup_reconcile_enabled` as an explicit stop switch plus a cheap SQLite-header preflight, so operators can disable automatic outbox/truth reconciliation and already-corrupt truth storage fails closed before further reconciliation work.
- Added a single-responsibility verified SQLite online-backup/health boundary for activation receipts, checking source and backup health plus logical fingerprint equivalence; ordinary startup still does not create backups.

#### Changed
- Propagated optional thinking controls through journal and nightly LLM calls and made the default lifecycle for non-time-sensitive automatic digests configurable while retaining review-first candidate behavior.
- Made the 50,000-row lexical release contract host-portable: relative p95 latency (`<= 4x`), page growth (`<= 2.5x`), CJK/English correctness, and requested result caps remain hard gates, while absolute `shadow_p95_ms <= 100` is reported as a cross-host target through structured `target_misses`.

#### Fixed
- Corrected the lexical-doctor release fixture to create SQLite truth storage through the production truth-connection boundary, preserving the 0700/0600 POSIX permission contract instead of weakening the doctor gate.
- Made relation-rebuild debt converge without reopening completed work, and made bounded vector reconciliation serialize, expose an explicit disabled receipt, and stop before outbox writes when the truth header is already invalid.
- Made Desktop principal recovery fail closed on corrupt or unreadable persisted identity and publish first-create identities with durable atomic replacement under concurrency.
- Made lexical backfill page replay idempotent, added the docid-leading postings index and health check, and made integrity checks detect rowid/memory-id identity swaps without correlated shadow rescans.
- Kept POSIX staging reservation descriptors open through identity-aware path cleanup before closing them, preventing immediate inode reuse from misclassifying an external replacement as call-owned; Windows retains close-before-unlink semantics, and identity-bound close retries still refuse reused descriptors.

### Compatibility
- Preserved the stable V1 provider ID, public tool names, SQLite truth-source contract, and rebuildable vector/graph companions.
- Preserved opt-in Fact Evolution, temporal current/as-of/history queries, bounded citation-grounded Reflection, scope routing, evidence authority, provenance-root validation, deterministic idempotency, atomic journal checkpoint behavior, and the release-identity contract.
- Durable `user`, `memory`, `project`, and `ops` targets continue to use governed shared scope; `general` remains local scratch. Optional PGVector and legacy-import paths remain optional and are not runtime dependencies.

## [1.9.0] - 2026-08-06

The 1.9.0 source candidate was pushed to `main` but was not tagged or published. It was superseded by 1.9.1 after cross-platform CI exposed a POSIX-only test-fixture permission mismatch; runtime safety behavior was unchanged.

## [1.8.7] - 2026-08-03

This cumulative release covers all changes since the last public release, `1.8.2`. It keeps SQLite authoritative and the stable provider/tool identities unchanged while combining the 1.8.3-1.8.6 reliability line with final identity, freshness, secret-handling, and cross-platform release hardening.

### Added
- Added a public vector-only threshold calibration fixture, bounded completed-outbox retention, and platform-native recovery-command generation.
- Added dry-run-first, receipt-backed operator recovery for legacy freshness debt, vector dead letters, and stale activation leases.
- Added blocking Windows Python 3.12 and pinned optional-native-dependency release lanes alongside Linux and macOS validation.

### Changed
- Made fact freshness an authoritative companion projection across recall and profile output. Invalid legacy validator metadata is quarantined as live-check debt, valid rows continue through bounded maintenance, and untracked rows never masquerade as verified current facts.
- Raised the default vector-only threshold to the calibrated value while preserving explicit per-profile overrides; local-embedder readiness and fresh fallback remain explicit and cannot reopen an existing generation with a different embedding space.
- Made forgetting policy switches effective, separated contradiction surface/penalize/suppress behavior, and tightened exact-text deduplication so distinct durable memory types remain distinct.
- Kept Experience promotion and Fact Evolution evidence-gated and reviewable, with scope routing, evidence authority, provenance-root validation, idempotency, and journal checkpoint ownership enforced at mutation boundaries.

### Fixed
- Failed closed before storage initialization when a non-CLI Hermes runtime lacks a trusted principal, preventing unscoped reads, writes, prompt injection, or background maintenance.
- Fixed current-state ranking and temporal interpretation for short Chinese and system/location questions without leaking stale, historical, or merely normative facts into present-state answers.
- Fixed Experience review, dedupe, merge, and transaction ownership across authenticated canonical-user and legacy account scopes.
- Hardened Windows PID liveness, installer replacement and rollback, long paths, FTS repair, console-safe operator JSON, LanceDB backup, and activation compensation without applying Unix-only assumptions.
- Centralized secret detection and redaction across capture, durable writes, recall, doctor, HTTP errors, release scanning, structured mapping keys, private-key blocks, cookies, tokens, and database credentials, including Unicode-compatible key forms.
- Hardened lifecycle relation restore, freshness backfill, semantic deduplication, truth-store permissions, package membership, release-identity checks, and pinned Windows/macOS/Linux CI lanes.

### Compatibility
- Preserved the stable V1 provider ID, tool names, SQLite truth-source contract, and rebuildable vector/graph companions.
- Preserved opt-in Fact Evolution, temporal current/as-of/history queries, bounded citation-grounded Reflection, existing evidence authority and provenance-root rules, deterministic idempotency, atomic journal checkpoint behavior, and the release-identity contract.
- Durable `user`, `memory`, `project`, and `ops` targets continue to use governed shared scope; `general` remains local scratch. Optional PGVector and legacy-import paths remain optional and are not runtime dependencies.

## [1.8.6] - 2026-08-01

### Changed
- Made legacy fact-freshness backfill quarantine invalid validator metadata, continue past malformed rows, and re-scan under an immediate owner transaction; startup now defers recoverable SQLite contention explicitly.
- Moved the standalone capture-LLM probe out of pytest collection while retaining an explicit subprocess contract for all manual checks.

### Fixed
- Added governed defaults and configuration-registry ownership for untracked, needs-live-check, stale, and expired fact-freshness ranking penalties.
- Closed Unicode-compatible sensitive-key bypasses and centralized HTTP/transport error redaction on the canonical secret-pattern taxonomy.
- Made freshness, vector dead-letter, and activation-lease operator JSON ASCII-safe; routed stale-lease recovery through the shared truth-connection boundary.
- Rejected unrelated relation endpoints during lifecycle rollback and kept exact-text rows with distinct durable memory types out of the same deduplication group.
- Preserved Fact Evolution, temporal, Reflection, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, release-identity, and Windows PID-liveness contracts.

## [1.8.5] - 2026-08-01

### Fixed
- Replaced Windows activation-lease PID probing through `os.kill(pid, 0)` with a read-only process-handle query, preventing child doctor checks from sending `CTRL_C_EVENT` to a process-group owner.
- Preserved Fact Evolution, temporal, Reflection, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, and release-identity contracts.

## [1.8.4] - 2026-08-01

### Added
- Added dry-run-first operator recovery for stale activation leases, legacy freshness coverage, and vector outbox dead-letter events, with verified SQLite backups, idempotent operator-ledger evidence, and mirrored receipts.
- Added a blocking Windows Python 3.12 full-suite CI lane alongside the focused installer contract.

### Changed
- Made every authoritative memory insert initialize freshness in the same SQLite transaction using memory-type policy defaults; public recall now supports `advisory` and `strict` freshness modes with explicit warnings.
- Made forgetting policy switches effective, including the two-key hard-delete safety gate, and implemented distinct `surface`, `penalize`, and `suppress` contradiction modes.

### Fixed
- Closed maintenance-tool schema gating, PyPI fail-open, shared-connection lock, SQLite reconnect, truth-store permission, release-source coverage, and stale activation-guard recovery gaps.
- Centralized secret patterns across capture, doctor, and release scanning; expanded provider/token/database/cookie coverage, stopped exempting force-added sensitive files, and removed matched-value echo from release findings.
- Made operator JSON automation ASCII-safe under Windows legacy console encodings and aligned POSIX doctor fixtures with the owner-only truth-store contract.
- Preserved Fact Evolution, temporal, Reflection, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, and release-identity contracts.

## [1.8.3] - 2026-07-31

### Added
- Added a public 72-pair `gemini-embedding-001` vector-only threshold calibration fixture, a metric gate, bounded completed-outbox retention, and platform-native recovery-command generation.

### Changed
- Raised the default vector-only recall threshold from `0.30` to calibrated `0.70`, while preserving explicit per-profile overrides; the packaged benchmark reduces weighted error from 29 to 16 at the required 0.80 recall floor.

### Fixed
- Restored strict-schema and runtime compatibility for operator-authorized `identity.chat_aliases`. Exact chat aliases remain opt-in, require cross-platform identity sharing, and take precedence over account aliases because they explicitly grant the whole chat one canonical durable identity.
- Fixed short Chinese system/location questions being tokenized as hard entity scopes, added bounded answer-shape intent evidence and present-state authority ranking, and kept historical questions out of current-state reranking.
- Fixed Experience review/dedupe/merge closure across authenticated canonical-user and legacy account scopes. Runtime-derived owner aliases are restricted to accessible non-pool scopes, structured shared-pool ids can never prove owner equivalence, and review/merge apply revalidates authoritative rows under an immediate write transaction with compare-and-swap updates. Optional prior dry-run payloads bind both public tool and storage apply paths; direct callers remain exact-scope by default.
- Added raw Telegram-ID curated-memory allowlist coverage for canonical identity configurations without changing the conservative gateway default.
- Rejected empty or malformed account/chat aliases at both runtime resolution and configuration ingestion, and made canonical-alias governance tests exercise the actual cross-platform gate.
- Fixed `merge_playbooks(commit=False)` transaction ownership and journal doctor streak semantics so callers never receive an uncommitted success and recovered digest runs reset current failure health.
- Made Windows FTS repair, activation compensation, LanceDB backup, long-path handling, symlinked config updates, and manual rollback receipts use verified platform-correct contracts; genuine external file locks remain fail-closed with a physically retained maintenance lease.
- Required concrete answer evidence for current operating-system and timezone questions, including Linux distributions and multi-character Chinese subjects, so generic manuals and topic mentions cannot outrank the actual current fact.
- Preserved the stable Fact Evolution, temporal, Reflection, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, and release-identity contracts while hardening their surrounding reliability boundaries.

## [1.8.2] - 2026-07-28

### Added
- Added a durable, cursor-based relation rebuild queue with bounded foreground synchronization, monotonic lifetime/pass progress, next-revision handoff, background draining, read-only debt reporting, and backup-first repair tooling.
- Added a transactionally maintained relation-frequency companion with per-memory postings, per-scope/entity document counts, bounded peer lookup, resumable legacy backfill, and scope reclassification debt.
- Added outbox-first vector startup reconciliation with bounded truth pages, a durable compound watermark, atomic page planning, and resumable background continuation.
- Added an authoritative SQLite operator ledger for playbook lifecycle changes, with deterministic post-commit receipt mirroring and idempotent repair for interrupted mirrors.
- Added clean-install regressions that load an installed plugin from outside the source tree and verify nested-clone wheels in a fresh virtual environment despite a polluted parent path.
- Added configurable `light`, `balanced`, and `full` semantic retention profiles for immediate and journal LLM extraction; sanitized turn text remains in the journal instead of being duplicated into durable recall memory.

### Fixed
- Made the Ruff lint contract explicit (`E4`, `E7`, `E9`, `F`) and excluded CI's temporary Hermes source copy so toolchain default changes cannot silently redefine the release gate.
- Preserved the 1.8.0 Fact Evolution, temporal, Reflection, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, and release-identity contracts unchanged.
- Enforced the no-transcript-duplication contract with a deterministic source-overlap gate shared by per-turn, journal, and nightly LLM extraction; long exact or near-verbatim copies are rejected before durable recall writes while short quotations remain allowed.
- Made private-key redaction fail closed when a PEM block extends beyond the bounded capture scan, and made fresh vector bootstrap remove only a newly created, proven-empty local companion when manifest publication fails so dynamic-dimension retries remain automatic. SQLite main, WAL, SHM, and rollback-journal files now share one presence and cleanup ownership boundary, preventing compensation from deleting pre-existing sidecars.
- Made journal LLM transport, authentication, and parse failures return an error without advancing source checkpoints or misclassifying infrastructure failure as data rejection; retained-row pruning now stays below SQLite variable limits.
- Made event-candidate batches atomic, verified semantic-merge update receipts, and made repeated identical lifecycle transitions true no-ops without timestamp, audit, or vector-outbox churn.
- Closed writer shutdown enqueue races, made dead writer queues fail closed, and made current-turn recall prefetch fail soft without destabilizing the host turn.
- Aligned PGVector repair with the SQLite cleanup contract, corrected lexical fallback when no vector signal exists, and made relation-frequency poison rows use per-row savepoints with bounded retry/dead-letter evidence (`0011_relation_frequency_failure_queue_v1_8_0`).
- Rejected new plaintext secret-like content at the authoritative SQLite store/update boundary and redacted legacy sensitive rows from recall, prompt, and memory-inspection egress.
- Sanitized secrets and private filesystem paths again at the optional per-turn extraction network boundary, so direct callers cannot send unsanitized turn text to a separately configured capture LLM.
- Compensated activation leases and SQLite guards when installation fails after snapshot but before activation handoff, and included both retry-exhausted and dead-letter journal entries in default recovery inspection.
- Enforced public tool JSON Schemas at the in-process dispatch boundary, with redacted structured errors for required fields, types, enums, lengths, list sizes, and numeric bounds.
- Made fuzzy store merging explicit and conservative: exact duplicates remain automatic, while opt-in semantic merge accepts only contained additive assertions and preserves changed values as separate memories.
- Enforced target-derived write scopes so `general` remains local and durable targets cannot be redirected into chat-local storage; explicit shared-pool writes retain their existing policy gates.
- Changed sensitive forgetting to fail closed by default, reduced generic graph-entity noise, and stopped normative references to current state from being classified as concrete runtime snapshots.
- Made background journal-digest shutdown quiescent and fail closed: new digest work is blocked once shutdown begins, synchronous and asynchronous work are both tracked, and shared SQLite/vector resources remain open when a worker cannot acknowledge the bounded stop request.
- Serialized complete vector outbox replay and bounded reconciliation per storage path so concurrent session providers cannot overlap SQLite schema/outbox maintenance or stall each other during foreground writes.
- Made lexical FTS integrity lifecycle-aware so only ordinary-recall-visible rows are expected, inserted, or rebuilt; `doctor` now fails on hidden legacy membership drift, and a dry-run-by-default maintenance CLI requires explicit writer-stop confirmation plus a verified owner-only online backup before apply.
- Rendered recalled memory snippets as single-line escaped JSON under an explicit untrusted-data boundary, preventing stored Markdown/XML-like text from manufacturing prompt sections or acquiring instruction authority.
- Created and reopened mutable SQLite vector companions with owner-only file permissions, including active sidecars, and rejected symlink-following mutation paths.
- Restricted temporary-memory markers to lexical boundaries, so durable words such as `template` are no longer demoted by the substring `temp`.
- Completed isolated-chat coverage by suppressing Hermes' parallel built-in curated-memory surface in addition to Scope Recall prompt, tool, capture, journal, and digest paths.
- Removed full-truth and full-vector enumeration from ordinary vector startup; durable outbox debt is replayed before one bounded truth page, and the page watermark advances atomically with its outbox events.
- Removed journal and nightly vector companion bypasses in favor of committed outbox replay, made LanceDB upserts idempotent across concurrent table handles and processes, and made duplicate physical IDs a blocking doctor condition.
- Made foreground relation synchronization use an independently bounded neighborhood, with cached deterministic tokenization and trigger patterns; exhaustive work continues through the durable rebuild queue.
- Made deterministic operator-receipt publication refuse concurrent conflicting evidence instead of overwriting it between validation and atomic publication.
- Prevented large relation scopes from rolling back otherwise valid store, update, or merge operations merely because an exhaustive pair scan exceeded the foreground budget.
- Replaced foreground relation-frequency truth scans with transactionally maintained per-scope/entity counts; blocked-entity reads and synchronous peer selection now use the companion index, while legacy backfill and threshold reclassification run as bounded recoverable maintenance.
- Made relation-frequency receipt refresh fail closed when its corpus-revision compare-and-swap loses a cross-connection race, so rebuild workers defer instead of binding stale blocked-entity policy.
- Made manifestless non-empty vector state fail closed consistently across setup, runtime startup, N-1 upgrade preflight, and the explicit migration CLI; migration now builds a validated shadow generation and can CAS-activate it without first fabricating a legacy current manifest.
- Split vector-store opening into read-only inspection and existing-only runtime mutation contracts, so an active generation can be updated without allowing startup to create missing storage or switch to a different backend.
- Preserved the original `2/4/8s` OpenAI-compatible connection-retry behavior from #27 while keeping the hardened bounded schedule configurable and allowing an explicit empty array to disable it.
- Refined the token-assignment boundary issue reported by @df-5c in #28: `per_token` and `*_per_token` metric assignments no longer trip plaintext-secret filtering, while compound credential keys such as `access_token`, `session_token`, and `super_token` remain blocked and redacted across text and structured-key surfaces.
- Made local SentenceTransformers readiness load the configured model before creating a vector generation, suppress private exception causes, match active generations against post-load dimensions, and try an equivalent fallback after a device-specific failure. Fresh bootstrap now serializes physical creation with manifest publication, loads fallback models only when needed, inventories named companions even when their embedder block is missing, and shares both success and sanitized failure within each concurrent model-load cohort.
- Fixed the LM Studio/llama.cpp tool-grammar failure reported by @lost-in-thoughts in #31 and explored in #30 by removing only unsafe nested long-string grammar bounds; structured freshness, claim, and evolution capabilities remain available, with a static release guard and validation against the upstream C++ converter/parser.

## [1.8.1] - 2026-07-23

### Fixed
- Made the dependency-free SQLite vector fallback portable to Windows by applying descriptor-based POSIX mode hardening only where CPython exposes `os.fchmod`; Windows continues to rely on the inherited profile-directory ACL boundary.
- Closed raw SQLite test connections before activation compensation replaces database files, covering Windows' refusal to unlink or replace an open database while preserving the same fail-closed rollback contract.
- Made explicit CJK entity regression coverage deterministic without the optional `jieba` package, and declared the `setuptools` build backend in the development test environment used by no-isolation clean-build checks.
- Preserved the 1.8.0 Fact Evolution, temporal, Reflection, scope routing, evidence authority, provenance-root, idempotency, journal checkpoint, and release-identity contracts unchanged.

## [1.8.0] - 2026-07-15

### Added
- Added opt-in structured Fact Evolution with temporal current/as-of/history queries, reviewed mutation receipts, and deterministic release benchmarks for scope routing, evidence authority, replay safety, and journal checkpoint atomicity.
- Added bounded Reflection synthesis with strict citation allowlists, citation-grounded candidate material, provenance-root source diversity, and explicit review-only mental-model candidates.
- Added public runtime-configured chat source isolation across prompt recall, tools, capture, journal, and digest backlog processing; deployment identifiers remain outside the package.
- Added read-only N-1 upgrade compatibility checks for runtime configuration and READY vector-generation physical receipts before any backup or replacement.

### Changed
- Accepted the vector-only threshold and configurable OpenAI-compatible embedding retry contribution from @df-5c in #27, preserving contributor authorship; the 1.8.0 follow-up adds strict transport-exception classification, bounded runtime validation, operator documentation, and regression coverage.
- Centralized target-to-scope routing so durable `user`, `memory`, `project`, and `ops` facts use the shared scope while `general` remains local scratch.
- Made Fact Evolution idempotency derive from stable source identity rather than scheduler run IDs, and made journal fact actions and source checkpoints atomic per candidate.
- Expanded configuration diagnostics with per-mode persistence risk, legal choices, and resident-versus-scheduled reload semantics.
- Replaced audit-number-specific release commands with a versioned manifest of transaction, temporal, activation, privacy, and N-1 upgrade invariants.
- Expanded the Reflection benchmark from two to eight valid responses and added explicit polarity, role-order, temporal-order, conditional, quantifier, and historical proposition matrices; memory-evolution release metrics now include evidence polarity/subject binding, chunk provenance, global exposure budgets, and adversarial zero-write behavior; the release aggregate also runs fixed 100k/1M temporal-ledger p50/p95/p99 and scan-cap profiles.

### Fixed
- Prevented unrelated user quotes from authorizing claims merely because assistant or model text in the same batch mentioned the proposed value.
- Bound first-person fact evidence to a trusted runtime speaker subject, rejected contraction and CJK negation for positive claims, and kept adversarial `auto_apply` attempts at zero durable writes.
- Rendered real message IDs in nightly/journal prompts, restricted citations to the current chunk, checkpointed only exact cited IDs, kept parse/filtered chunks pending, removed the 80-message provenance cap, and enforced `max_session_chars` as a global exposure budget.
- Made `install --activate` failure-atomic across plugin, Hermes config, provider config, and SQLite state by capturing pre-state and a verified SQLite online backup before replacement, including both link identity and dereferenced target bytes/mode for symlinked config paths, then compensating and read-back verifying config, migration, provider-load, and runtime-verification failures.
- Bound fact evidence to token/entity boundaries and ordered subject-predicate-value roles, made public tool-lane evidence non-authoritative without a runtime registry, and required RETRACT evidence to match the ledger-owned target claim with explicit correction semantics.
- Rejected future-effective successors and future/finite ADD intervals that the static lifecycle cannot safely represent; RETRACT now defaults its valid-time boundary to the transaction timestamp, supports an explicit trusted past boundary, and rejects future closure.
- Required confirmed maintenance mode and a SQLite writer-lock preflight before activating an existing truth DB; unconfirmed compensation cannot overwrite post-snapshot truth, and changed vector companions are discarded with rebuild receipts.
- Made Hermes YAML activation duplicate-aware, inline-map and quoted-key compatible, lossless for supported documents, fail-closed for malformed or unsupported constructs, and crash-safe through same-directory `fsync` plus atomic replace.
- Included old-memory vector delete events and successor upserts in Fact Evolution receipts, and added named fourth- and fifth-audit blocker stages to the release gate.
- Made every legacy update, archive, merge, and hard-delete path fail closed for fact-owned memories; structured fact changes now require the Fact Executor authority, while `sql_store.update_row()` remains transaction-neutral.
- Committed structured, quarantined, and legacy journal candidates as atomic connected closures derived from the same or overlapping source entries; later candidate failures now roll the whole closure back, source checkpoints advance only after every outcome is terminal, and legacy vector upserts are deferred until commit.
- Replaced broad lexical relation-family authorization with argument-preserving predicate frames, including prepositions and conservative CJK entity boundaries; ambiguous relation evidence is review-only with zero durable writes.
- Added a cross-process activation maintenance lease, cached-statement invalidation, pre-backup per-table SQLite DML guard triggers for raw/legacy writers, guard-free offline rollback snapshots, activation-owned epochs, and logical compensation preflight fingerprints; post-snapshot truth drift now stops compensation before any vector/plugin/config/database restore, retains every current surface, and returns a manual-recovery receipt. Successful commit removes guards before releasing the lease. Windows atomic config replacement no longer reports failure after replacement has already succeeded when directory `fsync` is unsupported.
- Made truncated relation scans fail before graph mutation, validated the full definition of the current-single-slot unique partial index, added a focused Windows Python 3.12 installer lane, and included all new adversarial cases in the blocking release gate.
- Rejected Reflection role swaps, polarity reversal, temporal-order reversal, dropped conditions/modality, quantifier drift, and historical-to-current drift even when lexical token coverage is complete.
- Forced memory-filtered current temporal queries to use the dedicated memory index, removing ledger-size-linear scans exposed by the 1M-row release benchmark.
- Prevented unsupported Reflection answers and observations from becoming durable review candidates, and prevented multiple memories derived from one provenance root from satisfying source-diversity gates.
- Added a release-identity gate that rejects reuse of an already published package version unless an explicit development-snapshot waiver is used for non-release verification.
- Made public durable update and merge operations acquire one `BEGIN IMMEDIATE` owner transaction before ownership reads; truth, FTS, relations, governance, and vector outbox intent now commit or roll back together.
- Made every SQLite truth insert/update atomically enqueue current-generation vector outbox intent from SQLite generation state rather than cached runtime state; capture replay runs only after commit while optional freshness remains observable and savepoint-isolated.
- Restricted durable fact authority to explicit current-state evidence; past, future, seasonal/historical, finite-range, fixed-duration, contract, transition-event, temporary, conditional, and uncertain clauses are review-only, including dotted month abbreviations and hyphenated duration quantifiers.
- Replaced process-global and ambient context activation authorization with an explicit token passed only to the installer-owned bootstrap connection; sibling threads, same-context ordinary connections, and ordinary providers cannot inherit write permission.
- Normalized copied staging directories to owner-readable/writable/executable modes so installation from immutable or read-only source trees can still complete atomic replacement and cleanup.
- Made runtime verification surface configuration load errors and made upgrades fail before backup/replacement when an existing READY vector generation lacks a bound physical preflight receipt.

## [1.7.2] - 2026-07-12

### Added
- Added immutable vector-generation manifests with compare-and-swap activation, migration receipts, durable replay outbox handling, and explicitly activated shadow builds.
- Added backend-agnostic vector storage, local SQLite brute-force fallback, optional PostgreSQL/pgvector support, and runtime backend selection for hybrid recall.
- Added an optional semantic candidate-extraction pipeline with strict policy gates, provenance-preserving candidate storage, and preview-first review/apply tooling.
- Added independent adversarial regression coverage for folded data URLs, structured secret-like metadata keys, freshness cohort integrity, config save/load symmetry, candidate concurrency, lifecycle explain parity, generation safety, and companion cleanup.

### Changed
- Unified ordinary-recall lifecycle policy so provisional and terminal-hidden rows are excluded from semantic merge, journal and nightly matching, nightly LLM context, exact insertion deduplication, maintenance deduplication, every vector mutation/replay path, migration, doctor accounting, and retrieval.
- Made vector-index repair inspect the active generation manifest by default while blocking in-place active-generation apply; legacy-root repair now requires an explicit operator flag and incompatible embedder spaces fail closed.
- Expanded read-only doctor and repair tooling for generation-aware SQLite/LanceDB consistency checks, hidden-vector debt, safe backups, and auditable receipts.

### Fixed
- Made positive Telegram identifier release scanning AST-aware for valid Python assignments, annotations, comparisons, mappings, allowlist collections, side-effect-free aliases, and split literals; JSON/TOML values are checked recursively, YAML lists are scanned across lines, unknown text uses bounded cross-line context, and synthetic exemptions are limited to explicitly marked test fixtures.
- Removed raw legacy generation paths from compatibility errors, sanitized and bounded all vector-startup exception messages, and limited system prompts to a bounded vector status code instead of detailed operator errors.
- Sanitized native-dependency probe output and bounded aggregated vector fallback diagnostics across internal status, operator stats, and warning logs.
- Added a subprocess native-dependency safety probe before doctor imports LanceDB/PyArrow in-process, preventing illegal-instruction crashes from unsafe wheels.
- Hardened archive and hard-delete flows with exact-ID scoping, vector-companion cleanup across active-generation and legacy roots, rollback recovery records, and truth-drift guards for repair apply.
- Allowed merge, dedupe, and nightly hard-delete flows to proceed when vector startup degraded before any companion generation existed, while continuing to require durable outbox intent for active, disabled, or repair-needed companions.
- Hardened automatic capture against folded inline data URLs while preserving surrounding prose.
- Added lifecycle-safe vector cleanup when candidate memories are archived, including fallback SQLite companion cleanup and repair-debt reporting.
- Removed folded/multiline data-URL payload continuations at the journal storage boundary while preserving surrounding user prose.
- Sanitized both mapping keys and values before browser output, governance audit persistence, all memory-metadata write paths (including nightly merge, lifecycle transition, and external imports), and freshness validator persistence, including collision-safe redacted keys, hashed import-source provenance, and preserved structured evidence identifiers.
- Based factual freshness numerator and denominator on the same active factual cohort and prevented non-zero eligible facts with incomplete coverage from reporting `ready`.
- Made runtime-config saves reuse load-time schema/type validation and use fsync-backed atomic replacement, rejecting invalid dotted updates as one operation.
- Made candidate conflict-query failures fail closed, protected bulk transitions with metadata/updated-at CAS, synchronized lifecycle and candidate status, and cleaned graph/vector companions across bulk and single candidate archive/supersede paths, including existing SQLite fallbacks.
- Made background writer failures, freshness-companion failures, candidate CLI output, and journal dry-run receipts observable and bounded without weakening SQLite truth durability.
- Redacted durable generation-manifest metadata/errors and migration-receipt details/errors at their authoritative storage helpers, including nested keys and values from direct callers that bypass higher-level runtime sanitization; health reports also sanitize legacy manifest metadata on output.
- Rejected absolute, Windows drive/UNC, and parent-traversal vector-generation storage paths before manifest persistence; health reports replace legacy invalid paths with an explicit safe marker.
- Replaced real-looking chat identity fixtures with reserved synthetic identifiers and made the release scanner reject unapproved positive and signed Telegram-style numeric IDs without echoing them.
- Scanned decoded text members in final wheel and sdist artifacts and made public packaging reject deployment-private source-isolation modules.
- Removed deployment-local counters from packaged historical release-readiness notes and made the release gate scan every versioned readiness document for private runtime state.
- Maintained release-gate coverage across forgetting, governance, journal recovery, dashboard, experience replay, installer rollback, fact freshness, relation extraction, and the golden benchmark for the 1.7.2 compatibility patch.

## [1.7.1] - 2026-07-08

### Fixed
- Kept runtime config diagnostics out of persisted operator config by filtering internal `_...` keys from both loaded config state and incoming dotted updates before writing `config.json`.
- Reported malformed runtime config through doctor/dashboard diagnostics instead of silently swallowing JSON/read errors, while keeping diagnostic fields read-only and non-persistent.
- Tightened candidate browser queries so processed event-digest rows marked promoted, archived, rejected, superseded, obsolete, or in-progress are not resurfaced as operator candidates.
- Made event-digest metadata redaction JSON-safe for nested dict/list/tuple/set/bytes/path/custom-object values before evidence packets reach candidate extraction or reports.
- Preserved cross-platform runtime-config tests by avoiding POSIX-only path suffix assertions.

### Changed
- Clarified external shared-memory bridge preview versus audit-writing receipt paths and retained read-only defaults for export inspection.
- Added hybrid/vector golden benchmark smoke coverage with `local-hash` and `sqlite-bruteforce` so release gates exercise semantic/vector recall paths without external credentials.
- Maintained release-gate coverage across forgetting, governance, journal recovery, dashboard, experience replay, installer rollback, fact freshness, relation extraction, and the golden benchmark while publishing this 1.7.1 patch.

## [1.7.0] - 2026-07-08

### Added
- Added event-digest evidence packets and reviewable candidate extraction with dry-run-first storage controls.
- Added read-only memory browser, candidate review commands, and humanized recall explain output for governance workflows.
- Added Experience-to-skill bridge helpers and replay-generation support for reusable operational playbooks, with experience replay coverage preserved in the release gate.
- Added vector backend abstraction updates, optional PGVector companion support, and vector backend operator documentation.
- Added external shared-memory export contract helpers, optional PostgreSQL bridge prototype, and explicit sensitivity governance for shared-memory payloads.

### Changed
- Event-derived candidates now reject unclassified generic chat instead of falling back to durable `memory/factual` proposals.
- Browser inspection redacts secret-like values and private paths by default; explicit `--raw` is required for local operator raw inspection.
- Release-gate checks now emit machine-readable progress on stderr and explicitly list the new productization modules, scripts, docs, and examples.
- Store recovery now rolls back dirty same-process peer providers that share the same SQLite truth DB before retrying a recoverable `database is locked` write.
- Maintained the stable V1 release line and release-gate coverage across forgetting, governance, journal recovery, dashboard reporting, installer rollback, fact freshness, relation extraction, and the golden benchmark while publishing the 1.7.0 productization feature set.

## [1.6.3] - 2026-07-07

### Fixed
- Closed the SQLite write-lock recovery gap from issue #25 by adding conservative `scope_recall_store` auto-recovery for recoverable SQLite lock/transaction errors: the provider rolls back/probes/reopens the shared connection if needed, retries the store once with identical arguments, and returns `recovered=true` plus `retry_count=1` in the receipt.
- Kept non-SQLite store failures non-retryable so business-logic exceptions still surface while rollback guards release any dirty SQLite transaction.
- Preserved forgetting, governance, journal recovery, dashboard, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark release-gate coverage while publishing this focused SQLite recovery patch.

## [1.6.2] - 2026-07-07

### Added
- Added `scripts/backfill.graph_relations.py`, a dry-run-by-default deterministic graph backfill that creates same-scope `supersedes` edges from trusted `metadata.superseded_by` provenance.
- Added `scripts/benchmark.graph_relations.py`, a deterministic API-free graph benchmark covering opt-in `supersedes` rerank improvement, hidden-peer leak prevention, and explicit zero relation weights; release readiness now runs it alongside the golden benchmark.
- Exposed graph density and hygiene counters in `scope_recall_stats`, including relation type distribution, orphan relation count, and lifecycle-hidden peer relation count.

### Changed
- Maintained the stable V1 release line and release-gate coverage across forgetting, governance, journal recovery, dashboard reporting, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark surfaces while publishing graph-relation and maintenance-tool hardening updates.

### Fixed
- Scope-filtered relation evidence in `scope_recall_inspect` and `scope_recall_explain` so graph relations never expose inaccessible, deleted, or lifecycle-hidden peer memory ids.
- Made explicit relation reranking symmetric for `supersedes` edges: enabling `retrieval.relation_rerank_enabled` boosts superseding memories and applies the configured `relation_superseded_penalty` to superseded peers while respecting explicit zero weights.
- Made `scope_recall_playbook_review` inspect-only by default for promote, quarantine, supersede, review, and merge write paths; operators must pass `dry_run=false` to apply DB mutations, and `force_cross_class` is documented and threaded through supersede/merge review flows.
- Made repeated `merge_playbooks()` apply calls idempotent when sources are already superseded by the selected target, avoiding duplicate `playbook_versions` rows and unnecessary `updated_at` churn.
- Classified LLM journal digest outputs filtered by quality gates as `filtered_or_rejected` through `candidate_status_counts`, keeping them observable in run metadata without routing non-error filtering into dead-letter handling.

## [1.6.1] - 2026-06-30

### Changed
- Published documentation, packaging, and release-provenance updates as a dedicated patch release after `v1.6.0` had already been tagged and published.
- Aligned public documentation and release metadata so the GitHub tag, package version, wheel, sdist, and PyPI release identify the same `1.6.1` source tree.
- Preserved the v1.6 product contract across forgetting, governance, journal recovery, dashboard, experience replay, installer rollback, fact freshness, relation extraction, and golden benchmark surfaces; this release does not introduce storage-schema or tool-surface changes.

### Fixed
- Fixed release provenance ambiguity by publishing the current release commit under a distinct `v1.6.1` tag instead of reusing `v1.6.0`.

## [1.6.0] - 2026-06-29

### Added
- Added production packaging and rollout surfaces: dry-run-by-default installer rollback/apply flows, operator runbooks, cross-profile rollout planning, response-contract documentation, and release-gate wheel/install/doctor smoke checks.
- Added governance cleanup, forgetting, and rollback tooling for soft-archive batches, including governance audit coverage reporting, default rollback support for `scope_recall_forget`, and transaction-bound audit inserts.
- Added journal recovery tooling for retry-exhausted/dead-letter entries, including replay scheduling, operator no-replay classification, dead-letter category reporting, and dashboard visibility.
- Added Experience Kernel productization: playbook bootstrap/search/inspect/feedback/review/promote tools, conservative auto-promotion quality gates, duplicate playbook reporting, supersede CLI review routing, and experience replay benchmarks.
- Added fact freshness scaffolding for durable factual memories, with dashboard coverage/staleness reporting and freshness-aware recall policy hooks.
- Added relation extraction and graph hygiene support for owned-by/affects/depends-on/supersedes/same-topic style edges, contradiction-safe edge generation, and repair/counting scripts.
- Added golden benchmark fixtures and release-gate execution for curated recall regression, including low-value scratch exclusion, archived-old-fact exclusion, and entity/project isolation cases.

### Changed
- Changed `scope_recall_forget` to soft archive by default with governance audit receipts and explicit rollback commands; hard delete is limited to maintenance flows.
- Changed delete/dedupe/nightly cleanup semantics to vector-first fail-closed behavior so SQLite truth is preserved when rebuildable vector companion cleanup fails.
- Changed vector repair to dry-run by default; writes now require explicit `--apply` or the `vector repair apply` CLI route.
- Changed recall/profile filtering so archived, superseded, rejected, candidate, and in-progress rows do not consume ordinary recall budget unless explicitly requested.
- Changed nightly digest and journal extraction paths to report fallback/dead-letter/quarantine status through doctor/dashboard instead of hiding opaque failures.
- Changed memory quality archive/reporting paths to distinguish active secret/pollution findings from archived historical rows.
- Split the scope-recall doctor into focused `doctor_*` modules while keeping `scripts/doctor.py` as the compatible CLI wrapper and preserving direct import re-exports used by tests/operators.
- Centralized graph hygiene repair/counting, maintenance dry-run helpers, digest result payload builders, recall pipeline merge/rank helpers, and provider schema construction into dedicated modules so future governance work has smaller review surfaces.

### Fixed
- Fixed governance audit transaction atomicity: `record_governance_audit_event()` is now a DDL-free INSERT helper, preventing sqlite `executescript()` from implicitly committing business updates before rollback/commit failure.
- Fixed soft-archive consistency when vector deletion succeeds but SQLite/entity/audit/commit later fails: SQLite is rolled back, the operation returns a failed receipt, and vector status is marked `needs_repair`.
- Fixed rollback reachability for `scope_recall_forget` archive batches by including that audit event type in default rollback candidates.
- Fixed top-level tool exception sanitization so fallback errors redact secret-like strings and local paths before returning to users.
- Fixed OpenAI-compatible hosted embeddings for OpenRouter-style backends by explicitly requesting `encoding_format="float"` from the OpenAI SDK (#24).
- Fixed SQLite provider initialization/bootstrap concurrency by opening the truth DB with a 10-second busy timeout instead of the Python sqlite default (#23).
- Hardened doctor runtime checks by opening the SQLite truth DB with URI `mode=ro` and by narrowing the doctor wrapper import fallback to `ImportError` so real import-time bugs are not hidden.
- Hardened release cleanup so the gate no longer removes repository-local `.venv` directories.

### Release verification
- Release artifacts are built only after the source tree passes the strict `scripts/check.release.py` gate in CI.
- Live-dashboard evidence in release-readiness documents is maintainer validation context, not a customer deployment health claim.

## [1.5.3] - 2026-06-26

### Added
- Added `scripts/repair.graph_hygiene.py`, a dry-run-by-default maintenance script that reports and, with `--apply`, removes orphan `memory_entities` / `memory_relations` rows from the rebuildable SQLite graph companion.
- Added `scripts/promote.memory_candidates.py`, a dry-run-by-default candidate-memory promotion planner/apply path that promotes safe ordinary `candidate` memories, optionally archives low-value noise with `--archive-noise`, and records governance audit events for applied mutations.
- Added doctor visibility for ordinary candidate-memory debt, including candidate count, age, target/source distribution, promotable rows, archive candidates, and samples so promoted-only profile behavior cannot silently starve on stale candidates.

### Changed
- `scope_recall_profile` now defaults SQLite rows to `lifecycle=promoted`; pass `include_candidates=true` to intentionally include non-hidden candidate rows while `include_general=true` remains the explicit switch for local scratch/general rows.
- Reduced the default primary-agent tool schema surface with a new `tool_schema_profile="compact"` default (6 tools, about 4.7 KB in repo-local measurement) that exposes core store/search/context/profile plus compact `scope_recall_memory` and `scope_recall_entity` dispatch tools; `tool_schema_profile="standard"` restores the legacy 20-tool read-only/diagnostic surface, and `tool_schema_extra_tools` can selectively expose diagnostics while staying compact.
- Kept the low-frequency `scope_recall_store_secret_index` schema behind `secret_index_tools_enabled=true`; direct calls also fail closed unless the operator explicitly enables it.

### Fixed
- Added lifecycle filtering to entity/profile graph read paths so `scope_recall_entity`, `probe`, `related`, and profile entity lookup hide `archived`, `superseded`, `obsolete`, and `rejected` memories consistently with the main recall path.
- Reduced deterministic entity-extraction noise from tool traces and filtered legacy noisy entity metadata/rows from graph read surfaces, including common tool tokens such as `read_file`, `search_files`, `execute_code`, `skill_view`, and `session_search`.
- Added a SQLite doctor graph-hygiene check that reports orphan graph companion rows and marks the runtime store as needing repair when they are present.
- Added a deterministic journal-digest durable-value gate so obvious webhook/notification/log/tool-summary noise is rejected before it can become durable `user`/`memory`/`project`/`ops` rows, while preserving reusable root-cause/fix/workflow candidates.
- Made `scripts/repair.vector_index.py` fail closed when the primary configured vector embedder is unavailable; operators must explicitly pass `--allow-fallback-embedder` before rebuilding with `vector.fallback_embedder`, and dry-run reports primary/fallback availability plus existing-vs-planned dimensions.
- Made maintenance dry-runs fail-safe: `scripts/repair.graph_hygiene.py` now accepts explicit `--dry-run`, `--dry-run` wins over accidental `--apply`, and candidate-promotion dry-run review output redacts secret-like text and private paths.

## [1.5.2] - 2026-06-25

### Added
- Added Recall Funnel traces for search/explain/benchmark paths, including candidate-pool sizing, per-stage candidate counts, filter counts, returned ids/chars, and retrieval timings.
- Added benchmark aggregate metrics for latency percentiles, known-answer recall, top-k accuracy, forbidden-id violations, filter counts, and optional prompt-budget hit rate.
- Added `scripts/benchmark.retrieval_regression.py`, an isolated synthetic benchmark that stress-tests lexical retrieval with distractor memories and Recall Funnel traces without requiring vector dependencies or API keys.

### Changed
- Added `retrieval.top_k` as the default tool result limit while preserving explicit per-call `limit` overrides.

### Fixed
- Made vector sync release tests use the deterministic `local-debug` embedder so release gates no longer depend on hosted embedding network availability.
- Synchronized `retrieval.top_k` across packaged `config.json` and in-code default config, exposed background journal digest health in `scope_recall_stats`, cached configured capture skip regexes to reduce per-turn filter overhead, and serialized vector companion mutations behind a provider-level lock.

## [1.5.1] - 2026-06-24

### Fixed
- Fixed strict release-gate dirty-tree checks in CI by ignoring known local/runtime scratch directories such as `.hermes-agent-src/` while still blocking real tracked or untracked source changes.

## [1.5.0] - 2026-06-24

### Added
- Added governance cleanup, journal recovery, operator dashboard, and repository-owned golden benchmark release-readiness tooling.
- Added golden benchmark cases to packaged artifacts and release metadata checks.

### Fixed
- Made `scripts/benchmark.golden.py` run in an isolated temporary Hermes home by default, copy the current plugin source for provider discovery, and keep any `--hermes-home` config read-only unless an explicit maintenance-only `--overwrite-config` flag is used with automatic backup/restore.
- Made release readiness run the golden benchmark and report dirty/untracked worktree state so new files cannot be missed before a release.
- Made hard-delete forgetting fail closed when no vector companion is provided, preventing SQL truth deletion that could leave stale vector hits.

## [1.4.5] - 2026-06-24

### Added
- Expanded `scope_recall_explain` so each returned row includes rank-aligned retrieval evidence for lexical/BM25/vector/RRF scores, metadata quality adjustment, entity overlap/distance bonuses, relation evidence/rerank contribution, memory-type temporal policy, temporal decay, recency bonus, threshold settings, and final score.
- Added rejected-candidate visibility to `scope_recall_explain`, including `rejected_count` and score-threshold rejection reasons for candidates filtered out before final ranking.
- Added assertion-case support to `scope_recall_benchmark`: cases can declare `expected_ids`, `forbidden_ids`, `min_rank`, `min_top_score`, and `auto_explain_on_fail` while preserving the legacy `queries` latency-smoke mode.
- Added benchmark regression cases and a CI/type-check matrix covering full extras, sqlite-only/native-free paths, missing optional jieba, shared-pool configuration, and pyright checks.
- Added memory-type-aware temporal policy so durable facts/preferences/procedures decay less aggressively than episodic or temporary evidence, with policy class/weight surfaced in explain.
- Added persisted `memory_relations` evidence to recall/explain and feature-gated relation-aware reranking through `retrieval.relation_rerank_enabled`.
- Added explicit `shared_pool` write policy: the pool remains read-only by default, `scope_mode="shared_pool"` writes require `shared_pool.write_enabled=true`, and writes are limited to configured durable targets.

### Fixed
- Made `scope_recall_update` re-run deterministic conflict/relation review after content or target changes so updates receive the same contradiction evidence as newly stored memories.
- Preserved accumulated feedback metadata during updates, including feedback counts, feedback-adjusted trust, conflict-review fields, and higher existing importance scores.
- Fixed journal digest skip/covered-candidate paths so filtered or already-covered candidates still advance the processed watermark instead of leaving permanent backlog.
- Fixed `scope_recall_forgetting_run` soft-archive persistence and hard-delete vector consistency, including vector record deletion and relation cleanup.
- Kept conflict-review metadata in sync on peer memories when related rows are deleted.
- Prevented heuristic journal digest from producing template/transcript-shaped durable memories such as `Operations workflow summary`, `Journal digest memory`, `user:`, or `assistant:` wrappers.
- Prevented low-signal Experience playbooks such as “继续”, “进度如何”, and fixed reply smoke tests from being auto-created as reusable procedures.
- Fixed explicit `scope_mode` handling so `local`, `shared`, and `shared_pool` writes are respected, semantic merge stays inside the selected scope, and shared-pool rows can be updated/merged when write-enabled.

## [1.4.4] - 2026-06-23

### Added
- Added `docs/contract.matrix.md`, a maintainer gate matrix that maps each major scope-recall contract to source files, targeted tests, release gates, and dynamic probes so large-context changes do not rely on an agent remembering the whole plugin.

### Fixed
- Made the SQLite brute-force vector companion safe to use from background journal/digest threads by opening the connection with `check_same_thread=False`, serializing access with a local lock, and closing/reopening the companion cleanly when `setup_vector_layer()` is rerun after a `needs_repair` state.
- Skipped `session_messages` tool dumps in session-end tool-trace journaling so current-session MCP readbacks cannot be restaged as memory-provider evidence.
- Enabled the native-safe `sqlite-bruteforce` vector fallback by default when LanceDB/PyArrow are absent or unsafe on non-AVX hosts.
- Bootstrapped the empty SQLite truth/journal schema and sqlite-bruteforce `vector_meta` records during `hermes memory setup` config saves so operators can verify installation before the first live message lazily initializes the provider.

## [1.4.3] - 2026-06-20

This is the first public release after `v1.4.0`; the GitHub release notes for `v1.4.3` include the cumulative `v1.4.1`, `v1.4.2`, and `v1.4.3` changes.

### Changed
- Defaulted `experience.auto_promote_low_risk` to `false` so automatic Experience scans create candidate playbooks unless low-risk auto-promotion is explicitly enabled.

### Fixed
- Blocked Experience auto-promotion for final-failure or incomplete task traces even when earlier logs contain `passed`/`ok` success tokens.
- Tightened final-failure detection to avoid false positives from words such as `cannot`, `no errors`, or `redacted`.
- Nightly digest now records `ok_with_fallback` and `extractor_used=heuristic-fallback` when LLM output is empty, unparsable, or filtered out before heuristic fallback writes candidates.
- Preserved already parsed LLM candidates when a later chunk explicitly returns `action=skip`, and continued parsing later chunks when an earlier chunk returns `action=skip`.
- Marked LLM fallback runs as `error` when heuristic fallback also produces no candidates.
- Made the optional legacy `memory-lancedb-pro` migration importer load LanceDB lazily. This importer is only used when importing existing OpenClaw memory stores into scope-recall; normal Hermes runtime, tests, and non-import workflows do not require OpenClaw or LanceDB.

## [1.4.2] - 2026-06-20

- Clarified Experience Kernel runtime docs so default prefetch and operator-enabled automatic promotion are described as separate controls.
- Added doctor visibility for nightly digest health, including latest status, recent fallback/error rows, and consecutive failure counts.
- Added release regression coverage for the Experience docs/schema promotion contract and nightly digest doctor reporting.

## [1.4.1] - 2026-06-19

### Changed
- Kept Experience preflight packet injection enabled by default but made background reusable-experience promotion opt-in (`experience.auto_promotion_enabled=false`) until the review queue has enough field feedback.
- Nightly digest runs that fall back from LLM extraction to heuristic extraction now record `ok_with_fallback` instead of plain `ok`, preserving success while making degraded provider health visible.

### Fixed
- Hardened report/evidence surfaces so session-end tool capture stores safe summaries by default, tool JSON errors redact local paths, journal rejections/errors, feedback notes, hygiene/forgetting previews, and Experience evidence use a shared report sanitizer for secrets, private paths, attachment markers, and raw tool traces.
- Made release-gate sentence-transformers coverage deterministic by mocking local encoder behavior in default tests and moving real HF model loading behind an explicit `SCOPE_RECALL_RUN_SENTENCE_TRANSFORMERS_INTEGRATION=1` integration test, preventing release readiness from depending on network/cache/GPU state.
- Preserved manual Skill governance anchors during Experience playbook anchor sync/backfill; source-managed related-skill anchors are now inserted only when missing instead of deleting and rebuilding all anchors for a playbook.
- Wired `experience.auto_promotion_enabled` into successful background/session-end journal digest runs so automatic reusable-experience promotion can run without manually calling `scope_recall_experience_promote`.
- Added Skill anchor/conflict enforcement for Experience Playbooks: promoted playbooks write `skill_anchors`, startup backfills anchors for existing promoted playbooks with `related_skills`, open conflicts force `no_reuse`, missing anchors degrade direct reuse to guided reuse, and stale/misleading feedback opens Skill conflict records.

## [1.4.0] - 2026-06-17

### Added
- Added the conservative Experience Kernel MVP: procedural playbook schema/tables, deterministic `procedural_playbook.v1` validation with per-step `capability_class`, scope-filtered playbook create/search/inspect/preflight/review/feedback/stats tools, feedback run counters, bounded preflight packet rendering controlled by `experience.prefetch_enabled`, doctor visibility for Experience tables, and a read-only `scripts/experience-replay.py` benchmark for comparing baseline coverage against Experience packets.
- Hardened the Experience Kernel MVP so `experience.enabled=false` is a global kill switch, create can only write `candidate`, promotion requires review, secret-like playbook/feedback text is rejected before persistence, legacy secret-like rows are redacted before tool/preflight output, corrupt core playbook JSON fails closed, `reuse_policy` is enforced before direct reuse, shared-scope feedback cannot demote global playbooks, terminal playbook statuses reject feedback, and CJK queries are not misclassified by whitespace-only low-signal checks.
- Added the first automatic reusable-experience loop: `scope_recall_experience_promote` scans evidence-backed journal task traces, writes `task_episodes`, creates reusable experience handbooks, auto-promotes low-risk verified handbooks, and keeps high-risk handbooks in `needs_review` for later agent/operator review instead of requiring end users to manually inspect raw memory rows.
- Added the first forgetting loop: `scope_recall_forgetting_report` and `scope_recall_forgetting_run` identify duplicate, scratch, tiny, wrapper-noise, and secret-like memory rows; the default action is soft archive via metadata, with hard delete reserved for explicit hard-delete candidates.
- Added journal backlog observability to `scripts/doctor.py`, including unprocessed role distribution, oldest backlog age, attachment/path contamination counts, configurable warn/fail thresholds, and operator recommendations for digest throughput and tool-trace hygiene.

### Changed
- Experience runtime injection is now enabled by default in the current source candidate through `experience.prefetch_enabled=true`; set `experience.prefetch_enabled=false` to keep runtime injection silent while exposing read-only playbook search/inspect/preflight/stats and scoped feedback tools for operator-guided reuse.
- Journal digest now dynamically raises the per-run entry limit when backlog exceeds the configured threshold, capped by `journal.max_entries_per_digest_ceiling`, so old queues can drain without permanently over-provisioning normal runs.

### Fixed
- Sanitized session-end tool traces with the same `sanitize_capture_text()` / `should_capture_text()` path used for user and assistant capture, preventing image attachment markers, `image_cache/img_*` paths, secret-like text, and low-value tool dumps from entering new journal rows.
- Classified failed LLM journal digest batches as `retry-exhausted:<kind>` or `dead-letter:<kind>` in journal rejections and run metadata, preserving retry/dead-letter evidence instead of leaving opaque quarantine rows.
- Redacted raw and partially masked provider key strings from journal digest quarantine error messages before storing rejection snippets or run metadata.

## [1.3.0] - 2026-06-14

### Added
- Added `scope_recall_profile`, a compact high-level profile/context surface over accessible durable `user`/`memory`/`project`/`ops` rows, optional local `general` scratch, and live Hermes curated `USER.md`/`MEMORY.md` entries.
- Added regression coverage proving the profile surface is registered as a provider tool, live-reads curated memory without copying it into SQLite, preserves gateway user isolation, recalls durable rows across sessions for the same user, and excludes local `general` scratch unless requested.

### Changed
- Documented why this is a minor release: it adds a new public tool/API surface without breaking the V1 storage or runtime compatibility contract.

## [1.2.1] - 2026-06-14

### Fixed
- Preserved surrounding user text when gateway image attachment markers or local `image_cache/img_*` paths appear inline rather than on their own line, while still stripping the attachment metadata before journal/capture storage.
- Added regression coverage for inline attachment marker sanitization so pre-compression journal staging cannot silently drop the user's actual sentence.

## [1.2.0] - 2026-06-14

### Added
- Added `ScopeRecallMemoryProvider.on_pre_compress()` so Hermes context-compression boundaries stage sanitized user/assistant messages into the journal before old turns are summarized/discarded.
- Added regression coverage proving pre-compression staging strips image attachment metadata, filters wrappers/tool output/secret-like text/trivial acknowledgements, and never writes raw compression-boundary content directly into durable memory.

### Changed
- Relaxed vector stats regression coverage to accept the designed `sqlite-bruteforce` fallback when LanceDB/PyArrow is unavailable or unsafe, while still requiring a ready vector companion and fallback evidence.

## [1.1.2] - 2026-06-14

### Fixed
- Sanitized gateway image attachment markers before capture/journal storage, removing local `image_cache/img_*` paths and inline image placeholders while preserving the user's surrounding text.
- Added regression coverage so screenshot-only payloads are rejected as empty and screenshot questions are journaled without local image paths.

## [1.1.1] - 2026-06-14

### Fixed
- Treated short assistant acknowledgement messages such as `Understood.`, `Noted.`, and common Chinese ACKs as trivial capture input so they cannot enter the journal.
- Prevented assistant-only journal rows from being promoted by heuristic or LLM journal digest, including legacy rows created before the ACK filter.
- Added memory-quality regression tests proving assistant-only acknowledgements are skipped rather than becoming durable memories.

## [1.1.0] - 2026-06-14

### Added
- Added the `hermes-scope-recall` standalone distribution shape with a `hermes-scope-recall` console script.
- Added `hermes-scope-recall install` to copy the provider into `$HERMES_HOME/plugins/scope-recall/` without touching provider-owned data under `$HERMES_HOME/scope-recall/`.
- Added `hermes-scope-recall verify` plus installer tests covering dry-run, forced replacement safety, Hermes memory-provider discovery, and CLI round trips.

### Changed
- Renamed the Python distribution package from `scope-recall` to `hermes-scope-recall` while preserving the Hermes provider ID `scope-recall` and Python import package `scope_recall`.
- Packaged plugin metadata, docs, and operator scripts inside the wheel package so the installer can register a complete unpacked Hermes provider from site-packages.
- Updated README install guidance for the supported standalone-provider path proposed for Hermes upstream documentation.

## [1.0.16] - 2026-06-14

### Fixed
- Probed LanceDB/PyArrow native imports in a child process before importing them inside Hermes, so no-AVX/AVX2 hosts that hit `Illegal instruction` are treated as unsupported instead of crashing the agent process.
- Added automatic `sqlite-bruteforce` vector fallback when the configured LanceDB companion is absent or unsafe and `vector.fallback_backend=sqlite-bruteforce` is set.

### Changed
- Added `vector.fallback_backend` to the default config and setup schema.
- Documented the native-safe vector path for non-AVX hosts and bumped package, plugin, release-check metadata, README, and stability docs to `1.0.16`.

## [1.0.15] - 2026-06-13

### Fixed
- Reused one chat-completions endpoint builder across capture, journal, and nightly digest paths so provider-specific endpoints and `append_v1=false` are honored consistently.
- Redacted sensitive HTTP/SSE error bodies before provider exceptions surface from Codex responses or streaming response parsing.
- Kept pure `role=tool` journal traces in provenance only; heuristic digest no longer promotes raw tool output into durable memory.
- Changed empty-store nightly scope inference to use an explicit or CLI fallback instead of silently defaulting to Telegram.
- Split readable aliases from writable scopes so legacy cross-platform platform scopes remain read-only unless an explicit migration writes them.
- Preserved the updated row's real `scope_id` when nightly digest updates vectors for legacy rows.
- Redacted secret scanner findings in the release gate while still reporting file, line, and rule evidence.

### Changed
- Added regression coverage for the v1.0.15 audit findings and updated the provider tool-trace test to assert journal-only provenance behavior.
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.15`.

## [1.0.14] - 2026-06-13

### Added
- Added opt-in canonical identity mapping for cross-platform durable recall. When `identity.cross_platform_shared_scope=true` and explicit `identity.user_aliases` map platform accounts to one canonical user, `user`/`memory`/`project`/`ops` rows share a canonical durable scope while `general` scratch remains local to the platform/account/chat/session scope.
- Added query-time compatibility for legacy platform-specific durable shared scopes so mapped identities can still read existing rows before any explicit migration.
- Added digest transport controls for provider-specific OpenAI-compatible endpoints: `endpoint` / `chat_endpoint` and `append_v1=false`, including CLI support for `scripts/nightly-digest.py --endpoint` and `--no-append-v1`.
- Added regression coverage for default isolation, unmapped-account isolation, mapped durable sharing, scratch non-sharing, legacy shared-scope aliases, endpoint construction, and redacted provider HTTP errors.

### Fixed
- Fixed journal/nightly digest chat-completions calls that incorrectly forced `/v1/chat/completions` onto provider-specific roots such as Ark Coding Plan.
- Fixed maintenance tool schema registration so `maintenance_tools_enabled=true` is visible before provider `initialize()`, matching Hermes tool registration order.
- Preserved built-in curated memory default behavior for CLI sessions without an explicit user id while still allowing configured `cli_user_id_fallback` for canonical identity mapping.

### Changed
- Newly written provider, journal digest, and nightly digest rows include audit metadata for `raw_platform`, `raw_user_id`, and, when mapped, `canonical_user` / `scope_identity_mode`.
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.14`.

## [1.0.13] - 2026-06-12

### Added
- Added lifecycle-aware conflict review: newly inserted contradictory durable memories now record bidirectional `contradicts` relations plus `needs_conflict_review` metadata without automatically superseding or hiding older rows.
- Added governance review candidates for local scratch rows, conflict-review rows, superseded/obsolete/rejected lifecycle rows, raw turn-source rows, low-confidence rows, and archive candidates so historical dirty data can be reviewed without automatic deletion.
- Added `scripts/migrate.legacy_hygiene.py`, a dry-run-first legacy hygiene migrator that backs up SQLite truth, archives historical `general`/raw/scratch rows without deleting content, and normalizes missing durable lifecycle/category metadata.
- Added regression coverage proving automatic conflict detection does not hide older rows, exact-id forget behavior matches docs, lifecycle metadata survives governance runs, dirty-history candidates are reported for operator review, LLM digest retries transient failures before quarantine, and legacy hygiene migration is backup-backed and read-only by default.

### Changed
- Recall still suppresses explicitly `superseded`, `obsolete`, `rejected`, and now `archived` rows by default, but automatic contradiction detection no longer writes `lifecycle=superseded`; operators must use explicit update/merge/delete actions after review.
- Journal LLM digest now classifies provider failures and retries transient timeout/rate-limit/network/server errors before quarantining; auth/quota/parse failures fail closed without wasteful retry loops.
- Governance classification now preserves existing lifecycle and conflict-review metadata instead of overwriting it with a fresh generic classification.
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.13`.

## [1.0.12] - 2026-06-12

### Added
- Added journal-first provenance capture with `journal_entries`, `journal_digest_runs`, and `memory_journal_sources` tables. Eligible turn text is staged as provenance instead of being written directly as durable recall memory.
- Added `scripts/journal-digest.py`, a background digest entrypoint that groups related journal turns, creates high-density memory candidates, merge-upserts existing rows, links source journal evidence, and syncs the configured vector companion only for durable memory rows.
- Added weighted reciprocal-rank fusion (RRF) and entity-distance scoring primitives so lexical, vector, BM25, curated-memory, and entity-neighborhood signals can be combined without trusting incompatible raw score scales.
- Added regression coverage for journal/provenance storage, provider long-turn chunking, digest evidence links, same-topic merge/upsert behavior, LLM-first extractor defaults, non-silent LLM failure handling, background digest scheduling, doctor `.env` isolation, RRF promotion of cross-signal hits, and entity-distance reranking.

### Changed
- `sync_turn()` now defaults to journal-first staging and routes long eligible turns into the journal chunking path instead of dropping them at the outer capture-length gate. Legacy per-turn regex durable extraction is explicitly gated behind `per_turn_extraction.enabled=false` by default, and raw user fallback remains disabled by default.
- `on_session_end()` now captures compact tool execution traces into journal provenance; synchronous durable promotion is not the default, and LLM session-end digest requires explicit `journal.allow_session_end_llm=true`.
- Journal digest now defaults to LLM-first extraction, groups by conversation session/topic, runs from a non-blocking background scheduler controlled by `journal.digest_interval_hours`, honors `journal.max_entries_per_digest`, records skipped candidates in `journal_rejections`, preserves provenance by default (`retention_days=0`), and requires explicit `journal.allow_heuristic_fallback=true` or `--extractor heuristic` before degraded heuristic fallback can consume journal evidence.
- Hybrid retrieval now includes bounded BM25 final-score contribution and RRF metadata blending while preserving current-turn recall, scope isolation, and lexical/vector fallback behavior.
- Bumped package, plugin, release-check metadata, README, DESIGN, and stability docs to `1.0.12`.

### Fixed
- Fixed unrelated journal tasks over-merging through a global `scope-recall` bucket, while preserving same-session merge/upsert behavior for continuing work.
- Fixed `scope_recall_forget`/dedupe deletion leaving orphan `memory_journal_sources` provenance rows.
- Extended `scripts/doctor.py` to validate journal/provenance schema, backlog, digest run, rejection, and orphan-link health without leaking profile `.env` values into process-global `os.environ`.

## [1.0.11] - 2026-06-11

### Added
- Added a `MiniMaxEmbedder` (provider: `minimax`) and a `build_embedder` route for the MiniMax `embo-01` embedding endpoint. The endpoint is non-OpenAI-compatible (`texts` plural, `type: "db" | "query"`, `vectors` reply), so the embedder talks to it directly via `urllib`.
- Added MiniMax document/query request-type separation: vector indexing/upserts use `db`, while vector search uses `query` through the embedder query path.
- Added optional MiniMax `GroupId` support for accounts that still require it, with `group_id` / `group_id_env` configuration.

### Changed
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.11`.

## [1.0.10] - 2026-06-10

### Added
- Added deterministic external-artifact enrichment for direct memory writes and nightly digest candidates. GitHub issues, PRs, commits, releases, repositories, and URLs now get a human-readable `Artifact anchors:` block plus structured `artifacts` metadata, derived entities, and tags.
- Added `scope_recall_store_secret_index`, an explicit credential-index tool that stores searchable service/account/purpose/vault-reference metadata without storing plaintext secret values in SQLite, FTS, vector text, exports, logs, or chat replies.
- Added regression coverage for direct GitHub issue anchors, nightly digest artifact preservation, and secret-index export hygiene.

### Changed
- Bumped package, plugin, README, stability contract, and release-check metadata to `1.0.10`.
- Updated project URLs to the Hermes-specific repository slug `410979729/scope-recall-hermes` while keeping the runtime package and plugin ID as `scope-recall`.
- Strengthened nightly digest extraction instructions so external artifacts retain repo/name, issue/PR/release/commit identifiers, exact URLs, and available status/date/author/next-step anchors.

### Fixed
- Fixed vague memory records that mentioned external work without durable lookup anchors, forcing later sessions to rediscover issue/PR/release URLs from scratch.
- Fixed a secret-index false positive where multiline credential metadata such as a label ending in `credential` followed by `Kind: api_key` could be rejected as `secret-like-content` even though no plaintext secret was stored.

## [1.0.9] - 2026-06-09

### Added
- Added the `sqlite-bruteforce` vector backend for non-AVX or native-dependency-sensitive hosts. It stores rebuildable vector companion rows in `$HERMES_HOME/scope-recall/vector.sqlite3` while keeping `$HERMES_HOME/scope-recall/memory.sqlite3` as the truth source.
- Added `docs/naming.md` to define the public `scope-recall` spelling versus Python/tool/config identifiers that use `scope_recall`.
- Added `docs/upstream-recommendation.md` with the standalone-provider checklist and Hermes upstream recommendation route.
- Added regression coverage for native-free vector imports, `sqlite-bruteforce` runtime sync/search, doctor reporting, and repair-script rebuilds.

### Changed
- Moved `lancedb`/`pyarrow` to the `lancedb` optional dependency extra. Default package import no longer requires native vector dependencies, while CI and LanceDB installs use `.[lancedb]`.
- Extended `vector.backend` configuration, runtime dispatch, doctor diagnostics, release checks, and repair tooling to cover both `lancedb` and `sqlite-bruteforce` companions.
- Updated installation docs to distinguish the recommended LanceDB path from the native-free SQLite fallback path.

### Fixed
- Fixed the no-AVX/native-import failure mode where importing vector runtime modules could fail before the operator had a chance to select a safer backend.
- Fixed the #4 naming ambiguity by documenting where each spelling is authoritative instead of performing a risky whole-repository rename.

## [1.0.8] - 2026-06-03

### Added
- Added deterministic Chinese entity fallback hints so compound input-method terms such as `自然码` and `双拼` are extracted even when Jieba is unavailable or segments differently in CI/runtime environments.
- Added `docs/external-shared-memory.md` to document safe bridge boundaries for deployments with a central shared backend such as PostgreSQL.

### Changed
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.8`.
- Reworded the V1 scope documentation around the positive architecture: local-first recall, SQLite truth storage, LanceDB companion retrieval, explicit bridge boundaries for external shared backends, Hermes-native skill ownership for procedural knowledge, and deployment-driven observability.
- Included the external shared-memory integration document in release-gate source and wheel checks.

### Fixed
- Fixed the GitHub Actions regression where the Chinese entity test could fail because `自然码` was not extracted when Jieba was not installed or did not split the compound phrase as expected.

## [1.0.7] - 2026-06-03

### Added
- Added `scripts/doctor.py`, a read-only source/runtime health report that checks release metadata alignment, SQLite truth availability, LanceDB companion readability, and repair recommendations.
- Added BM25 as an optional final-score component for hybrid retrieval, while preserving candidate-local SQLite FTS5 `bm25()` normalization and raw-score metadata for explainability.
- Added optional Jieba-backed Chinese entity extraction and broader code-ish entity extraction for mixed Chinese/English project memory.
- Added explicit temporal-decay scoring, deterministic source-trust priors, typed `memory_relations`, and conservative contradiction marking with feedback/metadata evidence.
- Added opt-in shared-pool scope stats plus `scope_recall_inspect`, `scope_recall_explain`, and `scope_recall_benchmark` observability tools.

### Changed
- Bumped package, plugin, release-check metadata, README, and stability docs to `1.0.7`.
- Extended the release gate stable-tool check to cover the full public V1 default tool surface and new observability tools.

### Fixed
- Aligned the README public version text with package/plugin metadata and documented the Hermes venv + `PYTHONPATH` test command so plain `pytest` from an unrelated environment is not mistaken for release evidence.
- Preserved pure lexical recall in default hybrid mode when BM25 metadata exists but `bm25_weight` is still zero, avoiding accidental dampening of local/general matches.
- Reduced generic English entity noise so related-entity results keep explicit caller-provided agent identities visible.

## [1.0.6] - 2026-06-01

### Added
- Added `capture_llm` module: LLM-powered semantic extraction of user+assistant turns into classified durable memory (preference, workflow, pitfall, decision, etc.) with user-configurable model and endpoint.
- Added `capture_llm` configuration block (`capture_llm.enabled`, `capture_llm.model`, `capture_llm.base_url`, etc.) with safe defaults (disabled by default, requires API key).
- LLM extraction runs in `sync_turn` before legacy regex extraction; if LLM succeeds, regex and raw-user fallback are skipped to avoid noise.
- LLM extraction preserves entity and tag metadata on stored candidates for better recall targeting.

### Changed
- `sync_turn` now has a four-tier capture pipeline: LLM semantic extraction → regex extraction → raw user capture → raw assistant capture (legacy).
- Bumped package, plugin, and release-check metadata to `1.0.6`.
- Synced public README/stability/OpenClaw comparison wording with the v1.0.4/v1.0.5 entity, feedback, and nightly digest features.
- Extended the public `scope_recall_store` tool schema `memory_type` enum to include workflow-oriented digest types already accepted by the governance layer.

## [1.0.5] - 2026-06-01

### Added
- Added `scripts/nightly-digest.py`, a profile-scoped daily conversation digest that reads Hermes `state.db`/legacy `lcm.db`, extracts durable memories, writes through the SQLite truth store, syncs the LanceDB companion when enabled, and records digest run/source ledgers.
- Added task-session workflow extraction so successful tool-heavy work can be retained as reusable `workflow`/tool-chain memory without storing raw tool or system output.
- Added digest safeguards for secret redaction, task-vs-normal session classification, dry-run planning, exact duplicate cleanup, and semantic skip/update/insert decisions against existing scope-recall rows.
- Added regression coverage for nightly digest session loading, sensitive-value redaction, workflow memory writes, digest ledgers, duplicate skips, and dry-run no-write behavior.

### Changed
- Bumped package and plugin metadata to `1.0.5`.
- Extended accepted `memory_type` values with workflow-oriented digest types such as `workflow`, `tool_trace`, `summary`, `pitfall`, and `decision`.

## [1.0.4] - 2026-05-31

### Added
- Added a local SQLite graph layer with `memory_entities` and `memory_feedback` tables.
- Added deterministic entity extraction and backfill for existing SQLite truth rows.
- Added `scope_recall_context`, `scope_recall_probe`, `scope_recall_related`, and `scope_recall_feedback` tools.
- Added memory type, importance, trust, entity, and tag metadata support for explicit `scope_recall_store` calls.
- Added recall ranking support for metadata quality and entity overlap while preserving lexical/vector gates.
- Added BM25 ordering for SQLite FTS5 candidates before recency tie-breaking, so older exact lexical matches are not cut from the candidate pool by newer weak hits.
- Added regression coverage for entity probe, related lookup, compact context rendering, feedback trust updates, and stats.

### Changed
- Bumped package and plugin metadata to `1.0.4`.
- Extended stats with scoped entity and feedback counts.
- Made `retrieval.candidate_pool` apply inside SQLite lexical candidate selection.

### Fixed
- Reject generic `[System note: ...]` gateway/runtime wrappers, interrupted-turn recovery prompts, and preserved task-list wrappers before they can enter automatic capture or manual write surfaces.
- Added regression coverage for the stale restored-message failure mode where an interrupted-turn system note could preserve an older user request and contaminate recall.
- Tightened hybrid vector-only automatic recall so mid-confidence semantic-neighbor drift does not inject unrelated durable memories when there is no lexical evidence.
- Added regression coverage for length-framed scope identifiers so delimiter-bearing `user_id` values cannot collide with split `user_id` + `chat_id` scope components.
- Added regression coverage for operator `scope_recall_dedupe(scope_only=false)` to ensure cross-scope duplicate cleanup matches the documented maintenance-tool semantics.

### Changed
- Refined the operator dedupe regression so it creates duplicate fixture rows through the provider write path while keeping vector sync disabled for deterministic storage-only setup.
- Reworded DESIGN operational follow-up from reviewer-specific cleanup into public deployment guidance.

## [1.0.3] - 2026-05-20

### Added
- Added structured memory classification metadata for new writes, including category, tier, kind, lifecycle, authority, confidence, sensitivity, expiry, entity, tag, and scope-mode fields.
- Added FTS hygiene repair coverage so missing, stale, or duplicate SQLite FTS rows are detected and repaired deterministically.
- Added hygiene-report coverage for structured metadata presence and release-time regression coverage for the expanded governance layer.

### Changed
- Isolated the default Gemini embedding credential to `SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY`, avoiding accidental reuse of general OpenAI or Google API keys.
- Kept the OpenAI-compatible Gemini endpoint as the hosted default while retaining `local-hash` as the no-credential fallback.

## [1.0.2] - 2026-05-18

### Added
- Added `capture_filters.py` to centralize automatic capture hygiene and block runtime-wrapper text such as recent Telegram context, context-compaction handoffs, skill-review meta prompts, and secret-like literals before they enter SQLite or vector storage.
- Added regression coverage for capture filtering, structured content capture, context-wrapper rejection, and default assistant-response non-capture.
- Added storage receipts to `scope_recall_store`, `scope_recall_update`, and successful `scope_recall_merge` responses so governance companions can close promotion/merge/rejection loops against concrete write evidence.
- Added conservative curated-memory policy controls: global `USER.md` / `MEMORY.md` recall now requires opt-in for explicit gateway `user_id` contexts unless an allowlist/profile-global mode is configured.
- Added stable OpenClaw import fingerprint material for missing/invalid legacy timestamps so dry-run/import reruns remain idempotent.

### Changed
- Changed default automatic capture posture to reduce raw `general` noise: `capture_assistant=false`, `min_capture_length=40`, and `capture_hard_max_chars=2500`.
- Kept short extracted durable candidates eligible for capture even when raw-turn capture uses a higher minimum length, so concise user preferences and ops facts are not lost.
- Treat exact semantic-merge matches as duplicates rather than no-op merges, preserving existing memory ids without rewriting content.

## [1.0.1] - 2026-05-16

### Security
- Scoped all ID-based write paths (`scope_recall_update`, `scope_recall_merge`, query-driven delete plumbing, and dedupe deletes) to the current accessible scope set so a caller that learns an inaccessible memory id cannot update, merge, or delete that row from a different user, sibling agent, or local chat/thread/session scratch scope. Ordinary merge calls now fail if any requested source id is missing or inaccessible, including explicit-content merges that would otherwise silently overwrite the target. Ordinary update/merge calls now also reject shared/local mode changes, preventing durable rows from becoming cross-window `general` scratch or local merges from swallowing shared durable memory.
- Restricted maintenance tools behind explicit `maintenance_tools_enabled=true`. `scope_recall_dedupe`, `scope_recall_govern`, and `scope_recall_repair` are hidden from the default tool schema and fail closed unless operator mode is enabled; `scope_recall_export(scope_only=false)` also requires operator mode.
- Changed `scope_recall_dedupe` default behavior to current-scope-only. Cross-scope dedupe remains available only as an operator maintenance action.

### Changed
- Reframed the scope model as permanent shared memory plus local scratch scope: durable `user`/`memory`/`project`/`ops` rows follow the same user + agent identity across windows/chats, while `general` rows stay local.
- Aligned package metadata, plugin metadata, release checker, README, stability contract, and design docs with the public `v1.0.1` tag.
- Added `CONTRIBUTING.md` to verified wheel data files so installed release docs match the README documentation table.

## [1.0.0] - 2026-05-15

### Added
- Declared the first stable V1 release line with explicit provider identity, storage, tool, retrieval, migration, and runtime-freshness contracts in `docs/stability.md`.
- Added V1-grade release checks for stable metadata, required documentation, wheel contents, and public-facing version consistency.
- Kept release-tree scanning focused on `scope-recall` sources when CI clones Hermes into `.hermes-agent-src` for runtime compatibility tests.
- Added a public README structure with badges, quick start, architecture diagram, tool quick reference, troubleshooting notes, and release-gate guidance.

### Changed
- Promoted package and plugin metadata from `0.2.0` to `1.0.0`, while keeping the public package classifier at beta/release-candidate maturity until broader field use.
- Aligned the public Python support floor and CI matrix with the current Hermes runtime requirement of Python 3.11+.
- Tightened V1 documentation around SQLite truth ownership, LanceDB companion-cache rebuildability, and OpenClaw migration/compatibility boundaries.
- Changed GitHub Actions to run `scripts/check.release.py` as the remote CI gate so CI matches the local V1 release audit.
- Replaced agent-specific author/copyright wording with project contributor wording and added `SECURITY.md` plus a `py.typed` marker for public-release hygiene.
- Fixed scope id serialization to avoid delimiter-collision between user/chat/thread/session components and aligned `scope_recall_dedupe(scope_only=false)` with its documented cross-scope semantics.

## [0.2.0] - 2026-05-12

### Added
- Added vector audit stats for physical LanceDB row count, unique id count, and duplicate extra row count.
- Added regression coverage for duplicate vector row repair, stale vector row cleanup, vector upsert failure degradation, light top-level package import, and the intentional `on_memory_write` no-op boundary.
- Renamed public provider from `lancepro` to `scope-recall` with a deprecated compatibility shim left in place for the old plugin directory.
- Added SQLite truth store + LanceDB vector companion architecture for hybrid current-turn recall.
- Added scope isolation coverage for `chat_id`, `thread_id`, and `gateway_session_key`.
- Added focused release docs: migration notes, upstream differences, and OpenClaw import guidance.
- Added idempotent OpenClaw import tooling with stable source fingerprints and an `import_ledger`.
- Added release bootstrap files: `pyproject.toml`, `.gitignore`, and `CONTRIBUTING.md`.
- Added GitHub Actions CI and a local `scripts/check.release.py` gate for test/build/secret/path/artifact verification.
- Added `scripts/repair.vector_index.py` to rebuild the LanceDB companion from SQLite truth with backup support.

### Changed
- Switched active Hermes memory provider to `scope-recall`.
- Refactored provider internals by splitting migration logic, recall fusion, capture flow, storage views, and tool handling into dedicated modules.
- Changed vector maintenance from init-time full rebuild toward incremental sync by stable row id and `updated_at`, including stale-row cleanup and duplicate physical-row repair.
- Clarified README and DESIGN documentation to describe the real runtime architecture, configured Gemini OpenAI-compatible default embedder, and local fallback boundary.
- Updated release regression coverage so the default runtime path explicitly verifies fallback to `local-hash` when API embeddings are unavailable, while dimension-rebuild coverage uses an explicit local-hash config override.
- Fixed wheel packaging so the published artifact installs as an importable `scope_recall` package instead of scattering provider modules at site-packages top level.
- Restored Python 3.10/3.11 compatibility in `vector_store.py` by removing 3.12-only f-string quoting syntax.
- Included the OpenClaw import script in wheel data files for public release completeness.
- Preserved SQLite truth writes when LanceDB delete/upsert fails and marked the vector layer `needs_repair` for later repair.
- Kept top-level `import scope_recall` free of Hermes runtime imports; `register()` lazy-loads provider code.
- Documented `on_memory_write` as an intentional observational no-op because curated memory files are live-read instead of mirrored.
- Replaced dynamic `ALTER TABLE` f-string construction with an explicit allowlisted migration mapping and changed test placeholder keys to obvious non-secrets.

### Compatibility
- Legacy `lancepro_store`, `lancepro_search`, and `lancepro_stats` aliases remain accepted during transition.
- Legacy `$HERMES_HOME/lancepro/` SQLite/config storage is migrated forward on first initialization.

### Known limitations
- Vector repair/rebuild is available through `scripts/repair.vector_index.py`, but live gateway runtime freshness still requires an explicit service restart / human-triggered verification after deployment.
- OpenClaw historical imports still require an explicit one-shot import step; they are not automatically reused.
## 2026-05-20 — Retrieval hygiene regression

- Removed arbitrary recent-memory backfill from lexical SQLite retrieval. This prevents unrelated ordinary turns from recalling fresh durable ops rows (for example OpenClaw / 凌晨 task context) solely because of source/target bonus.
- Added a `vector_only_min_score` gate so weak vector-only matches cannot auto-recall unrelated durable ops rows without lexical evidence.
- Added alias-expanded SQL discovery so lexical-only recall still finds intended alias matches such as `response style` → `replies` without broad recency scans.
- Added regression coverage for unrelated-query suppression, high-confidence semantic hits, relevant lexical hits, and alias-expanded discovery.
