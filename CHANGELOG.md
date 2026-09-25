# Changelog

All notable changes to `scope-recall` will be documented in this file.

## [Unreleased]

### Scope Recall 3.3.0rc3 - 2026-09-24

- The version moves past the `v3.3.0rc2` tag.

### Scope Recall 3.3.0rc2 - 2026-09-24

- The version moves past the `v3.3.0rc1` tag.
- A digit run glued to other text no longer reads as a Telegram bot token. A Codex source key ends its hex installation id in eight digits for about one installation in 45 and runs on into the session UUID; the key matched the token pattern, and every capture of such an installation was refused as a secret without a word. It was also the intermittent failure of `test_mcp_stdio_all_tools_and_host_thread_bound_mutations`, which draws a new installation id each run. None of the pilot's installations has such an id.
- A client entry's session start reads nothing of the store. It counted the whole store as a local installation does, 7-8 s on the pilot's shared store, past the 2 s Codex gives a hook, at every Codex session start.
- A candidate's evaluation window ranks and names a source by its effective origin, as the promotion rules read it: a person's message that came in with an imported store is first-hand. Ranked by the stored column it sat behind every newer tool output, and the question it posed did not count it (found in the 3.2.0 audit). On the pilot's store no waiting candidate holds such a message yet (80 of 489,203 evidence rows point at imported sources, none a person's), so no evaluation is asked again because of this.
- Claude Code's prompt hook runs the entry's `hook_processing_seconds` (at most 6 s; Claude Code waits 15 s) from the start instead of the 2 s Codex's hooks get. Recall on the pilot's shared store took 2.7-5.7 s, so with 2 s most automatic recalls came back empty. The prompt waits that long before the model answers; lower `hook_processing_seconds` in the entry's runtime config to trade recall for speed.

### Scope Recall 3.3.0rc1 - 2026-09-24

- The version moves past the `v3.2.1rc1` tag, to the minor version in which Codex and Claude Code join a shared store.
- Codex and Claude Code attach to a shared store as entries ([docs/shared-store.md](docs/shared-store.md)). A local client brings no grants of its own: `attach --host codex|claude-code` gives it the owner grants of attached Hermes entries (`--grants-like`) and captures into a scope every owner row reads (`--capture-like`), so what the owner types into it reaches every agent and no other entry's binding changes. The Codex adapter serves both clients; `--home` replaces `--config` for an attached home, whose audience does not depend on the workspace, and its prompts are the owner's, verified by the attach the way the Hermes CLI's are.
- `apply-install --host claude-code` writes a Claude Code plugin (hooks, MCP stdio server, the memory skill) into `~/.claude/skills/scope-recall`, which every session of that user loads. Prompts and final replies are recorded; tool calls are not. Changing a memory through Claude Code's MCP tools is refused: they carry no conversation id. `doctor --host claude-code` checks it. `--project-root` is needed only by a Codex that keeps its own store.
- An entry's runtime config searches the shared worker's vector table. An entry whose routes came from a store that named its table otherwise searched a table nothing fills, and its recall lost the vector half; tianji's did, from its own 3.1 store, after it joined the pilot store on 2026-09-24.
- An `attach` that leaves the shared worker's config as it was no longer rewrites it: any write makes the running worker restart.

### Scope Recall 3.2.1rc1 - 2026-09-24

- The version moves past the `v3.2.0` tag.
- The shared store's commands read a worker runtime config sized for every scope a store may hold. The worker's config lists each scope twice, about 120 bytes a scope, and `attach`, `detach` and `adopt` read it only up to 64 KB, which the pilot's 221 scopes had nearly reached (58 KB). Rehearsing two more instances on copies of the live stores, the first attach took the store to 332 scopes and 85 KB, and the second was refused with `runtime-config.json is missing or too large`; `detach` and `adopt` would have been refused as well. The limit is now 1 MB, about four times what `MAX_SHARED_SCOPES` (1024) needs, and `attach` refuses before it writes a config it could not read back. The worker and its wake never had the 64 KB limit, so no running store stopped.

## [3.2.0] - 2026-09-24

3.2.0 lets several agents keep one memory. Until now each agent had a store of its own, and what the owner told one of them the others could not recall. A shared store is one store that several Hermes agents read and write, each attached as an entry: what the owner tells one agent, another can recall, and the recall says which agent it came in through; a deletion through any of them is gone for all of them; and moving the memory to another machine is copying one directory. `import-entry` brings each agent's earlier memories along. Upgrading does not make a store shared: an agent that is not attached keeps its own store. Only Hermes agents attach so far; Codex keeps its own store. How to set one up is [docs/shared-store.md](https://github.com/410979729/scope-recall-hermes/blob/v3.2.0/docs/shared-store.md). We have run three of our own agents on one shared store since 2026-09-23, with their earlier stores imported (about 87,000 sources), and much of what follows is what that turned up.

These reach every store, shared or not:

- **A tool's output is kept, but no longer turned into facts.** It is still found by its words and by meaning. On our store 2,946 of 3,175 claims rested on tool output alone -- file sizes, paths, ports -- and none of them answered any of the owner's 30 real questions. A question worded exactly like one of those claims is now found less often; its entry below has the numbers. How a task was done belongs to the host's skills.
- **Recall searches every scope an agent may read, in one vector request,** instead of one scope at a time until its budget ran out, and a recall is no longer emptied because something new was captured while it was being read.
- **A memory's time is shown in the host's time zone,** not in UTC, which a model took for its own local time.
- **A name given for the first time is kept as a fact** ("my cat is called ..."). It used to be filed as an alias, which nothing could ever confirm, so no agent recalled it.
- **Embedding is faster and holds up better:** claims go a hundred to a request, and a provider's brief refusal no longer spends the attempt of every item that was waiting.
- **Five reports from other installs are fixed:** long Chinese text the embedding provider refused (#125); Feishu sessions refused for carrying two ids of the same sender (#116); WeChat, Feishu and Desktop-login sessions refused because of the session key their gateway sends (#124); a store refused without a word after a 2.0 plugin had opened it (#117); and a watchdog flag that could delete the runtime config (#118).

Before the tag, four reviewers who had written none of this read everything since 3.1.2. None found a memory reaching a reader outside its scopes; what they did find is fixed, and listed first below.

**Upgrading from 3.1.x.** Stop the host and its worker and take a `backup`. Install the package, run `plan-install` and `apply-install` for each host as after any upgrade, start the host again and run `doctor`. The first open by 3.2.0 moves the store from schema 1109 to 1110: two columns with a constant default and one new table, no row rewritten, though SQLite reads every source row as it adds the column, about 3 s a gigabyte. On a store above 100 MB that open is the worker's next pass, `apply-install`, `upgrade-store` or a Hermes session starting; a hook reports `upgrade_pending` until then. The step is one way: a 3.1 process refuses a 1110 store (`SCHEMA_UNSUPPORTED`) without touching it, so upgrade everything that opens one store together, and going back means restoring that backup. Then, if you want them, three one-off commands, each working a bounded page at a time and described in [docs/install.md](https://github.com/410979729/scope-recall-hermes/blob/v3.2.0/docs/install.md):

- `retire-rootless-claims` lists the unconfirmed claims an earlier release derived from tool output alone, and with `--apply` retires them; confirmed claims, every source, and a proposal a person has since restated stay.
- `retry-failures --apply` re-opens the embeddings that failed with `http_400` on long Chinese text.
- `repair-claim-frames` re-frames the first-time names an earlier release stored as aliases.

As with 3.1.2, this release ships without the P18 formal acceptance receipt; the figures here are our own measurements.

The entries below are the changes as they were written when each landed.

### What the release audit found

Four reviews of everything since 3.1.2, each by a reader who had written none of it, before the tag. None found a memory reaching a reader outside its scopes; these are what they did find.

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
