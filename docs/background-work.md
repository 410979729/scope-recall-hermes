# Background work and recovery

SQLite is authoritative. Model requests and vector publication run outside its transactions; their results are
accepted only under a current lease and unchanged source authority. A nonterminal entry has a scheduled attempt
or an explicit dependency that doctor can report. A stopped entry retains its reason and a recovery action.

Failure classification and delay schedules live in `core/failure_policy.py`. `transient` failures can clear with
time; `capability` failures wait for a route, credentials, budget or provider; `configuration` failures require
correcting input or configuration; `refused` outcomes enforce authority or content policy. Unknown codes remain
eligible for an operator's bounded recovery grant. Unknown candidate failures do not clear an attempted-request
fence: whether an operator can reopen work and whether repeating a paid request is safe are separate questions.

The next-state columns include self-transitions when an attempt is deferred. Property tests drive the actual
storage and replay operations and check these tables, including schema state constraints.

## Work items

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
|---|---|---|---|---|---|
| pending | pending, leased, done, obsolete | Claim at available_at; completed consolidation can cover pending siblings | Supervisor plans available_at when the route is configured; unavailable routes remain visible to doctor | Three attempted failures; capacity refusals refund the attempt | no |
| leased | leased, pending, done, failed, obsolete | Settle with owner/token fence; expired lease returns to pending or fails at the attempt limit | Supervisor plans lease_until; expiry is reclaimed by the next worker | Same work budget; no lease renewal around an uncertain model call | no |
| done | done, pending | Completed; deliberate re-embedding can reopen a page | Explicit embedding-space change | No automatic replay | yes |
| failed | failed, pending, obsolete | Retain last_error_code; known infrastructure faults can reopen after their cooldown | Worker runs bounded automatic recovery; doctor shows codes and `retry-failures --apply --config <runtime-config.json>` | Automatic recovery grants are bounded; one release grant; two explicit item retries | stopped |
| obsolete | obsolete, pending | Authority removed; reversible suppression can reopen an unattempted candidate evaluation | Suppression release revalidates authority and advances lease token | Never reuse an old lease | yes |

Ordinary failure waits double from two seconds to a minute. Provider capacity refusals wait thirty minutes,
doubling to four hours without abandoning the item. Account, budget and missing-credential refusals park work
for an hour without spending an attempt. Invalid consolidation or evaluation output gets one recorded repair
attempt. A repeated invalid result stops and retains validation metadata. Operator recovery closes unfinished questions whose work stopped; previews retain their request marks, and a spent grant closes bookkeeping without repeating the request. Private-mode work is held without an
outbound request. `scope-recall retry-failures --apply --config <runtime-config.json>` grants bounded recovery after repair; adding
`--include-terminal` explicitly includes by-design refusals. Stored retry marks retain their recorded meaning.

Embedding preparation uses the earlier of the pass deadline and the group's effective lease deadline, with time
reserved for publication and bookkeeping. A single-request embedding port receives decreasing budgets and may
return an identified prefix. Prepared members are published and completed before unprepared members are returned
unspent. A failed single request retains the prefix and its failure code; the failing member records its outcome,
unasked members wait under that failure's schedule, and the pass stops asking that work type. Publication still checks every member's source, scope, derivation and lease. Malformed partial results
cannot be paired positionally with another subject. A pass with no request budget left stops claiming that type.

## Candidate debounce

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
|---|---|---|---|---|---|
| pending_evaluation | pending_evaluation, waiting_evidence, resolved, archived, blocked | Sweep at quiet-window or deferral deadline; queued evaluation owns the work lease | Supervisor's settle wake; doctor separates collecting, queued and nothing-new-to-ask | One queued question per evidence fingerprint; unchanged questions are not repeated | no |
| waiting_evidence | waiting_evidence, pending_evaluation, archived, blocked | New admissible evidence reopens debounce; old idle candidates become dormant | Capture evidence triggers; doctor reports waiting_evidence | No timer can invent missing evidence | dependency |
| resolved | resolved, blocked | The candidate has a verdict; fact lifecycle owns subsequent changes | No automatic re-evaluation of this settled version | None | yes |
| archived | archived, pending_evaluation, blocked | Dormant lack-of-evidence entries may wake on new evidence; other archives stay closed | Capture trigger only for dormant_no_evidence; doctor distinguishes dormant entries | No repeated unchanged question | dependency or yes |
| blocked | blocked, pending_evaluation, waiting_evidence, resolved, archived | Reject outbound work; suppression release revalidates the current head: judgeable facts resume debounce, active facts resolve, other fact states archive | Explicit authority restoration, never a timer | No attempt while blocked | stopped |

A verdict-produced revision inherits its answered evidence fingerprint and collected evidence. A version change alone cannot ask that question again; new admissible evidence or an explicit rule change can. Claim publication compares the final assertion, qualification and cited evidence after temporal and conflict ordering. Identical assertions do not conflict with themselves; replayed active assertions retain their head, and unchanged evidence alone cannot resolve an existing dispute.

Source-verified owner statements with supported positioned evidence resolve at publication. Other statements retain their qualification reason beside the candidate state. A support capability failure preserves the candidate and stops later support requests in that batch. After thirty days without new evidence, waiting candidates become dormant archives: the claim remains proposed, remains labelled in recall, and new admissible evidence can reopen it. No timer deletes a claim or repeats an unchanged question.

Evidence settles after fifteen quiet minutes or at most one hour of accumulated deferral. Five unanswered
questions slow later questions to at most once a day. These evidence timers do not classify provider failures.

## Candidate evaluations

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
|---|---|---|---|---|---|
| queued | queued, resolved, waiting_evidence, archived, failed, obsolete | Claim associated work; begin_model_attempt persists before requesting a verdict; expired failed work can retain queued bookkeeping | Work queue or settle sweep; doctor names retry-failures to reconcile stopped work, with a retry only if its grant remains available | Work limit; only explicit provider rejections clear the attempted-request fence | no |
| resolved | resolved | Fact accepted and work completed in one transaction | None | None | yes |
| waiting_evidence | waiting_evidence | Question answered without sufficient evidence; work completed | New evidence creates a new question through debounce | Never repeat this question automatically | yes |
| archived | archived | Rule or evaluator closed the question | None | None | yes |
| failed | failed, queued, obsolete | Retain failure_code; uncertain crash/timeout keeps its request fence | Bounded repair or genuinely new evidence; doctor shows failed work and action | Explicit retry rules preserve authority and request fences | stopped |
| obsolete | obsolete, queued | Deletion, suppression or stale head fenced the question and its work | Only reversible suppression of an unattempted question can reopen it | Old lease token is invalidated | yes |

## Capture inbox

These are logical states encoded by row presence and last_error_code, not another stored status column.

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
| ready | ready, deferred, given_up, stopped, completed, cancelled | Replay or deterministic conflict rekey in one transaction; rollback retains the row | Worker inbox wake; capture authorization is checked again | Infrastructure contention preserves the row | no |
| deferred | deferred, given_up, stopped, completed, cancelled | Retry at the timestamp in DEFERRED; replay and rekey retain their own path | Supervisor reads the same deferred_until rule as replay | Twenty-four delayed attempts; one minute doubling to one hour | no |
| given_up | given_up, ready, deferred, cancelled | Preserve reason and path for inspection | Doctor reports reason; `retry-failures --apply --config <runtime-config.json>` restores the correct path | No automatic attempt after give-up | stopped |
| stopped | stopped, ready, cancelled | Preserve terminal code; ACCESS_DENIED returns to ready through an explicit retry grant; invalid input stays stopped | Doctor recommends retry-failures only for ACCESS_DENIED; correct other stopped input at the capture source | None | stopped |
| completed | completed | Source effects and inbox removal committed atomically | None | Idempotent source identity | yes |
| cancelled | cancelled | Authority or deletion fence removed the capture | None | None | yes |

Deferred stamps whose timestamp is malformed or implausibly far in the future are due immediately. Give-up
counts survive release changes. A conflict is rekeyed from the content fingerprint, never by widening authority.

Session records are also durable capture ledgers. A worker reads bounded pages from attached Claude Code, Codex and
WorkBuddy homes and from a local Codex installation's registered record or native session directory. It records only
offsets and failure codes beside the home; content remains in the host's record until normal capture accepts it.
Unchanged completed files cost no store read. Installation time bounds automatic recovery; importing earlier history
is a separate owner operation. dsh retains unacknowledged messages in its existing plugin spool. Incomplete or deferred pages request a later
worker pass. Completed ledgers rely on ordinary worker autostart and host end hooks for discovery; they create no
perpetual polling task. A library-wide checkpoint retains the next scan position across files and entries. A budget
cut returns `pending_scan` and requests another worker pass; it does not complete an unvisited record or consume a
failure attempt. Partial files retain their byte cursor and yield to later files and entries, including when a file
ends in an incomplete line or continues growing. A scan retains its revisit requirement across budget cuts until
a full sweep resolves all unfinished files; per-file retry timestamps still prevent early retry.

Local capture authorization has an immutable timestamp separate from configuration saves. An older installation
uses its verified, bound installation receipt's write time, or its entry's first recorded capture when no receipt
exists. Without either evidence, automatic replay stops. Reinstalling an existing store preserves that time; creating
a fresh store starts a new capture lifetime and resets its scan position. Authorization and rotation checkpoints
bind to the immutable database identity, which survives file replacement and schema upgrades. Existing databases
receive an identity without an inferred creation time. The installer preserves old receipt evidence before writing
a replacement receipt. A valid JSON grant with an invalid time stops replay; doctor reports the reason. Re-running
the trusted installer authorizes only future capture, while restoring a trusted grant requires a backup of the same
database. WorkBuddy message records attest their session through
`sessionId`; filenames may identify a different storage agent and cannot grant a session identity. Every replayed
message must agree with that session before its page reaches capture.

Ledger checkpoints use the inbox's `deferral`, `replayable` and `given_up` rules and delay limits. The worker receipt
and doctor expose aggregate `transcript_replay` outcomes. `retry-failures --apply` reopens given-up and denied
checkpoints; `--include-terminal` additionally permits an explicit retry after invalid input is repaired. Recovery
checks current entry grants, original occurrence times, deletion fences and private history again. Inline private
spans are removed through the same capture filter, including nested or unfinished spans.

While ordinary storage refuses an upgrade, a metadata-only privacy snapshot may still recognize the unchanged private
interval contract. It checks the recorded and header schemas, installation identity, location and scopes, exact table
and index declarations, and restore fences. It cannot read sources or write data. An unknown privacy format still
refuses a local copy.

## Hermes capture replay

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
| buffered | buffered, in_flight, kept, completed, cancelled | Retry every thirty seconds and at host lifecycle hooks | Retry thread revalidates original audience before writing | Thirty minutes in memory, then persist eligible captures | no |
| in_flight | buffered, kept, completed, cancelled | One writer per identity; shutdown can persist its checkpoint | Original writer settles; concurrent replay skips the identity | Same memory window | no |
| kept | kept, buffered, completed, cancelled | Atomic file beside the store; reload after one-hour disk cooldown or host start | Existing retry thread polls disk cooldown; doctor counts kept files | Bounded memory buffer and disk capacity; no abandonment of persisted capture | no |
| completed | completed | Source committed or durable inbox accepted it; remove disk spool and confirm observation | None | Store identity deduplicates an interrupted acknowledgement | yes |
| cancelled | cancelled | Revoked authority or private capture is discarded | None | Never replay revoked authority | yes |

A host shutdown ends the retry thread; persisted captures resume at the next host start. Malformed spool files
remain available for inspection. `scope-recall doctor --host hermes --instance-root <home>` reports disk backlog.

## Backup upkeep

These states are derived from backup-state.json; file locks fence concurrent publication and retention.

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
| due | due, publication, retention, waiting, blocked, unconfigured | Publish one verified archive; a crashed lock owner releases its OS lock | Supervisor's daily_backup wake | Ordinary failure delays fifteen minutes | no |
| publication | publication, retention, waiting, blocked, unconfigured | Publish archive before recording retention checkpoint | Job failure records the checkpoint; supervisor reopens after the delay | Job deadline stops repeated oversized jobs | no |
| retention | retention, waiting, blocked, unconfigured | Resume rotation of published archives; never republish to repair retention | Supervisor's backup_retention wake | Fifteen-minute error delay; publication remains valid | no |
| waiting | waiting, due, publication, retention, blocked, unconfigured | Wait for next daily snapshot or retained error deadline | Supervisor reads persisted timestamps | No model or queue-item budget is spent | no |
| blocked | blocked, due, waiting, retention, unconfigured | Job exceeded its deadline; mirror upkeep continues | Doctor explains the limit; create a manual backup and clear backup_blocked after repair | No automatic snapshot while blocked | stopped |
| unconfigured | unconfigured, due, waiting, retention, blocked | No backup destination is configured; no job starts | Doctor reports missing backup configuration; configure a destination to resume the retained checkpoint | No attempt without a destination | dependency |

`scope-recall backup create --host hermes --instance-root <home> --output <new-backup.zip>` exercises a manual
backup after disk problems are corrected.
Set backup_blocked to false in backup-state.json to allow the next scheduled snapshot. Disabled snapshots wait
on explicit configuration; the deletion mirror remains independently scheduled.

## Deletion mirror

| State | Next states | Next action and lease expiry | Wake after capability recovery | Retry limit | Terminal |
| pending | pending, caught_up, unconfigured | Append missing tombstones under mirror lock; an interrupted owner releases its OS lock | Supervisor retries every five minutes; doctor reports lag and destination errors | No count limit; persistent tombstones are the checkpoint | no |
| caught_up | caught_up, pending, unconfigured | Current deletion inventory is durable; a new deletion creates pending work | Post-commit attempt then periodic catch-up | Idempotent inventory keys | yes |
| unconfigured | unconfigured, pending, caught_up | No configured backup destination | Doctor reports missing backup configuration; configure backup destination to enable upkeep | No outbound attempt without a destination | dependency |

Mirror failure never rolls back a committed deletion. The lag marker is cleared only after all current records
have been appended and flushed. Restores apply tombstones before making restored content available.


## Entity identity projections

A projection of a current statement can also check its entity identity when the worker supplies the shared
source-support judgement port. It nominates spelling and vector candidates in the exact scope, project and branch,
then publishes only a supported same-entity decision. The projection lease, private-mode checks and model budget
remain in force. An unavailable vector port leaves a content-free diagnostic and allows spelling candidates.
An uncertain identity leaves both records separate. Name candidates come from indexed character grams and exact
evidence mentions; at most 128 names are scored and 16 distinct subjects are passed to judgement.
A judgement receipt belongs to a name in its exact audience, so later facts with checked names spend no model
request. Endpoint invalidation reopens that name through an existing projection lease.

Identity links are ordinary versioned aliases with source evidence and pinned endpoint versions. Retraction stops
the link; a later endpoint revision invalidates its old proof and can refresh a live link. A retracted link is not
recreated by automatic backfill. Existing statement bodies are preserved.

Older stores gain an unresolved progress bit with a default value. The worker queues bounded pages through the
existing projection lifecycle. Doctor reports completed and pending identity checks; literal recall remains
available throughout. A missing judgement capability leaves the backfill pending.


## Importance and ended conversations

Every extracted claim can carry an importance from 1 to 5. Explicit requests to remember, corrections and health
or safety preferences receive 5; decisions and long-term constraints receive 4; ordinary stable facts receive 3;
short-term useful events receive 2; incidental conversation receives 1. Scoring does not grant fact authority.
Older claims read as 3 immediately and the worker marks neutral defaults in pages of 128 without model calls.
The stored importance remains the base score when pinned; pinned claims read as 5 and unpinning restores that base. The owner's metadata override survives later model extraction. Importance selects
background preferences; ordinary query ranking does not read it.

The management detail page edits importance and pinning. The CLI uses
`scope-recall memory importance <ref> --expected-revision <n> --score 4 --pin` (or `--unpin`). The existing revise
request accepts `new_value: {"importance": 4, "pinned": false}` with current human evidence explicitly naming the
claim and the requested metadata. These changes preserve the fact's wording and content revision.

Conversation summaries are extractive claims with the derived-summary basis and sentence-level source positions.
Each sentence retains its speaker role. The item origin reflects the least trusted member, including verified imports: tool observation, then assistant, then human. Summaries remain claims and never become fact evidence roots. A summary is omitted only when every excerpted sentence appears verbatim in displayed original messages; partial overlap keeps the whole summary. Packet fitting retains the summary when an original cannot be displayed and refills released capacity from the bounded candidate pool. Task summaries also receive the existing recovery structure and can cite
actual tool observations; an assistant's report remains attributed to the assistant. Sources from different scopes,
projects, branches or entries are never combined. A session closes on `records.close_session(context)` or after
30 minutes without capture. Hosts that do not supply an end signal use the idle trigger. A task closes only when
its existing episode state is completed; an open task never closes merely because it has been idle. Automatic host anchors with unknown task state do not block a session idle summary; only recorded open or interrupted work does. Unknown tasks still receive no task summary. Source holds block both input and reads; a completed clear scan or owner release restores source-backed eligibility. Shared-store imports rebuild summaries from renamed originals, while portable exports include only a currently valid summary snapshot.

Coverage is processed Unicode codepoints divided by all codepoints in the current eligible source versions. It
measures complete input traversal, not retention of every detail. Input traversal and excerpt coverage are separate. All consecutive input fragments must finish before summaries
are offered. Summaries are offered first; a raw message is removed only when positioned exact excerpts cover
every character. Messages with unquoted details remain candidates under the existing packet budget. Pages contain at most
16 sources and 12,000 UTF-8 bytes including instructions, and each result is at most 32 sentences and 1,536 excerpt characters, within the claim limit of 2,048 including speaker labels.
A group exceeding 2,048 sources remains on the raw path. Historical discovery reads 128 sources per pass, visits
64 group records per scheduling page and queues at most eight groups. Durable cursors make later groups reachable.

The supervisor wakes at the idle deadline or for old-source discovery. Unchanged completed input pages are reused after continuation, and only new or changed pages request the model.
Generation uses the existing projection
lease and auxiliary model port, including private-mode and budget-ledger checks; no new provider is configured.
Automatic summaries require explicitly configured API or CLI subscription budgets. The CLI default budget for
other work does not authorize summaries. With no model or budget, raw messages and existing facts remain usable. Doctor reports summary group states and
pending importance defaults. Failed and incomplete summaries retain raw fallback and use the existing bounded
work retry policy.

A summary records the source versions, content hashes and related fact revisions. Hydration and final release
recheck them, including current scope, deletion and suppression. A source withdrawal, new source version or fact
correction blocks the previous summary immediately. Rebuilding removes excerpts overlapping corrected or retracted
fact evidence; positioned evidence narrows the exclusion, and unpositioned evidence excludes the whole source.
Whole sentences are selected using the existing Chinese/English sentence boundaries, preserving conditions across
commas and semicolons. Over-budget sentences are omitted with raw fallback. Reused quotes retain their positions. Date and entry parsing, date filters and raw expansion remain
available. The dated-conversation channel checks every member, including tools, against its date, entry and session filters.
Replacement eligibility considers only the user and assistant messages that this channel reads; other messages remain available. Previous-conversation questions resolve to the latest visible prior session,
then use the same channel and fallback. Portable fact exports include importance and pinning and omit stale summaries.

## Session briefing

The worker prepares one briefing manifest per scope from unfinished task resumes, recent decisions, and important
or pinned preferences and constraints. A briefing contains at most three tasks, five decisions from the last fourteen
days, and eight preferences or constraints. Each line includes its type, content, time and reference. It uses existing
facts and task resumes without calling a model. At delivery the current scope, evidence versions, private mode,
deletion, suppression and source holds are checked again. A changed or unavailable item is omitted.

The runtime settings `session_brief_enabled` and `session_brief_tokens` control delivery; the defaults are enabled
and 1024 estimated tokens. Claude Code and Codex receive the briefing at session start and after a compaction event.
Hermes receives it on the first prefetch and resets its delivery marks on its pre-compression hook. WorkBuddy and
DeepSeek Harness receive it on their first prompt; their current adapters expose no compaction event. The same
session does not automatically receive an already delivered briefing item again. Session usage keeps briefing and
question-recall counts separate and stores references rather than copying source content.

Automatic host injection uses `injection_token_cap` for the complete rendered text, including guidance, and
`session_token_cap` for the combined briefing and question-recall usage since compaction. The defaults are 12654
estimated tokens per response and no session ceiling (`null`). The per-response default preserves the saturated
legacy output even with the largest supported briefing. These are tokenizer-independent estimates, not provider
billing counts. Zero disables ordinary injection; compaction clears the accumulated usage.

Question recall and briefing references share the delivery ledger. Preparation is read-only; final confirmation
rechecks current truth and records the chosen references and usage together. If the writer is busy, safe text still
returns with an unconfirmed-delivery reason, and neither usage nor notice acknowledgements are persisted.
A session that received a changed fact gets its current value with the old reference. Deleted, suppressed,
quarantined or private items produce reference-only withdrawals. Up to three notices share at most 300 estimated
tokens, reserved before ordinary content, and remain eligible after the session ceiling is reached. An undelivered
notice remains eligible on the next interaction. Hook receipts expose the response usage, committed session usage,
both ceilings and delivery outcome. An unconfirmed receipt cannot establish a committed cumulative count.

Owner recording commands return before injection or accounting. Paused entries release no background, recalled
content or correction notices, and retain undelivered notices for resumption. Reviewed fact replacements use the
current retained value; adding evidence without changing the statement does not send another correction notice.

The store schema is 1132. Supported older store layouts upgrade along the same coordinated chain. The additive schema upgrade takes the existing automatic snapshot before changing the store. The snapshot can
be restored with the supported store-restore workflow; original conversation text and existing fact payloads are
not rewritten by the upgrade.


## Cross-session observations

When ordinary work is idle, the worker can consolidate first-hand events into an `observation` claim.
A project partitions the input when available; otherwise the verified source principal does. Scope and branch
remain separate. Eight new source events are required, from at least two sessions; a fact and its original
message count only once. Each discovery page reads eight events and retains at most sixteen. Each batch can
publish at most three observations. Source windows contain at most 512 characters each and the whole model
input is bounded by 32,000 UTF-8 bytes. The original full text remains the evidence authority.

Generation and independent evidence support use durable stages in the existing work queue and the existing
auxiliary model ledger. They share the same explicit API or CLI subscription budget requirement as summaries.
No model or no explicit budget produces no reflection requests. Private mode blocks outbound generation and
support. The entire dependency manifest is checked at dispatch, publication, read and final release; relevant
full-private rule changes also invalidate an in-flight result even if the rule closes before release.
CLI work shares the ordinary per-pass call allowance; completing a stage does not reset it.

Every published body has passed independent support judgement and cites at least two source sessions. It is
labelled as an induction, never an owner quotation. Observations cannot act as source roots, grant authority,
resolve identities or revise ordinary facts. They do not enter the other-session overflow list. Visible original
statements take precedence; an observation whose complete statement is already displayed is omitted.

Read and final release recheck the input versions, hashes, related fact heads, scope, suppression and source safety.
Corrections, withdrawals, deletion and safety holds make an observation unreadable before rebuilding. Direct owner
facts in the same slot and later owner denials take precedence over the induction. The management review list
shows unconfirmed observations; confirmation retains the derived basis and all original dependencies. The owner
can edit or delete an observation through existing management and revision operations. Portable export includes
only valid observation heads, with their source chains; imports without local dependency records rebuild the
observations rather than treating the exported induction as owner authority.

Each scope has an independent ceiling of four reserved reflection calls per UTC day, shared across projects,
workers, generation and support. This allows one batch with three support decisions. A reservation is persisted
before dispatch and is not refunded after interruption; the auxiliary ledger and explicit model authorization
remain separate requirements. Doctor reports the day, reserved calls, limit and reset time without message text.

Historical discovery advances eight sources per page with fifteen minutes between pages. It uses the same daily
ceiling and durable cursor as live reflection. After two consecutive generations yield no valid candidates, the
new-evidence requirement doubles from eight to sixteen, then thirty-two and sixty-four; the input window remains
at most sixteen sources. A valid generation resets that requirement. Obsolete sources are removed from the
persisted buffer, so an unusable window does not schedule immediate empty passes.

Each candidate declares the input session numbers it cites. The core independently checks at least two distinct
sessions and exact original quotes before requesting support. Invalid candidates are omitted with a diagnostic
reason; valid siblings continue. Bounded owner-conflict pages keep their previous conclusion until a real conflict
is found. Ordinary fact text requalification leaves observation states unchanged. A new supported revision may
replace an invalid observation while retaining its old dependency history; direct owner suppression or deletion
is not released by that replacement.


### Injection text and usage

`injection_format` selects `json` (the default), `compact`, or `outline` in the runtime configuration.
The default preserves the existing host text. Compact memory items use one escaped text line with the type,
content, time and versioned references. Outline keeps a short deterministic excerpt and complete references;
use inspect to read the full item. Handoffs retain their external-report warning, unverified session label
and all evidence references. Outline correction notices name the current readable replacement for inspection;
withdrawals contain only the old reference and the withdrawal instruction.

All formats use the same authorization, screening, host-memory deduplication, entry controls and delivery
confirmation. Per-response and session ceilings count the final formatted text, including guidance, briefings
and notices. A smaller representation may retain more whole items within a configured ceiling. Receipts include
the chosen format and retained versioned references. Preparation is read-only; only confirmation accounts usage.

The doctor's `injection_usage` and the management recall history show injection tokens, session tokens,
injection limit and session limit. A null session limit means unlimited. An unconfirmed delivery has no confirmed
session total. Usage history is lossy diagnostic metadata and never replaces the SQLite delivery ledger.
