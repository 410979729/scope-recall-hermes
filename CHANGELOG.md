# Changelog

All notable changes to `scope-recall` will be documented in this file.

## [3.1.0] - 2026-09-18

Thank you for waiting, and sorry it took this long. 2.0.1 shipped at the end of August and it has been quiet here since, because we were not writing a patch on top of 2.0. We rebuilt the project.

**1,114 files changed, 177 commits, 96,108 lines added and 286,764 removed.** 197 modules in one flat directory became four files at the top level with everything else behind `core/`, `adapters/`, `runtime/`, `vector/` and `maintenance/`. Production code went from **141,044 lines to 48,289** — about a third of what it was, doing more.

2.0 was not a small or careless system. It was 197 modules, 3,322 tests, and a specification that described two-layer memory authority, claim-backed facts, durable work with leases and poison isolation, candidate semantics and a context compiler. Most of what 3.x does, 2.0 had designed. What changed is that it now **happens**.

### Memory matures now, instead of being sorted on the way in

This is the real difference, and it is the one we set out to build.

In 2.0, a piece of content was routed **at write time**. It either met the conditions for the claim lane — canonical type on the allowlist, valid structured proposal, acceptable provenance, strict authority router, confidence and identity and temporal checks — or it fell through to unstructured memory. One decision, made on first sight, from a single mention.

In 3.x a memory has a life. It is captured verbatim first. Candidate meanings **accumulate evidence** across sources rather than being judged on one remark. A **quiet window** lets a candidate settle before anything evaluates it, so a passing comment does not harden into a fact. **Corroboration** across independent sources strengthens it. **Consolidation** turns what survived into a claim with a version. **Requalification** revisits that claim when later evidence arrives, and **duplicate collapse** merges what turns out to be the same memory said twice. **Episodes** group what happened together. And **background context** surfaces what is relevant without being asked — you do not query your own memory, and neither should the agent have to.

Every one of those stages — `candidate_debounce`, `consolidation_chunks`, `requalify`, `duplicate_collapse`, `episode_storage`, `background_context` — is new in 3.x. None of them existed in 2.0.1.

Here is what that is worth, measured on the same machine. This is the 2.0.1 database that was handed to the migration, beside the 3.x instance that replaced it:

| | 2.0.1 | 3.1.0 |
| --- | --- | --- |
| Sources held | 4,688 memories | **167,117** |
| Facts in the claim layer | **0** | **490**, in 685 versions |
| Evidence links | — | **147,816** |
| Tables | 76 | 32 |

**2.0's claim layer was empty in production.** Not small — zero. The design was there, the code was there, the tests were there, and after the whole 2.0 era not one memory had made it through the gate. 3.x carries 490 claims backed by 147,816 pieces of evidence, and holds thirty-six times the source material, on one third of the code.

That gap between a specification and a row in a table is what this release closed.

### Scope Recall is now a general-purpose memory plugin

2.0 was called *Scope Recall for Hermes*, and it was exactly that — shaped around a single host from top to bottom. 3.x is a memory core with hosts as adapters in front of it.

**Hermes and Codex both work today**, through MCP tools and hooks, against the same memory. **Claude Code and the DeepSeek harness are next**, and the list is meant to keep growing: adding a host is now writing an adapter, not touching memory code.

If you use more than one agent tool, this is the change that matters most to you. Your memory stops belonging to whichever program happened to write it — you can tell one tool something and ask another about it.

### The store underneath was rebuilt

Both versions keep truth in SQLite with a LanceDB companion; that part never changed. What changed is everything about how the tables are defined and what the vector rows are for.

**The schema is one file now.** In 2.0, 22 different modules each created their own tables. In 3.x, four do, and the memory core's 32 tables and 16 indexes live in a single 371-line schema — which is why a migration can be verified, and why the table count fell from 76 to 32 while the corpus grew thirty-six fold.

**A vector row carries no text.** Its payload is empty; every answerable byte comes from SQLite. 2.0's vector rows carried their own copy of the content, which quietly made the index a second store. Now it is a pure derivative, with a process-isolated driver and its own compaction: delete it, corrupt it, change embedding model, move machine, rebuild — nothing is lost. The vector subsystem does this in 2,722 lines where 2.0 needed 12,776.

**The claim's quote must be a literal slice of a stored source revision.** 2.0 checked quotes too, but against normalised text, in a module whose own docstring declined to claim entailment. In 3.x the check is a precondition: storage refuses the claim if the span is not really there. It is a large part of why the claim layer has rows in it at all.

### What that gives you

- **Memory that follows you between tools**, instead of one plugin for one program.
- **Facts that actually form.** Accumulated evidence and a settling window, rather than a gate almost nothing passed.
- **An answer to "why do you believe that"** — the supporting sentence, in the source revision it came from.
- **Deletion you can trust**, including through an index rebuild.
- **A disposable index.** The expensive, fragile part of a memory system is the part you are allowed to lose.
- **"I don't know" as a real answer.** A question your corpus cannot support does not get a plausible-sounding one.
- **Visible cost.** Every model call is priced into a local ledger before the request leaves the machine.
- **A third of the code** to read, audit and fix when something is wrong.

3.1.0 in particular made the derived layer fast enough to live with: on a 167,000-source corpus, building or rebuilding a vector store moved from days to hours. On two benchmark question sets, correct answers went from 17/30 to 27/30 and from 14/25 to 20/25, with no regression on exact facts or on questions the corpus genuinely cannot answer.

### Upgrading from 2.0.x

**There is no in-place upgrade, and that is deliberate.** The two schemas are different enough that a silent conversion would be the kind of thing you only discover was wrong months later. Migration is an explicit, resumable, offline job that leaves your 2.0 database untouched.

Read `docs/upgrade-contract.md` before you start. The short version:

1. **Stop the old plugin from writing.** Shut down the agent, or at least the memory provider. A migration from a live database is refused.
2. **Back up the old store yourself.** The tool keeps its own isolated copy, but take your own first. This is the only irreplaceable thing in the process.
3. **Install 3.1.0 fresh and let the installer create an empty instance**, with its official installation manifest. Do not point the new plugin at the old directory.
4. **Prepare the job**, naming the old database, a fresh job directory and that manifest:

   ```
   scope-recall migrate prepare --source <old memory.sqlite3> --job <job dir> \
       --installation-manifest <installation.json> --host hermes
   ```

   If the old store has more than one scope, supply `--scope-map` mapping each old scope to an audience the new instance is actually bound to. Migration will refuse to guess, refuse to merge two old scopes into one audience, and refuse to widen a permission it cannot read.
5. **Run it**, then read the report:

   ```
   scope-recall migrate run --job <job dir> --source-quiesced
   scope-recall migrate verify --job <job dir>
   scope-recall migrate status --job <job dir>
   ```

6. **Queue the index** when you are satisfied with what came across:

   ```
   scope-recall migrate queue-index --job <job dir>
   ```

   Embeddings are generated in the background afterwards, bounded by your own budget. Until that finishes, recall works on the exact, lexical, claim and recent channels — semantic search is the part that arrives last.

**Things worth knowing before you migrate**

- **Re-running is safe.** The job is idempotent: interrupt it, lose power, run it again. It resumes rather than duplicating.
- **A blocked report is a real answer, not a failure to retry around.** If migration reports `blocked`, your old database is intact and the new one is explicitly incomplete. Do not start using the new instance until the block is resolved — an incomplete store will not pretend to be complete.
- **Migration never calls a model.** No re-summarisation, no embeddings, no memory leaves your machine during the job itself. The index step afterwards is the only thing that spends anything, and only through the route you configured.
- **Old vectors are not reused unless they can be proven equivalent** — same model, dimensions, encoding, segmentation and source mapping. Otherwise they stay in the old snapshot and the new index is built fresh. Row counts matching is not evidence that memories match.
- **What cannot be expressed losslessly goes to an archive rather than being approximated.** You will see it in the report. We would rather tell you a memory is in cold storage than quietly change its meaning.
- **Deleted memories stay deleted.** Deletion state and its dependency closure are applied before anything becomes readable or indexable.
- **Allow real time for it.** The duration depends on your data volume, not on a number we can promise here. Measure it on a copy first if that matters to you.

### About cost — please read this before enabling the model routes

3.x calls a language model of your choice for consolidation and candidate evaluation, and an embedding model for semantic recall. **This is where your money goes, and it is easy to underestimate.** A busy instance can make several thousand model calls in a day.

Some honest guidance from running this on real workloads:

- **Watch the quota from day one.** Every call is metered in a local ledger before it is made, with the model, tokens and charge recorded. `scope-recall doctor` shows the standing. Look at it in the first week rather than at the end of the month.
- **We suggest MiniMax M3 as the plugin's model.** On a controlled comparison — the same forty consolidations, two runs per model — it matched a well-regarded alternative in quality while costing meaningfully less per call. It is not the only good choice, but it is a good default.
- **Turn the model's thinking mode off for this job.** Measured on the same set: with adaptive thinking the pass took six times as long, three calls exceeded the request bound and were reported as timeouts, and three answers broke the JSON envelope. Structured extraction under a strict output format is a task where reasoning tokens hurt. The shipped configuration has it disabled; leave it that way unless you have measured otherwise.
- **Prompt caching matters more than the headline price.** Our prompts repeat heavily, so a provider with effective prompt caching can cost a third of one without it at the same nominal rate. Include that in any comparison you make.
- **Nothing is sent anywhere you did not configure.** Routes are explicit, credentials come from your own environment file, and a route you have not approved is simply not called.

### What is not finished

We would rather list these than let you find them:

- **The independent evaluation gate has no receipt yet.** The project declares a frozen, adjudicated evaluation campaign as a release criterion, and it has never been executed. Everything above is measured, but by us, on our own corpora. Treat the accuracy numbers as evidence, not as an audit.
- **Quoting tool output is still the largest source of lost memories.** When a source is a JSON tool result — a file listing with line-number gutters, an escaped API response — models frequently cannot reproduce a span byte for byte, and the claim is rejected. The memory is captured and searchable; it just does not become a claim. We are working on this and it is the next thing we intend to fix.
- **The vector companion has no explicit ANN index yet.** It relies on LanceDB's defaults, and the SQLite fallback is honest brute force. At our corpus sizes this is fine; on a much larger store you would want a built index, and building one is on the list.
- **Background scheduling is Windows-only.** Autostart uses Task Scheduler. On Linux and macOS the plugin works, but you schedule the worker yourself.
- **CI covers Windows.** The host, native and integration tiers run on Windows only; other platforms are tested by hand.
- **The 2.x compatibility layer is a one-way migration tool, not a bridge.** It reads the old format once. It is not maintained as a permanent compatibility surface, and there is no path back.

### Thanks

To everyone who filed an issue against 2.0.1 and then waited: the reliability work in this release started from your reports. And to the contributors who sent code — the embedder retry and backoff, the configurable retry delays, the vector admission floor, the secret-pattern word boundary, and the MiniMax embedder that a good part of this release now recommends — thank you.

## [Unreleased]

### Scope Recall 3.1.0rc42 a drain spends its seconds on the work - 2026-09-18

Found by timing every SQL statement, then every second, of one pass on an instance with 150,000 queued embeddings. Of 20 seconds, 8.7 went to five whole-store statistics queries that ran twice, 2.5 to the one embedding request, 1.0 to 32 vector commits, and every statement that was actually about an item took under half a millisecond. Then the owner of that pass turned out to be waiting 91 seconds after it finished. The queue was not slow because SQLite was too small, or because the provider throttled anything: it was slow because each pass re-counted the backlog it was there to shrink, and because nobody read what the pass said until its window ran out.

End to end on that instance: **96 items a minute before, 1,250 a minute after**, with the provider's own limits untouched at 0.3% of the requests a minute it allows.

- **A pass was read only after it exited, and a receipt is larger than a pipe.** The owner spawned each pass with piped stdout and polled for exit before reading a byte, so a pass whose receipt exceeded the pipe blocked in `write` with its work already committed and could never exit: 200 items done in 34.8 s, the owner returning `worker_watchdog_timeout` after 125.4 s, every pass, for as long as passes had been large enough. Both pipes are now drained by bounded readers while the pass runs. This was the largest single factor and the hardest to see, because the work was done and the queue moved -- only the receipt saying so was lost.
- **A pass may carry a thousand items, and four bounds had to be told apart to allow it.** `max_items` was 32, set when each source was its own request and each vector its own commit; the claim page, the recovery page and the pass itself all read that one number. Reopening a failure is a model call later and keeps its own page of 200; embedding a hundred documents is four hundredths of a cent and does not. Measured: 200 items in 16.8 s, 600 in 21.8 s, 1000 in 30.7 s.
- **A group asks its requests at the same time.** Ten requests of a hundred documents, asked one at a time, were 38 of a 1000-item pass's 55 seconds. Four may now be in flight; each reads the clock when it starts, and the answers are read back in the order they were asked, because that order is what matches a vector to its source.
- **The queue age is the doctor's to ask for.** A pass's report walked every queued row and every source to say how old the backlog was -- 1.5 s per pass, growing with the backlog. A pass reports its depth and its own items; the age keeps its exact meaning, unmoved by a retry, for the caller that asks.

- A pass called `status()` twice. The second call existed only to prove the database it had opened was the bound one, and discarded the answer: six whole-store aggregates and a JSON scan of every source's admission record, 4.4 seconds on that instance. Proving the binding is now one indexed row, and the receipt asks for the queue without the admission counts it never read -- it reports `source_only` from its own items.
- The native store's write API is plural and the Lance writer handed it one row at a time, so a pass of 32 sources took 32 lock-held handshakes and 32 dataset commits. Measured on a real store at real dimensions: 0.95s for 32 one at a time against 0.11s for one call carrying all 32. A group is now committed once, guarded once for every member -- lease, live subject, scope, project, branch, derivation epoch -- and a group the guard refuses writes nothing, leaving each member to publish on its own fence exactly as before.
- A pass was bounded at 32 items, a number set when each source was its own request and each vector its own commit. With both shared, that bound only decided how often a pass paid for its process start and its queue report. A pass may now carry up to 200, which is the core's own long-standing bound rather than a second number beside it, and the provider's real batch ceiling is measured rather than assumed: 32 texts answered in 2.6s, 64 in 3.2s, 100 in 3.8s, 250 refused with HTTP 400. One request carries a hundred and a longer group is sent as consecutive full requests, so a pass of two hundred sources asks twice instead of seven times.

Measured and not changed: the provider. At Tier 1 the account was using 0.6% of its requests a minute and 0.5% of its tokens a minute while the queue moved at 96 items a minute; every second of the difference was local.

### Scope Recall 3.1.0rc41 the batch reaches the worker - 2026-09-18

- rc40's batched embedding did not run: the worker probes the port it is handed, which is the runtime's bounded wrapper, and `prepare_sources` was not among the methods that wrapper forwards by name. A live drain still sent 183 single-document requests in six minutes. The wrapper now offers it, and the contract test asserts the capability through the seam the product uses rather than only against the port itself.

### Scope Recall 3.1.0rc40 one embedding request carries a pass's sources - 2026-09-18

Found while an instance migrated from 2.x sat twelve days from having semantic recall over its own memory: 153,000 sources waiting to be embedded, moving at 960 an hour.

- Both embedding dialects take an array -- Google's endpoint is literally `batchEmbedContents` and OpenAI's `input` is a list -- and the adapter sent arrays of one, so a store of 167,000 sources was 167,000 requests to rebuild. A pass now claims its ready embed items together and asks for them in one request, while each is still read, fenced, published and finished by itself. A group whose request fails, whose port cannot batch, or whose answer counts differently than it was asked falls back to one request each, so an answer can never be attributed to the wrong source; a refusal that stands the work type down still stops the pass, and the items it would have covered go back without spending an attempt. Verified against the real provider: eight documents in one request, eight distinct 3072-wide vectors, 2.0 s, one ledger row.

### Scope Recall 3.1.0rc39 a queue that cannot feed itself, and rejections that say what went wrong - 2026-09-18

Found while another instance tested its own memory and reported a stalled consolidation queue, a broken vector channel, a protocol bug and an empty summary layer. Three of the four were real; the fourth was the provider.

- A pass evaluated at most eight candidates while its schedulers queued up to sixteen more, so on a snapshot replayed with a model that answers instantly the queue grew by eight a pass with no new conversation at all: 941 waiting, the oldest ten hours old, each one having cost a model call to get there. A pass now queues at most what it can also evaluate and stops queueing once the queue is deeper than passes can reach; the eight-per-pass limit still holds candidates behind conversation work and lifts when nothing else this pass can do is waiting. Replayed again, 885 pending became 724 over six passes where before it grew to 913.
- A recall asking for a day's memory with `as_of` written in a local offset was rejected as `INPUT_INVALID` with field `format`, the schema keyword rather than the field; a missing property came back as `required`. The name is now taken from the error's own path, so a failed derivation also hands the model `source_refs` instead of `required` on its one repair attempt. The recall tool now states what `as_of` accepts and what each mode is for.
- Every plain chat turn opens an episode whose resume stays unwritten until a consolidation summarises it, and recall delivered that placeholder -- `{"state": "unknown"}` -- as an item of a six-item packet. Asked for by name it is still delivered.
- From 05:26Z to 12:27Z on 2026-09-17 Google refused every embedding call on another instance, 296 in a row with no success in the two minutes after any of them, and each of those recalls reported `vector_error:AuxiliaryModelError:http_status` -- what a malformed request also reports. The provider's status now joins the label.

Measured and not changed: retrying a refused query embedding, because across all 296 refusals no call succeeded within two minutes of one.

### Scope Recall 3.1.0rc38 a candidate keeps its own name, an unfinished attempt is offered once more - 2026-09-18

Found by replaying one instance's terminally failed candidate evaluations against the real model in a private sandbox: of eight, three settled, three broke the format again, and two had been overtaken by their own claim.

- Every name an evaluation was rejected over was the candidate's own, written differently: `embedding_retry.py` came back as `embedding_retry.py 全文` from the document's heading, a subject holding `\"看图\"` came back with plain quotes, and a predicate of a whole clause came back as its first word with the rest moved into `value_text`. One candidate had been refused four times over its predicate, each refusal a model call, and the feedback retry came back written differently again. What a candidate is -- kind, subject, predicate -- was recorded before the call, so a name that is the candidate's once escapes, spacing, width, quoting and case are set aside is restored to what was recorded. A name that is not the candidate's is still refused, and a kind never is: it is one of a fixed set. Replayed on the same eight candidates and the same provider, five settle instead of three; the one that still fails wrote a different word for its predicate, which is a disagreement, not a spelling.
- The at-most-once fence is committed before the model call, so a worker killed after it -- a gateway stop, a lost lease, a machine restart -- left the candidate failed with nothing recorded and nothing to look at. 24 of another instance's candidates and 5 of one instance's sat there, waiting for evidence they already had. A pass now reopens such a row once, bounded by `attempt` as well as by a marker, because an interruption observed as a lost lease reports `lease_exhausted` and keeps no history.

### Scope Recall 3.1.0rc37 a question reaches the reply it was given, one name written two ways is one name - 2026-09-17

Found by the QA benchmark over real questions and their answers on one instance and another instance snapshots, where rc36 answered 17 of 30 and 14 of 25.

- A reply was joined to its question only through episode membership, which is spent on every event of the episode in id order, so a question that was recalled at all could use the whole relation bound before reaching what it was told. The turn a person's message opened is now followed first, for every seed: the assistant's visible messages in the same scope and session, in capture order, until the person speaks again, within thirty minutes and at most three. They may take at most half the relation bound, so the claims and episodes reached from the same seed are still inspected.
- A question asked about `DLSS5` and `glm5.3-flash`; the replies wrote `DLSS 5` and `glm-5.3-flash`, and hydration refused them as naming something else. What is offered is now read both ways, with and without the space, hyphen or underscore where letters meet digits. The query is not read that way: `H100 exact identifier` would have asked for `H100as`, and `give me 3 options` would have demanded an answer containing `me3`.

Measured on the same snapshots at one wall clock, rc36 against rc37: one instance QA 17/30 to 27/30, another instance QA 14/25 to 20/25. Facts (60/60), no_match (20/20), supersession (2/2), questions (58/60), another instance scenario and complaint sets are unchanged.

### Scope Recall 3.1.0rc36 claims survive an unqualified summary, evidence has to speak to its candidate, a held writer is busy - 2026-09-17

Found while rc35 ran on all four instances and while a fifth instance was migrated from 2.0.1.

- A single-page consolidation rolled back whole when one resume or reference summary failed qualification. That cost a second model call and, when the second answer failed too, every claim in the page. another instance recorded 208 such summary violations in one day, nearly all `goal_authority`. A paged consolidation already dropped the summary and kept the claims; every worker result now does. A result submitted outside the worker is still rejected whole.
- A source became evidence for every reachable candidate sharing any lexical term with it: 116,000 rows on one instance's 1,215 live candidates, 6% of which restated the candidate. First-hand testimony keeps that rule. Any other source must restate the candidate or name its subject; a self subject such as 我 does not count. Replayed on the rc35 backups, one instance keeps 19% and another instance 30% of the newest evidence rows, every first-hand row, and every trigger-attached source a promotion cited. Existing rows are left as they are.
- A worker pass that raised `TruthWriterBusyError`, or SQLite reporting the database locked, exited as failed, which stops the supervisor until the next autostart wake. It now reports busy (exit 75, `worker_writer_busy`), and the supervisor waits `BUSY_BACKOFF_SECONDS` (30) before the next pass.

A vector threshold of 0.68 instead of 0.65 was measured and not taken: on one instance and another instance sandboxes with vectors, facts, questions, no_match and QA were identical at both values.

### Scope Recall 3.1.0rc35 a candidate source trigger always finishes - 2026-09-17

Seen while rc34 ran as a canary on one instance: worker passes still started every 8-17 seconds and processed nothing, now woken for `candidate_evidence_remainder`.

- A source that mentions more than sixteen candidates records a truncated trigger, and each pass resumed one page of it. Evidence membership is the cursor, so a page that linked nothing came back first on the next pass. 56 of one instance's 120 open triggers named memory read back to the model, which is never evidence, and could never close. Reinjected memory now mentions no candidate, and a page that links nothing closes its trigger.
- The other 64 triggers owed 1,091 pages at one page a pass. A pass now continues up to `SOURCE_PAGES_PER_PASS` (16) pages, one short write each, within half of its budget.

Replayed on a one instance snapshot, the 120 triggers closed in 72 passes, and the planner then slept until real work was due.

### Scope Recall 3.1.0rc34 a pass that can make no progress is not started again at once - 2026-09-17

Seen while rc33 ran as a canary on one instance: two instances started worker passes back to back that could do nothing.

- one instance ran a pass every ~20 seconds. The planner counted a candidate evaluation that failed with `lease_exhausted` as recoverable, but `recover_transient_failures` reopens only consolidate, embed, rebuild_projection and purge, so the row stayed failed and the plan stayed due. The planner now wakes only for failures of `AUTO_RECOVERABLE_WORK_TYPES`.
- Delta ran a pass every ~7.5 seconds. With no embedding credential, each pass parked one of 2,548 embed items for an hour, and the next pass started at once for the item after it. A work type whose port refused before any attempt (credentials, budget, `provider_hold`, account refusals) is now reported in `unavailable_work_types`, so the supervisor sleeps it for its cooldown as it does a missing port; `evaluate_candidate` is slept that way too.

### Scope Recall 3.1.0rc33 recall returns answers, and a refusing provider is left alone - 2026-09-17

Found by another instance testing her own recall on rc32 and by a recall benchmark over one instance and another instance snapshots: earlier questions came back instead of their answers, facts reached recall only through relation expansion, the newest report in memory was dated a day early, a recall test report came back first for the queries it quoted, and three instances kept asking Google about 5,000 times a day while a monthly spend cap refused every call.

- A restarted Hermes gateway numbers turns from 1 again, so a new message could arrive under a key an older message already held, and the adapter copied that message's time onto it before storage re-keyed it. Only a replay of the same admitted content inherits the stored time now. Rows already written this way (23 on one instance, 14 on another instance) are dated by their write when recalled; the copy is recognized exactly and no stored row is rewritten.
- Recall items carry `occurred_at`: a source's occurrence time, or a claim's newest evidence. The guidance injected beside the packet says the later of two disagreeing items is the newer statement.
- Bigrams holding an asking character (什么吗呢呀吧嘛哪啥), a few asking words and English helper words no longer count toward matching, specificity or ranking.
- A claim channel matches each claim's own statement: the query must name at least half of the subject and hit the statement beyond one word, one more for an unpromoted proposal. The version offered is the one hydration would admit, so a replaced value is not. Relation expansion spends its bound on the best seeds first. A claim is shown without its quoted spans, which for tool-derived claims were escaped JSON that filled the packet; its `evidence_refs` still name every source.
- A bare question (short, with a question mark or an asking ending) and a reply written in a turn with three or more memory lookups keep 0.6 of their rank in auto and current recall, and stay available as context.
- Budget admission weighs an event's size against a whole default packet (4,096 units) instead of 256-unit steps, and a run re-sorted for admission keeps the ranker's order among the events it admits, so a 690-unit reply no longer loses its slot to 15-unit questions.
- A model whose latest calls were capacity or account refusals is held from a minute after one refusal, doubling to half an hour, until the first answered call. The hold is read from the shared ledger: drains do not claim its work types, the planner sleeps them, adapters refuse to send (`provider_hold`, parked without an attempt), and the worker status names `provider_hold:<model>`. A healthy model is never held for another's refusals.
- The doctor names five or more model answers cut off at the output limit within an hour (`model_output_truncated`) and the two remedies: a larger `max_output_tokens`, or the provider's thinking turned off.

On consistent snapshots (one instance 10:30Z, another instance 10:49Z), rc32 -> rc33, top 3: one instance facts 32/60 -> 60/60, supersession 0/2 -> 2/2, facts asked as questions 17/60 -> 58/60; another instance facts 9/17 -> 16/17, asked as questions 4/17 -> 16/17, the two rc32 field complaints 0/2 -> 2/2. Questions the owner asked, with the reply in the top 5: one instance 15/30 -> 17/30, another instance 12/25 -> 14/25. no_match stays 20/20 on both.

### Scope Recall 3.1.0rc32 memory costs less than the agent it serves - 2026-09-17

Measured on one instance (2026-09-12..17): the plugin sent about 74 M uncached prompt tokens against the agent's 12 M, and the candidate loop was 81-97% of every day's plugin tokens. 60-96% of each day's evaluations re-asked a candidate already judged, 307 candidates were asked ten times or more, and 95% of candidates came from tool output. Of 10,650 evaluations, 27 promoted a fact: 19 on a candidate's first verdict and 4 on its second.

- A candidate gets two model verdicts (`AUTOMATIC_VERDICTS`). After that it is asked again only when a source no earlier verdict saw restates its value (its subject for procedure, intention and alias); otherwise the question is answered `repeat_without_restatement` without a model call. Replayed over one instance with rc31's checks, 20% of the historical model calls remain and 24 of the 27 promotions survive.
- A preference, constraint, decision, intention or alias proposed only from tool or document sources is archived as `person_kind_without_person` instead of becoming a candidate: 2,034 such evaluations on one instance promoted nothing, and when the person says it, the consolidation of their own words proposes it with the authority it needs. Candidates registered by older releases are archived when next scheduled, and queued evaluations when the worker reaches them.
- A source longer than 3,000 characters reaches a candidate evaluation as a window of 1,500 characters either side of the candidate's value (its subject, then the source's head, when the value is absent). Qualification still reads the complete stored source. Replayed, windows keep 44% of the evidence the remaining evaluations carried.
- Work claims order candidate evaluation after every other type, so candidate work never holds captured conversation back from consolidation and embedding.
- When a provider's `total_tokens` exceeds prompt plus completion, the difference (for example Gemini 2.5 Flash thinking, which another instance's route billed at roughly 8,000 tokens a call while recording a median of 325) is stored as `unreported_output` and charged.

### Scope Recall 3.1.0rc31 pay only for questions that can be answered - 2026-09-17

Measured on one instance the day rc30 shipped: DeepSeek billed about 20 million prompt tokens, about 70% of them outside its prefix cache, for 2,019 candidate evaluations that promoted 7 facts. A DeepSeek balance that ran out then failed 100 evaluations in fifteen minutes, and no operator command could reopen them.

- Candidate evaluation first checks two necessary conditions of `claims.qualify` and records the answer without a model call when either fails: the candidate's value appears in no supplied source (`value_not_in_evidence`; qualification needs it inside a quote, for every kind but procedure, intention and alias), or no supplied source can lend the authority its kind needs (`no_authoritative_evidence`). The check runs where an evaluation is scheduled and again in the worker, so evaluations queued before this release are covered. The answer is stored like a verdict, with no work item and no attempt, so the settle sweep does not pick the same question again; new first-hand evidence poses a new question. Of the 10,602 evaluations one instance had run, 54% failed one of the two conditions and none of the 27 that promoted a fact did. The kind and origin sets are named once in `core/claims.py` and imported by `core/evidence_question.py`.
- A provider answering 401, 402 or 403 has refused the account, not the request. Such work is now parked for an hour without spending an attempt and its work type stands down for the pass, like a budget refusal; candidate evaluations hand their attempt back. `retry-failures` can reopen rows that failed this way before the release.
- Consolidation and candidate-evaluation input lists the sources before the refs, watermark and empty result. Those differ as soon as any one source does, and placed first they ended the prefix two requests could share before a single source. Replayed over one instance's 2026-09-17 evaluations, an ideal prefix cache reuses 68% of prompt tokens in the new order against 54% before.
- The auxiliary ledger records the prompt tokens a provider reports serving from its cache (`prompt_cache_hit_tokens`, or `prompt_tokens_details.cached_tokens`) in a new `cached_input` column, added on first use to existing ledgers. Charges still price every prompt token.

### Scope Recall 3.1.0rc30 current facts reach recall - 2026-09-17

Fixes found by testing rc29 on one instance's real data: facts were captured but reached recall late, stale, or not at all.

- The Hermes `recall` tool no longer fails with `INPUT_INVALID/current_source_refs` in every mode once one turn has captured 17 source segments (one user message plus 16 tool results, Scope Recall's own tools included). Core accepts up to 256 current-turn refs, the provider records every ref, and a turn that exceeds the bound degrades visibly: prefetch returns nothing and the tool answers with an `unavailable` packet and the capability gap `degraded:current_source_refs_limit` instead of an input error.
- Vector admission compares a hit with the configured embedding space (`RecallPolicy.embedding_space_id`) instead of the shipped default, so a route that names its model no longer has every vector hit refused as `vector_old_or_mismatched_space`. `doctor` names `vector_threshold_unconfigured` when vector recall is wired without a threshold, and `docs/configuration.md` documents the field and where calibrated values come from.
- Ranking keeps fusion order when every event fits the packet; size ordering only decides what admission must drop, so a newer long report is no longer ranked below an older short note. Lexical rows naming a query's hard identifier rank first and such terms are never pruned as common, so generic matches cannot crowd the only admissible source out of the candidate pool. A release version is also identified by its suffix (`3.1.0rc28` answers `rc28`), and comparisons count a suffixed version once.
- An explicit recall (Hermes tool, Codex MCP) that finds no evidence returns `no_match` rather than ambient preferences a caller could read as the answer; a resume request still gets its grounded current task, and automatic recall is unchanged.
- Query-time Chinese synonym groups (14 curated two-character groups such as 仍然/依然, 安装/装上) widen lexical candidates without inflating admission: a synonym hit counts as the query term it stands in for.
- Worker results are discarded only when something they were derived from changed during the model or vector call — a source's identity, suppression or blocks, a deletion operation in the scope, a restore, a claim version written to the same slot, or the episode resume — instead of on every `memory_epoch` move, which every captured message caused. Read-side fences are unchanged.
- A bounded fresh-conversation lane: every second claim of a pass prefers consolidate and embed work of human and tool sources persisted in the last two hours, and the deferred-source refill takes such sources first, so a message typed now is not queued behind the whole backlog.
- The worker claims model work only while the pass covers one bounded request plus a finalize margin, the watchdog hands its deadline to the child and kills only after a grace, one day-counter bound is shared by the worker and its planner (supervision and autostart no longer stop for the rest of a UTC day once 10,000 items were reserved), a long source's page checkpoint no longer stops other consolidation, and reinjected recall output is `source_only` instead of consolidate and embed work.
- Model output: timestamps with a non-zero offset are converted to the same UTC instant before validation and the prompt asks for UTC; a reply stopped at the output limit fails as `DERIVATION_INVALID/model_output_truncated` so the guided retry names it; fact entailment keeps dotted values such as `3.1.0rc28` in one clause.
- `trusted_runtime_worker_busy` and other launch gaps describe the latest launch attempt instead of sticking for the session, and `status.work_error_counts` counts each failure kind once.

### Scope Recall 3.1.0rc29 recovery candidate - 2026-09-16

- Restructures the shipped code without changing behaviour (the seven gate tiers are the contract, and pass with the same counts). The seventeen helper modules that sat at the package root live in `core/` and a new `vector/` package; `_internal/` is gone. God functions are named steps: `maintenance/legacy_conversion.py::migrate_legacy` (2,149 lines) is a staged pipeline over one `Conversion` object across `legacy_plan/sources/claims/deletions`; `core/candidate_storage.py` (1,188 lines) is `candidate_tables/intake/evaluations/sweeps`; `maintenance/install.py` (1,412 lines) is `install_common/receipt/codex/hermes/purge` with a two-entry host table instead of eight `if host ==` ladders; `maintenance/doctor.py::run_doctor` is a driver over named checks; `core/worker.py::drain_worker` is a dispatch table with one embed processor; `core/claims.py::qualify` is an ordered list of refusal rules; `core/recall_packet.py::compile`, `core/recall.py::search`, `core/storage.py::put_source`, `core/episode_storage.py::attach`/`apply_resume`, `core/mutate.py::apply_claim`, `core/truth_connection.py::connect_truth_database` and `scripts/check.py::main` are short drivers over helpers. Duplicated helpers have one copy: the Codex/Hermes tool boundary (`adapters/tool_common.py`), the vector store contract (`vector.VectorStore`), `_sha256`/atomic writes (`maintenance/backup.py`), bounded text, strict scalar coercion. The operator CLI routes through a command table. About 3,000 further lines that nothing reached (writer-handoff telemetry, the 2.x evolution-proposal parser, transport-noise classification, query-intent helpers, unused store members) are removed; `scripts/dead_code.py` is the reference scan that found them.
- `tests/sitecustomize.py` binds `scope_recall` to the checkout in every process the gate starts: until now a spawned child resolved the name through whatever distribution the interpreter had installed (on the development venv, an editable install of a retired rc11 checkout), so subprocess-driven tests exercised code that was not under test.
- Removes the retired 2.x engine and everything that only it reached: about 1,000 files and 400,000 lines (the root-level `provider.py`/`journal.py`/`relation_*`/`experience_*`/`fact_executor` stack, the `_internal/` runtime that wrapped it, 2.x operator scripts and benchmarks, 350 test files no tier ran, the frozen source snapshot under `verification/P08/source-candidate-v1`, and the 1.x/2.0 release-readiness and runbook documents). The wheel allowlist is unchanged: every module the 3.x entry points reach is still shipped, and `scripts/build.package_manifest.py --check` proves it. The nine legacy-only tests that `integration`/`release` still ran (`test_vector_policy`, `test_writer_handoff_*`, `test_zero_signal_retrieval`, ...) tested that engine and leave with it. `maintenance/legacy_fixture.py` moves to `tests/migration/` because only tests use it.
- Integrates the recovery-plan candidates: shared doctor/scheduler predicates, bounded derivation repair, packet budget handling, reusable query-embedding transport, and dependency ranges covering the locally checked versions.
- Separates migration conversion, activation and indexing; separates worker outcomes and consolidation; adds the offline package-upgrade path and package health checks. Removes only the previously approved legacy deletion set; operator D6 cleanup remains a checklist, not an automatic deletion.
- Adds optional, explicit `gpt-5.6-luna` Codex CLI consolidation with a verified tool-free binary, normal Core validation, durable subscription reservations and a per-call deadline. Offline request-boundary checks do not establish real subscription inference or continuous-operation acceptance; the startup timeout remains an open activation blocker.
- Includes the Codex consolidation checks in both integration and release selection, and separates RC CI from stable-release publishing. This candidate does not assert remote CI, deployment, 24-hour performance or seven-day acceptance.

### Scope Recall 3.1.0rc28 the Codex wrapper carries its own credentials - 2026-09-16

- The Codex host was the third installation, still on rc5. Its plugin directory held a hand-written `scripts/local_runtime.py` plus a second hook launcher, added forty seconds after the rc5 install and never recorded in the receipt: Codex starts the MCP server and every hook with its own environment, the installer's `.mcp.json` and `hooks.json` gave those processes no way to reach the embedding key, and the launcher loaded it from `embedding.env` by hand. The installer now takes `--env-file` for `--host codex`, writes it into `.mcp.json`, `hooks.json` and `scope-recall-hook.cmd`, and records it in the receipt; `mcp_entry` and `hook_entry` read it through the one function the worker already uses, `credential_environment` (only the names the runtime config declares, no dotenv interpolation), exposed as `host_process_credential_environment`. A missing or unreadable file is a stderr diagnostic and the process still starts, because a memory tool without its semantic channel is worth more than no memory tool; a hook never fails for it. `--host hermes` rejects the flag: Hermes processes inherit the gateway environment. The local launcher is retired with the rc5 wrapper.
- `tests/host/codex/test_env_file_credentials.py` enters `host`; `test_install_v11` gains the wrapper/receipt case. host: 122 passed; packaging green after this bump.

### Scope Recall 3.1.0rc27 one lineage, one line ending, one install truth - 2026-09-15

- The first version built from the canonical repository since rc22. rc23 through rc26 were developed, tagged and deployed from a standalone `git init` copy of the tree with no remote and no common ancestor with `release/3.1.0`, and every file in it was committed with CRLF, so a diff against the canonical branch showed 482,644 changed lines where 1,553 were real. Those four releases are replayed here as three commits with LF endings and their original messages; `.gitattributes` now declares `* text=auto eol=lf` so it cannot recur; the standalone copies are retired. Code delta against the rc26 that one instance runs: the two items below, and nothing else.
- `autostart plan/enable` without `--python` died in `pathlib` with a `TypeError` on `None` (hit on one instance while restoring the worker task after the rc26 deploy). `--python` defaults to the interpreter running the command, `--user-id` to the current account, and a contract failure from `plan()`/`apply()` is printed as `{"status":"error","code":...}` with exit 2 like every other maintenance command. `tests/contract/test_autostart_cli.py` enters `contract`.
- The root `scope_recall.py` self-import shim is removed. With the checkout as the working directory, `python -m scope_recall.maintenance.cli ...` resolved `scope_recall` to the source tree and ran it against the live instance instead of the installed package; a stray copy of the same shim in `%TEMP%` did the same from a "neutral" directory. Nothing shipped or gated imports it. Operator commands run with `-I` from now on; `AGENTS.md` gains the release and deployment rules.
- `distribution/codex/scope-recall/.codex-plugin/plugin.json` and `.mcp.json` are in the allowlist, stamped by the manifest tool and shipped in every wheel, and were never under version control: the `.gitignore` pattern `scope-recall/` meant the runtime data directory and also matched them. The pattern is anchored to the root and both files are tracked.
- Recorded, not changed: one instance's venv runs lancedb 0.37.1 and mcp 2.0.0 against `pyproject` ranges of `<0.31` and `==2.1.0`; the native tier has only ever run against the declared versions. another instance still runs rc10. Both are steps in `docs/recovery-plan-2026-09-15.md`.

### Scope Recall 3.1.0rc26 measure the packet that will be returned - 2026-09-15

- Admission measured a packet that did not exist. `RecallPacketCompiler` judged each candidate against an envelope that already carried the rejection markers (`budget_token_cap`, `expandable`, `budget_oversized`) of every higher-ranked candidate it had just refused, so one oversized item charged its markers to every item below it, and a packet with room for evidence was returned with none. Reproduced on one instance at a 1200-token budget: five candidates, zero items. The compiler now assembles the exact prospective packet without side effects (`_assemble`, with a fixed-width `diagnostic_ref` placeholder so the measured bytes are the returned bytes) and admits on its real canonical byte count; per-candidate rejection markers are merged after selection; the `diagnostic_ref` is the last field sacrificed rather than the first evidence item. `resume_state_unverified` joins the collapsed public markers.
- A caller that stopped waiting no longer kills the helper. When the request deadline expired inside a native lock wait or a slow Lance RPC, `lance_process_store` reaped a healthy worker, and every later recall failed `worker_failed` until an explicit reopen -- measured live as four consecutive dead recalls after one slow one. The helper is kept and the frame it still owes is parked and drained before the next request. Two defects in the first draft of that change are fixed here rather than shipped: the drained frame's error was raised into the unrelated next request (it is now discarded; its own caller already treated the outcome as uncertain), and nothing bounded how long a frame could stay owed, so a truly wedged helper would have degraded every later request forever. A frame older than `_pending_response_timeout` (60 s) reaps the helper and sets `requires_reopen`, and the new fault has a name: `worker_unresponsive`. The vocabulary guard in `test_vector_failure` caught the first version of this shipping a nameless RuntimeError, which is the guard doing its job.
- Records the five files hot-patched into one instance's site-packages this morning under the unchanged string `3.1.0rc25` (partition fan-out reserve, matched lexical terms and tool-role ranking, clause-level Chinese specificity, vector port reopen). They were never committed, so the repository at `v3.1.0rc25` and the live instance ran different code behind the same name -- the fault the rc24 note describes, repeated within a day. They are committed byte-identical to the installed copies and this release is the first version string that names all of the code one instance runs.
- Two test files selected by no tier enter `native`: `tests/test_cold_open_overlap.py` and `tests/test_lance_fanout_deadline.py`. integration: 1679 passed, 0 failed; native: 48 passed.
- Measured and deliberately not changed in this release, recorded so the next one starts from numbers rather than from the symptom. (1) The semantic channel is starved on one instance: `auto_recall_seconds` is capped at 5.0 and the vector stage receives 75% of what remains, about 3.3 s, while one query embedding through the local proxy takes 2.9-3.8 s (median 3.1 s, 6/6 measured). Of that, 2.5 s is connection setup: the HTTPS transport spawns a fresh worker per request, so every call pays a new proxy tunnel and TLS handshake (cold 3.8 s median; the same request on a reused connection 0.92 s). Six of six live recalls reported `vector_error:AuxiliaryModelError:timeout`. The fix is a persistent HTTPS helper with per-host connection reuse, which is a security-boundary change that needs its own tests and review. (2) Lexical-only admission is not monotonic in the budget on a live query: 1600 tokens returned the expected item, 2400 returned it plus one more, 3200 returned two claims and lost it, 4096 returned a single 3398-byte duplicate-import event and nothing else. Length-biased lexical ranking plus greedy admission in rank order lets one oversized item consume the packet; the compiler change above does not alter these outcomes (candidate and live trees produce identical packets), and an admission policy change needs an evaluation set before it is made.

### Scope Recall 3.1.0rc25 the number reaches the file that is polled - 2026-09-15

- `terminal_failed_work` was computed, used for the status decision, and then dropped. `persist_worker_status` accepts only a closed set of keys -- which is right, it exists to keep arbitrary text out of the file a host agent polls -- and the new field was not in it. The decision changed on the live instance and the number explaining it never appeared. This is the third time in this release that one half of a pair was updated and the other was not: rc15's two status decisions, the helper that reported `error_type` and the caller that discarded it, and now a payload field and the filter it passes through. Adding a field is not done until every filter between it and its reader has been asked.
- Ten more test files enter the gate, measured green first: 81 assertions over the vector policy, runtime, startup reconciliation, status contract, threshold calibration and write replay, the SQLite writer handoff and its contention, prior release regressions, and zero-signal retrieval. They are hygiene rather than the net that should have caught the vector fault -- that was found by measuring live gaps -- and they are added on that understanding.
- Records what a changeover costs, measured rather than assumed. Replacing the gateway produces about three minutes of `SQLITE_BUSY` (the main database is in DELETE mode, where a writer locks the whole file) and one `candidate_attempt_interrupted` from the evaluation it interrupts. Both are changeover costs and neither is a steady-state fault: twelve consecutive passes either side were clean, and this is the first real occurrence of `SQLITE_BUSY` on this instance in its recorded history. Migrating a 335 MB live database to WAL for a three-minute window per install is not worth its risk; draining the worker before replacing the gateway removes both symptoms at once and is now the recommended order.

### Scope Recall 3.1.0rc24 the watcher and the operator read the same instance - 2026-09-15

- `run_worker` decided its status twice, forty lines apart, and only one of them classified. rc15 fixed the pass-level decision and left the later one restating the rule it had just replaced, under a comment saying exactly why that must not happen. Measured live: 33 items failed, every one `derivation_invalid`, none actionable; the doctor said "attention" while the status file -- what a host agent actually polls -- said "degraded" with an empty gap list. It was wrong and silent at once, and a watcher went through two-day-old logs looking for a cause that was not there. The second decision now classifies using `work_error_counts`, which the status object already carries, and the status file reports `terminal_failed_work` so both readers can show the same split.
- No branch reaches "degraded" without naming its reason. `actionable_failed` carries its count, `background_gaps` were already listed, and `source_only` named nothing -- the same empty-gap shape one branch over, fixed as a code path since no status file has been observed in that state.
- This is rc24 rather than more of rc23 because the behaviour changed. rc23 was tagged and installed, then the fix above was committed under the same version, and for about an hour the repository and the live instance ran different code behind the identical string `3.1.0rc23` -- one with `terminal_failed_work` and one without. Nothing on the instance could tell them apart; the divergence was found by hashing a file, which is not how a running system should have to be identified. The gate now refuses a version that is already tagged at a different commit.

### Scope Recall 3.1.0rc23 run the contracts that were written, and name the vector fault - 2026-09-15

- Name the native vector fault instead of its family. A recall that loses its semantic channel reported `vector_error:RuntimeError`, and the Lance helper raises twenty-three distinct RuntimeErrors -- the helper lock timed out, the worker is closed, the worker exited mid-frame, the worker was never running, a deadline was exhausted, a fence handshake mismatched, the table is not open, lancedb is missing. Measured on one instance: every recall on 2026-09-15 carried that gap and which fault it was could not be recovered from anywhere, because the exception is caught and discarded and the message survives in no log and no table. `core/vector_failure.py` maps each to a token from a closed set; an unrecognised message contributes nothing rather than inventing a category, which the gate caught the first version doing when a test's own wording became `TimeoutError:synthetic_embedding_request_used_its_entire_allowance`.
- Stop discarding the name the helper already gave. `lance_process_store` reads `error_type` from the subprocess, re-raises two classes faithfully, and dropped it for every other failure. Restoring it on the fallback is the whole fix: `core/recall.py` already reads and screens that attribute, so nothing downstream changes and a remote failure now arrives as `RuntimeError:ValueError` rather than as a bare class.
- Run the contracts that were written. Twenty-eight files under tests/contract were selected by no tier: about 350 passing assertions that protected nothing, and four failures no one could see. One of those four was a contract rc18 changed on purpose -- capacity refusals stopped burning an item's attempt -- which should have turned that file red the day it changed, and instead sat unseen for four releases. The four are now correct against the contracts that actually hold, each saying which release changed it and why.
- Make the release gate a superset of CI. `release` is assembled from unit/contract/native/host/migration/packaging and never from `SUITES["integration"]`, so twelve contract files ran in CI and not in the gate that decides whether something ships -- and they were the twelve covering duplicate collapse, the evidence question digest, candidate debounce, corroboration, confirmation, coverage, the embedding budget and its retry, failure classification, the recall probe set, running-code staleness and subject binding. Every module 3.1.0 added. Release now runs 1642 tests where it ran 900; integration 1581 where it ran 1223.
- Guard both holes so they cannot reopen: a contract file selected by no tier fails the gate unless it is named with its reason, and a contract file that CI runs while release does not fails it too.
- Fix a test whose green depended on run order. It patched the clock by module path, and in the full gate run that path and the caller's own globals stopped being the same object, so the day never rolled over and the assertion failed only when a particular sibling ran first. It now patches the namespace the function reads.

### Scope Recall 3.1.0rc22 withdraw the instance-wide brake, keep the report - 2026-09-15

- Remove the drain page cut that clamped the whole pass to one item whenever `provider_refusals` was non-empty. It was measured against the live ledger during a real outage and was wrong three ways. It removed nothing: `core/worker.py` already drops a work type from `allowed` the moment one of its items returns a rate-limited code, so the refused model was already being asked about once per work type per pass -- it appears in 65 passes at a median of two calls each, while the healthy embedding model reached 39 calls in a single pass. It throttled the wrong work: the cut asked only whether *any* model was refusing, never which one or whether the work being drained routes to it, so that healthy model would have gone from 39 to 1. And it braked long after the wall came down: `provider_refusals` reads a one-hour lookback window, so it keeps reporting for an hour past the last refusal -- including for a model that has since been replaced and is no longer routed to. In the hour after exactly such a switch this instance cleared 168 pending items down to 15; a page of one would have prevented that recovery. That is a counterfactual and is stated as one: the recovery ran between 05:00Z and 06:00Z and rc21 was installed at 06:03Z, so the cut was never actually in place while it happened. The arithmetic for what it would have done is unaffected; the claim that it nearly happened is not available, and an earlier commit message asserted it.
- The refusal is still named in both readers, which was always the part that mattered. What a backward-looking, instance-wide, model-blind statement must not do is steer a forward-looking, per-item decision.
- Pins the mechanism that does the work, so the duplicate cannot come back: a rate-limited code stands its own work type down for the rest of the pass, the per-item capacity backoff decides when each item returns, and neither is an instance-wide brake.

### Scope Recall 3.1.0rc21 name the refusals that happen before the request - 2026-09-15

- Report `model_not_approved:<role>:<model>` from the doctor and the worker status file when a configured route names a model that is not in `approved_models`. This was invisible by construction: `reserve` raises before the request is sent, so the refusal writes no ledger row, and every measurement built on the ledger -- `provider_refusals` included -- cannot see it. Observed live: a provider switch registered the endpoint and the credential but not the name, and nineteen `evaluate_candidate` items deferred for an hour, every hour, without ever saying why. This release changes what is reported, not what the queue does: the item still keeps its attempt and is still never abandoned. The deferral is benign, which was measured rather than assumed: the nineteen were still untouched 45 minutes after the name was registered, because their hourly deferral had not elapsed; when it did, at 05:59Z, all nineteen drained within three minutes to sixteen `done` and three `derivation_invalid`, with `budget_unavailable` reaching zero and not one item abandoned for it. Counted by work_id range (15717-15735), not by differencing the global state totals: those move concurrently while a drain runs, so any single reading of them can straddle two batches -- an earlier count taken that way said ten and two, which does not even add up to nineteen. So the hourly retry costs nothing and self-heals the moment an operator fixes the configuration -- it lasts as long as the mistake does, not forever. What was wrong was only that nothing said why they were waiting. The check reads the configuration rather than the queue, so it also reports on an installation that has not yet queued anything.
- Report `ledger_missing:<name>` from the same two readers when a route that calls models has a ledger path that is not a file. This is the same fault wearing a different code: `reserve` raises `ledger_not_initialized` from `_connect_rw`, it folds into the same `budget_unavailable`, and all three readers go quiet in the same way -- `provider_refusals` returns `[]` because it cannot open the file, `_ledger_headroom` returns `{}` for the same reason, and an allowlist check does not look at files at all. Measured, not reasoned: with an absent path those two return exactly that, and `reserve` raises exactly that. Unlike an unapproved name, this one can arrive by a file being moved rather than by anyone editing the configuration.
- The two checks are one function, `pre_request_refusals`, named for the category rather than for the first member of it found: a refusal raised before the request is sent leaves no ledger row, so no amount of reading the ledger will ever surface it.

### Scope Recall 3.1.0rc20 size the backoff against what the queue actually did - 2026-09-14

- Raise the capacity backoff floor to 30 minutes and its ceiling to 4 hours. rc19 started it at 60 seconds, which was sized against intuition rather than measurement: over eight hours of a real outage the interval between one item's retries had a median of 115 minutes, with 74% between one and two hours and only 2.6% under two minutes. The apparent "a call every 2.5 seconds" was 197 separate items sharing that cycle, not any one of them looping — so a 60-second floor would have made the instance ask *more* often than leaving it alone. Projected against the same queue, the steady state falls from roughly 200 calls an hour to about 50.

### Scope Recall 3.1.0rc19 space the retries, never abandon the item - 2026-09-14

- Space out retries after a provider refuses for capacity: each consecutive refusal doubles the wait, from one minute to a ceiling of one hour. rc18 refunded the item's attempt so an outage could not consume its recovery budget, but left the ordinary 60-second ceiling in place — so 197 live items retried without pause for the whole outage, which is exactly the silent loop this design exists to prevent. The count is kept as a `capacity:N` token in the error code beside the existing `auto_retry:` history, so no migration is needed and an operator reading the row sees it. There is no limit on how many times an item may check, only on how often: the item is never given up on.
- Derive the worker's rate-limit stand-down set from `work_storage.CAPACITY_REFUSALS` instead of restating it. The two agreed about 429 and 503 while disagreeing about 502 and 504.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc18 an outage is not the item's fault - 2026-09-14

- Refund a work item's attempt when the failure was a provider declining to serve anyone (`http_429`, `rate_limited`, `http_502/503/504`). Those say nothing about the payload, unlike a timeout, which a large item can genuinely cause — so the lease never got an attempt. Measured on a live instance, four hours of "monthly usage limit reached, resets in 13 days" pushed 195 items into `failed` at `attempt=3` apiece, each then needing an operator to grant it back by hand; over the provider's full thirteen-day window that is thousands. An ordinary fault still exhausts its budget and still becomes terminal.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc17 the watcher reads the status file - 2026-09-14

- Report a refusing provider in the worker's status file as well as the doctor, from one shared implementation in `runtime/model_budget.py`. A host agent watching an instance polls the status file; naming the refusal only in the doctor left it reading "degraded" with an empty gap list for four hours while the provider answered every call with "monthly usage limit reached".
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc16 a blown deadline degrades, it does not empty - 2026-09-14

- Hydrate enough candidates to fill the requested packet even once the deadline is gone. An optional channel that overruns its allowance used to empty the packet entirely: measured against a live corpus, a vector port that respects its budget and then fails still returns six items, while one that overruns by a single second returns *zero* — the lexical, exact and recent candidates are all in hand and the hydrate loop abandons every one of them. On the instance this was found on, the two gaps appeared together in four of five degraded recalls. The floor is the caller's own `max_items` rather than a fixed count, since hydrating three when the packet holds eight only moves the emptiness to the byte budget; it is capped so a large `max_items` cannot make an overrun worse, and the overrun is still reported.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc15 say who refused, and why - 2026-09-14

- Report a model provider that is refusing most calls, named by the provider's own code: `model_refused:<model>:<code>`. A live instance spent four hours with every worker run marked "degraded" and an empty `capability_gaps` list while the provider answered all 344 calls with "monthly usage limit reached, resets in 13 days". Everything needed to say so was already in the ledger and nothing read it. A flake does not qualify; the condition needs a sustained majority of a recent window, and it clears itself when the provider answers again.
- Keep the provider's refusal *type* from a failed response body, and only that: a short symbol like `GoUsageLimitError`, validated against a bounded pattern and screened for secret-like text. The message beside it stays discarded, being free text that may carry account identifiers or URLs.
- Stop reporting a worker run as `degraded` when every failure it met was a by-design terminal outcome, and when an item was auto-retried rather than failed. The doctor already classified `derivation_invalid` as terminal and called such an instance "attention"; the worker called the same state degraded, so an operator watching both saw a permanent fault the health check denied. Both now share `core/failure_retry`'s classification instead of restating it.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc15 one verdict on what is broken - 2026-09-14

- Stop reporting a worker run as `degraded` when every failure it met was a by-design terminal outcome. The doctor already classified `derivation_invalid` as terminal and called such an instance "attention"; the worker called the same state degraded, so an operator watching the two saw a permanent fault that the health check denied. Both now share `core/failure_retry`'s classification rather than restating it. A clearable failure still degrades the run, and a terminal one is still visible in the run's items.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc14 name the failure, not its family - 2026-09-14

- Record SQLite's symbolic error name alongside the exception class when a drain fails, so `worker_error:OperationalError` becomes `worker_error:OperationalError:SQLITE_BUSY` or `:SQLITE_ERROR`. A live instance reported the bare family for days: contention worth fixing and a query bug arrive as the same string, and only one of them is the plugin's problem. The symbolic name is a bounded vocabulary, unlike the message, which may carry paths.
- Record the auxiliary `error_type` on a vector-channel failure for the same reason: every auxiliary failure is an `AuxiliaryModelError`, and whether the connection or the request failed decides both whether the read path's one retry applies and whether an operator should act.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc13 a new question, not a new clock - 2026-09-14

- Replace the evaluation timer backoff introduced in rc12 with a content test. The backoff doubled the wait after each fruitless verdict, which bounds re-asking by *elapsed time* -- so a candidate that had just received exactly the testimony that would settle it still had to wait out a clock. That is a rate limit, not a loop guard, and this project does not cap usage.
- Key the evaluation dedup on the *question* rather than the byte-exact evidence selection. The `UNIQUE(candidate, revision, evidence_fingerprint, rule_version)` guard already existed to stop a question being asked twice, but the fingerprint covered a recency window (`ORDER BY observed_at DESC LIMIT 16`), so one arriving tool observation displaced an older source and the key never collided. Replayed over a live store, 71% of all model judgements were the same question re-asked.
- Order candidate evidence first-hand first, so a person's statement is never displaced out of the window by tool output; new testimony is therefore judged at the first opportunity, with nothing between it and a verdict.
- Drop accumulating non-first-hand support as a second trigger. It was included on the reasoning that eight tool observations differ from four; the history disagrees -- re-judgements bought by that rule came to 955 model calls that produced exactly one conclusion.
- Refuse reinjected memory as candidate evidence (carried from rc12): it is this store's own recalled output returning, adding nothing it did not already hold.
- Stop reporting a money figure in the doctor. What an instance spent depends on the operator's own contract, so a currency amount means something different to every reader; calls and tokens are the same unit for everyone and are still reported, uncapped. The ledger still meters charges internally for the `meter_breach` anomaly check.
- Tolerate small clock skew when deciding whether a package was rewritten after a process loaded it, so an ordinary rebuild-and-reinstall is still detected.
- Protocol 1.1 and schema 1108 are unchanged.

### Scope Recall 3.1.0rc12 evaluation backoff - 2026-09-14

- Back off the candidate evaluation timer after a verdict that decided nothing. The settle window stopped a candidate being re-judged *while* evidence arrived, but a candidate whose trigger terms keep being hit never goes quiet at all, so it fell to the deferral limit every hour: measured on a live instance, 788 model judgements across 269 candidates in one afternoon, 93% `insufficient_evidence`, the median gap between two judgements of the same candidate 73 minutes. The evidence fingerprint differed every time, so the at-most-one-queued rule could not collapse them. Each fruitless verdict now doubles the next timer wait, to a ceiling of 24 hours; projected against the live candidate set that is 6,528 timer judgements in 24 hours down to 301. The quiet path is deliberately untouched, so a candidate that gains real support is still judged at the first opportunity.
- Refuse reinjected memory as candidate evidence. It is this store's own recalled output coming back, so it adds nothing the store did not already hold, and it keeps a candidate from ever settling; it accounted for 16% of the evidence rows behind the re-judgement churn.
- Ship the judged recall probe set in `tests/eval/recall_probes.py` with a gate test for its shape. The 8/8 and MRR 0.833 figures were previously only reproducible from a scratch directory.
- Ignore a package modification time in the future when deciding whether a process is stale. An extractor that mishandles the archive's local-time entries pushed 31 of 135 files four hours ahead, which would have reported every process stale forever.
- Version bumped from rc11 because three different builds carried that number while it was being iterated on; nothing with `3.1.0rc11` was released.
- Protocol 1.1 and schema 1108 are unchanged. No memory rewrite, queue purge or vector rebuild is required.

### Scope Recall 3.1.0rc11 bounded reads and distinct packets - 2026-09-14

- Bound the embedding input. Six sources on a live instance were permanently unembeddable at 16,505 to 65,536 characters, rejected with a code that is not auto-recoverable, so their text reached SQLite and the lexical index but never the vector index. Oversized bodies are now truncated with a visible marker rather than refused; stored memory is unchanged.
- Collapse retrieved content that is byte-identical, after ranking and before the budget, across both the query and background passes. De-duplication was keyed on `(kind, ref, revision)`, which is identity rather than content; on a store where an import re-delivered the same bodies under fresh identities, one document held four of six delivered slots and the answer ranked twelfth was never delivered. Two revisions of one object are not copies and both survive.
- Report every bounded read that cut something: truncation carries the stage and both counts, and a collapse carries how many copies were folded. A collapse is not truncation and keeps its own vocabulary.
- Count `derivation_invalid` as terminal for every work type, not only `consolidate`. Identical failures on candidate evaluation were driving a permanent `degraded` that no operator could act on.
- Derive the operator-clearable failure set from the worker's own transient set instead of listing it twice. The two had drifted: `model_unavailable` was auto-recoverable but absent from the operator list, so rows left over from one outage pinned an instance at `degraded` with no way to clear them.
- Add `requalify` and `retry-failures` maintenance commands, both defaulting to a read-only preview and both idempotent within a schema generation. Re-judging never re-opens a claim a person confirmed or that independent first-hand sources corroborated.
- Scale the suite watchdog to the number of selected test files. The integration tier grew past a flat 180-second bound and began reporting a timeout instead of a result.
- Refuse the release tier early, naming the gap and the remedy, when the package is not installed as a distribution. The doctor's entry-point probe runs isolated and cannot see `PYTHONPATH`, so a fresh checkout previously failed on an assertion that named neither cause nor cure.
- Retry the recall query embedding once when the connection failed rather than the request, budgeted from what the first attempt left and refused when too little of the deadline remains. A single transport blip previously cost the whole semantic channel for that recall; it was reported in `gaps` but not recovered. Requests the provider rejected, and timeouts, are still not retried.
- Protocol 1.1 and schema 1108 are unchanged. No memory rewrite, queue purge or vector rebuild is required.

### Scope Recall 3.1.0rc10 recall timeout repair - 2026-09-13

- Read the current SQLite memory epoch without scanning source and work queues at each recall fence, including the Hermes and Codex delivery checks. Fresh transactions, identity verification, deletion and version checks remain mandatory.
- Reserve up to one second (20% of the original deadline) for packet compilation and release. Optional candidate collection shares the smaller retrieval budget; the overall deadline does not increase.
- Keep an oversized first hit from consuming the budget or sole result slot ahead of usable evidence. Standalone oversized hits still produce the compiler's existing expansion diagnostics.
- Protocol 1.1 and schema 1108 are unchanged. No memory rewrite, queue purge or vector rebuild is required for this patch.

### Scope Recall 3.1.0rc5 composite correction closeout - 2026-09-13

- Split a grounded positive correction from an explicitly rejected old value when an extractor combines them. Recover exact sibling rendition assertions mistakenly placed in conditions, through the same evidence-admission path.
- Retire duplicate legacy composite frames only after their canonical replacement is admitted; preserve source text and historical payloads. Add frozen regressions from two actual failed model responses instead of resampling.
- Protocol 1.1, schema 1108 and the installed configuration remain unchanged. This is a bounded literal grammar, not unrestricted semantic entity merging.

### Scope Recall 3.1.0rc4 natural correction fixes - 2026-09-13

- Align uniquely grounded project, subject and predicate frames across extraction and candidate evaluation. Keep applicability conditions and reject ambiguous or unasserted statements.
- Order conflicting human evidence by source time, with capture order only for unknown-time live evidence from the same verified person. Late processing cannot resurrect an older value.
- Add bounded, model-free `repair-claim-frames` maintenance for existing records without rewriting source text or historical payloads. See [candidate notes](docs/3.1.0rc4-closeout.md).

### Scope Recall 3.1.0rc3 one instance acceptance fixes - 2026-09-13

- Route Codex capture through durable ingress and wake recovery for queued captures. Report content-free capture stage, disposition, durability, exception type, error code and elapsed time.
- Revalidate candidate evidence and bind its epoch atomically when beginning the model attempt. Unrelated writes while a candidate waits no longer force a paid result to be discarded; changes during the model call still block publication.
- Preserve protocol 1.1 and SQLite schema 1108. See [candidate notes](docs/3.1.0rc3-closeout.md); real-host receipts are separate from offline verification.

### Scope Recall 3.1.0rc2 integration candidate - 2026-09-12

- Combine the R1 evidence semantics, candidate lifecycle, and Hermes/Codex authorization work on protocol 1.1 and SQLite schema 1108.
- Preserve the candidate evaluator and its deadline through the runtime wrapper. Continue bounded evidence matching after the first 16 candidates, including after restart.
- Apply the source's exact scope/project/branch before limiting candidate matches, preventing visible but ineligible candidates from consuming every evidence page.
- Retry explicit temporary provider rejections through the existing three-attempt limit; retain failure history and the uncertain-outcome retry fence.
- Accept evidenced Chinese old-to-new corrections while preserving negation, uncertainty, subject binding, and old-value rejection.
- Include the four new runtime modules in the wheel allowlist and repair two stale integration fixtures. See [candidate closeout](docs/3.1.0rc2-closeout.md) for acceptance limits; this candidate has not been publicly released or deployed to a running assistant.

### Fixed
- Keep two-character Chinese content units in interrogative queries so a natural “who wrote which project” question can admit the same row as an exact title lookup. Window n-grams that glue light verbs onto question words no longer erase those units. A shared personal name or high vector score cannot admit a row when the query’s mixed letter-and-digit identifier (such as WSL2) is absent from that candidate; ordinary English category nouns stay on the baseline lexical scorer.
- Keep `_internal/recall` free of sqlite3 and Provider host binding: request SQLite busy-timeout and source collectors live on outer adapter modules, while the orchestrator receives a typed capability port from RecallService.
- Keep duplicate Event Digest candidates observation-only: do not refresh existing memories or enqueue companion work, and report no-touch duplicates without a misleading candidate audit.
- Preserve the original candidate-batch failure when savepoint startup or a post-release commit fails; roll back only the transaction owned by the batch.
- Apply the same lifecycle whitespace and case normalization in Python and SQLite, including historical padded metadata, while retaining existing missing/unknown-token visibility.
- Honor explicitly configured zero metadata, entity-overlap, and entity-distance ranking weights; share effective weight validation with diagnostics.
- Carry the remaining foreground recall budget through source collection, provider and helper locks, SQLite busy waits, embedding requests, and helper responses. Recoverable source failures remain distinguishable from missing evidence; integrity failures are not converted into successful recall.
- Preserve valid L4 verdicts when a model exceeds the 120-character reason budget; sanitize and bound the explanation after strict verdict/schema validation (#65).
- Reject duplicate JSON response fields instead of accepting the last of contradictory L4 verdicts, found during the accompanying protocol audit.
- Requeue orphaned relation-frequency failures from current SQLite truth with bounded retries and supersession evidence. Writer-handoff preflight vetoes no longer resume a writer that was never quiesced (#66).
- Supply an identifiable Scope Recall User-Agent for shared HTTP transports, including OpenAI-compatible and Anthropic nightly requests, while preserving provider-specific headers (#67).
- Add live `scope_recall_memory` candidate `promote` and `archive` actions through the gateway's admitted writer. Plans default to dry-run, apply supports revision checks, scope and Fact authority remain enforced, and lifecycle/audit/vector intent commit atomically. Store receipts now report the persisted candidate lifecycle (#68).
- Preserve a successful store result if its post-commit lifecycle receipt cannot be read. Return the committed id with lifecycle `unknown`, emit a content-free diagnostic, and never retry that successful write.
- Keep Windows LanceDB/PyArrow operations in a private persistent helper process and load local sentence-transformers only when selected. Native helper failure or timeout becomes a recoverable companion error while SQLite truth and pending outbox work stay in the Hermes process (#69).

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
