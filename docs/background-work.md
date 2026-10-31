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
