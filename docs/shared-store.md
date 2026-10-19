# One store for every agent

A shared store is one Scope Recall store that several agents read and write. Each agent is an
*entry* of it: its home holds only a pointer, and every source it captures is marked with its
entry. What the owner tells one agent, another can recall, and the recall says which agent it
came in through. A deletion through any entry is gone for all of them. Moving the memory to
another machine is copying one directory.

A home without a pointer keeps its own store and binds as its own installation says.

## What is where

```
<root>\                       the shared store; everything memory needs is in here
  installation.json           the store's id, and every entry's grants
  memory.sqlite3              the one store (schema 1110 or later)
  vectors\<space>\            vector companions of the one store
  runtime-config.json         the shared worker's config: every scope, the worker's model routes
  .env                        the shared worker's credentials (you write this)
  receipts\                   one receipt per command, and a copy of every file it replaced
  commands.lock               held by the command that writes the store, for as long as it runs
<home>\scope-recall\
  attachment.json             the pointer: the root, the entry id, its display name
  runtime-config.json         the entry's own model routes, used for query vectors at recall
<home>\scope-recall-attachment.lock
                              held by the command that reads or changes the pointer, while it runs
```

The store's id is drawn once, when the store is made, and is not a path. A copied store opens
nowhere until `adopt` records its new directory; opening it before that fails with
`store_moved:run_adopt`.

Each entry keeps the grants its own installation had: the same audience rows, so every chat
reaches the same scopes it reached before. Entries meet where their scopes are the same
strings, which for installations migrated from 2.x includes the owner's private scopes.

## Make a store and attach the first agent

Run the operator CLI from outside any source tree (`python -I -X utf8 -m scope_recall.maintenance.cli ...`).
Put the store outside every agent's home, on a short path.

```text
scope-recall init-shared --root D:\ScopeRecall\shared
```

The first agent may be a Hermes instance, as below, or a client, so that clients share the store
with no Hermes at all ([A store of clients alone](#a-store-of-clients-alone)).

To attach a Hermes instance that has its own store today, stop its gateway first (and its
worker: `autostart pause`), then move its `scope-recall` directory aside and attach:

```text
scope-recall backup --database <home>\scope-recall\memory.sqlite3 --output <backup-dir>\memory.sqlite3
ren <home>\scope-recall scope-recall.local-20260922
scope-recall attach --host hermes --instance-root <home> --root D:\ScopeRecall\shared ^
    --entry desk --display-name Desk ^
    --grants-from <home>\scope-recall.local-20260922\installation.json ^
    --runtime-config-from <home>\scope-recall.local-20260922\runtime-config.json
```

`--grants-from` carries over that installation's audience rows and owner principals, unchanged.
Without it the entry gets a fresh installation's grants: the CLI only. The old store stays where
you moved it; its memories are not copied into the shared store.

`--runtime-config-from` gives the entry its model routes, bound to the shared store and to the
worker's vector table, with the entry's spend ledger beside its pointer. The first entry attached
with one also gives the shared worker its routes, and its spend ledger moves to the store's
directory. Every later entry must use the same embedding model as the worker: a
query vector from another model searches a directory the worker never fills, so `attach`
refuses with `embedding_space_differs`. An entry attached without a runtime config recalls
lexically only (`vector_recall_unavailable`). The entry keeps the `vector_threshold` of the
config it was attached from; in the shipped embedding space a shared store needs `0.70`
([configuration.md](configuration.md#vector_threshold)).

Then give the shared worker its credentials in `<root>\.env` and register it once, with an
interpreter that has the same package version as every entry:

```text
scope-recall autostart enable --config D:\ScopeRecall\shared\runtime-config.json ^
    --python D:\ScopeRecall\shared-venv\Scripts\python.exe --env-file D:\ScopeRecall\shared\.env
```

An entry never starts a worker of its own. Leave the instance's own autostart paused or
removed; the shared store's worker does all background work.

Attaching another agent later adds its scopes to the store. Agents already running keep working
without a restart. A command that writes the store (`init-shared`, `attach`, `detach`, `adopt`,
`import-entry`), and `grants`, holds its `commands.lock` from its first read
to its last write, so no other writes in between. One that finds it held waits up to 10 s, then
writes nothing and names the command that holds it: run it again once that one has finished.
`attach`, `detach` and `grants` hold the home's `scope-recall-attachment.lock` as well, taken
before the store's, so that no other command points the home at another store meanwhile; `detach`
and `grants` read the home's pointer again once they hold both, and refuse, writing nothing, when
it names another store or entry by then. An `attach` run again from a home already attached (one
that stopped, or after `adopt`) keeps the entry's grants as they are and needs no grant options
([Changing an entry's grants](#changing-an-entrys-grants)).

## Bringing an agent's memories along

`attach` starts an agent on the shared store without what its own store held. To bring that in,
stop every attached host (and a WorkBuddy or dsh entry's resident recall server: `scope-recall resident stop`) and
pause the shared worker, then run for each agent:

```text
scope-recall import-entry --root D:\ScopeRecall\shared --entry desk --from <home>\scope-recall.local-<date>
```

The old store is opened read-only and copied in one transaction: sources, facts and their history,
episodes, candidates, deletions and blocks. Every imported source is marked with the entry and
gets an id of its own, since stores migrated from 2.x can share ids for different content; its
key and session take the entry's prefix, and what the old store had forgotten stays forgotten.
A fact whose slot is already filled is left out, and the receipt under `receipts\` names it; its
sources are imported. A fact whose current version is a denial the old store took for its value
("not CircleCI") is settled in the same transaction, as `restore-displaced-values` settles it,
and the receipt counts them (`denials_settled`). No vector is copied: the embeddings the old
store had are queued for the shared worker, and until it has made them those memories are found
by their words.
`--dry-run` runs the whole import and rolls it back. A store is imported once per entry; a
second run is refused, and so is a store that was not this entry's home's. It reads a 3.x
store; a 1.x or 2.x store is migrated into the agent's home first (`scope-recall migrate`,
[maintenance/AGENT_WORKFLOW.md](../maintenance/AGENT_WORKFLOW.md)).

## Attach Codex or Claude Code

A local coding assistant brings no grants of its own: whoever types into it on this machine is
the owner, as at the Hermes CLI. `attach` gives it the owner grants of entries already attached,
Hermes homes and other clients alike (`--grants-like`, their ids comma separated, or `all`): it
reads every scope their owner rows read and may write where they may. What the owner types into
it is captured into the scope another entry's owner captures into (`--capture-like`), which must
be one every owner row of every attached entry reads, so that every agent hears it; otherwise
`attach` refuses and names the rows that would not. No other entry's grants change and the
worker keeps running. On a store no other entry is attached to, leave both options out: the
client brings the owner's scope ([A store of clients alone](#a-store-of-clients-alone)).

```text
scope-recall attach --host claude-code --instance-root D:\ScopeRecall\claude-code ^
    --root D:\ScopeRecall\shared --entry claude-code --display-name "Claude Code" ^
    --grants-like all --capture-like desk ^
    --runtime-config-from <an attached home>\scope-recall\runtime-config.json
scope-recall apply-install --host claude-code --target-plugin-dir %USERPROFILE%\.claude\skills\scope-recall ^
    --instance-root D:\ScopeRecall\claude-code --agent-id <the store's agent id> ^
    --python D:\ScopeRecall\claude-code-venv\Scripts\python.exe --env-file <the file with the embedding key>
```

The plugin under `~/.claude/skills/` loads in every new Claude Code session of that user, the
desktop app's included; `claude plugin disable scope-recall@skills-dir` stops it. Its hooks record
each prompt as the owner's and each final reply as Claude Code's, and put what is remembered in
front of the prompt. What the model still has word for word is not put there again: an item that
stands only on this session's messages since its last compaction (the session record names it; with
none, since the session began) is left out, and other memories take its place. What was said before
a compaction reaches the model only as the compaction's summary, and is recalled. The
reminders Claude's desktop app puts in a message (a `<system-reminder>` block that opens a line) are
not the owner's words: they are neither recorded nor recalled for.
A turn is recorded under its prompt id, which Claude Code sends from 2.1.196.
At the end of each turn the Stop hook also reads the session record Claude Code keeps (the hook's
`transcript_path`) from where the last read stopped, and records what it shows being said: the
owner's messages, those sent while a turn was running included, and the text Claude Code shows
while it works. Tool calls and results, compaction summaries, task notifications and anything the
record does not mark as the owner's or as shown text are not recorded. A background task's
completion notice, which Claude Code hands to the model as a prompt, is not the owner's and is
neither recorded nor answered from memory. A message a hook already stored, or one still waiting in
the capture inbox, is recognised by its prompt id (one sent while a turn was running, which has none,
by its words and moment) and not stored twice; one that cannot be written now is written at a later turn. A long session is read over several turns, at most 3 s each. Where a
read stopped is kept in `<home>\scope-recall\transcripts`; deleting it only makes the next read
start from the top. Claude Code runs a hook command through a shell (Git Bash, or
PowerShell without it), so keep the interpreter, the home and the env file on ASCII paths without
spaces; `apply-install` refuses others. Its MCP tools read the store. The three that would change
it (`propose_memory`, `revise`, `forget`) are not offered: the store takes such a change only on the
word of the person's latest message in the same conversation, and Claude Code does not tell the tools
which conversation a call comes from, so the store would refuse each. Correct or delete through a Hermes
agent or Codex; the store is one, so the change holds for every entry. Each answer of the tools that
are offered names the gap `mcp_session_is_not_claude_code_conversation_id` (`..._workbuddy_...`,
`..._dsh_...` on those entries): the call is tied to no conversation. It is expected and needs
nothing.

Each hook is a process of its own, and starting LanceDB in a prompt hook can exceed the recall
budget. The entry's MCP server, which the client runs for as long as it is open, keeps a
LanceDB helper ready and answers the recall of the entry's prompts on this machine. The prompt hook
still stores the prompt itself, then asks the server for its recall; the server writes nothing. It
listens on 127.0.0.1 and names itself, with a token, in a folder of the user's own profile
(`%LOCALAPPDATA%\scope-recall\hook-endpoints\`, or `~/.cache/scope-recall/hook-endpoints/`), not in
the entry's home, and removes its name when it exits. A hook asks the newest server of its own
version, once that server has proved it holds the token, which is never sent. The other hooks read
no vectors and ask nothing.

A prompt hook gives the server all of its time but the answer's way back, whenever at least 1 s is
left. If the server has not answered when 1.5 s are left, the hook recalls as well, with the LanceDB
helper it started when it started, and uses the answer that ran its vector search (the server's when
both or neither did); a hook whose own recall went without it waits for the server until its own time
is up. A server answer without its vector search is used as it is, unless what failed was the
server's own (its key, its LanceDB helper, or no vector search at all while the hook has one): the
hook then recalls as well and uses its own if that ran its vector search. An embedding call's failure
(what the provider answered, the network, the time it took) the hook would meet as well.
With no server running (the client closed, or its MCP server disabled), or one that says it is busy,
the hook recalls itself at once; an answer that comes after the hook is done is dropped,
and one that failed, or came back empty because its read did not finish (the store unreadable, or its
time up), is replaced by the hook's own. A prompt
hook that asked says how it went on stderr (`CODEX_RECALL_RESIDENT:answered`, `slow`, `late`,
`without_vectors:<gap>`, `busy`, `refused`, `unproven`, `none`, or `failed:<reason>`). The server
writes no memory; its query embedding's cost is recorded in the spend ledger like any other, and so is
the hook's when both recalled. A server with a recall past the time its hook gave it tells every hook
that it is busy until that recall ends, and one that does not prove itself within 0.5 s loses its name
until its own check, every 2 s, finds it answering in time. The server keeps the handler it recalls with from
one prompt to the next, keeping its LanceDB table open and its embedding worker connected to
avoid reopening them for each prompt. One recall uses the handler at a time; another that comes meanwhile is
recalled by a handler of its own. It is made anew when the env file or the runtime config changed,
after a recall that raised, or when its runtime could not be attached. A recall that fails in the server is answered as
failed, with the last frames of its traceback on the server's stderr (the client's MCP log). It reads its key again at the next prompt
after its env file or the runtime config changed, or when it could not read them before. A server
started before an upgrade is not asked until its client restarts; a resident recall server ends itself
within 30 s of the upgrade ([install.md](install.md), section 12). Each open client keeps one LanceDB
helper, which its kept handler, the handlers made for prompts that come meanwhile and its tools share.
The system pages it out while it is idle, and the server searches it once more after each 10 minutes
without a recall that searched it. Each prompt hook still starts one of its own, which
ends with the hook, once its import is done, when the hook did not need it.

Claude Code or Codex on another machine attaches the same way, under a name of its own, and reaches
its entry here over HTTP on a private network: [remote-entries.md](remote-entries.md).

A Codex that keeps its own store today: pause its autostart, move `codex-installation.json` and
`data` aside, attach it with `--host codex` (its routes are the moved `data\runtime-config.json`),
and run `apply-install --host codex` without `--project-root`. Its hooks and MCP server then name
the home and serve every workspace. Refresh Codex's plugin cache and approve the changed hooks in
Codex. The moved store's memories are not imported.

`doctor --host codex|claude-code|workbuddy|dsh --instance-root <home>` and `detach` work for these
entries as for a Hermes home.

## Attach WorkBuddy

Every process on the store, its worker included, must run 3.5.0 or later before WorkBuddy
attaches: an older one does not know the host, and cannot replay a capture this entry queued.

WorkBuddy attaches the same way, as the owner at this machine, and runs the same hooks and MCP
server, which offers it the same tools as Claude Code, none that changes the store; its installer adds them to WorkBuddy's own `settings.json` and `mcp.json` rather than
writing a plugin ([install.md](install.md), section 12, which also says what is not recorded):

```text
scope-recall attach --host workbuddy --instance-root D:\ScopeRecall\workbuddy ^
    --root D:\ScopeRecall\shared --entry workbuddy --display-name WorkBuddy ^
    --grants-like all --capture-like desk ^
    --runtime-config-from <an attached home>\scope-recall\runtime-config.json
scope-recall apply-install --host workbuddy --instance-root D:\ScopeRecall\workbuddy ^
    --agent-id <the store's agent id> --python D:\ScopeRecall\workbuddy-venv\Scripts\python.exe ^
    --env-file <the file with the embedding key>
```

Quit WorkBuddy before `apply-install` and start it again after, then approve the MCP server
`scope-recall` in WorkBuddy's MCP settings: WorkBuddy starts a server of `mcp.json` only once it
is approved. WorkBuddy 5.6.2 runs that server only with a conversation's agent process, so its
prompt hook asks a resident recall server instead: started by its hooks and its MCP server, and
ended with the conversation's agent process that started it (WorkBuddy's agent ends every process
it started) or `resident_recall_minutes` after the last prompt, whichever comes first
([install.md](install.md), section 12). Its `Stop`
reads WorkBuddy's session record. Take the hooks out with `apply-uninstall` before `detach`;
`detach` alone leaves WorkBuddy's settings as they are.

## Attach dsh

Every process on the store, its worker included, must run 3.7.0 or later before dsh (DeepSeek
Harness) attaches: an older one does not know the host, and cannot replay a capture this entry
queued.

dsh attaches the same way, as the owner at this machine. A plugin the installer writes runs the
same hooks (dsh's own hooks name no turn and no reply), and dsh's MCP client runs the same MCP
server, with the same tools as Claude Code's, none that changes the store; the installer adds both as rows of dsh's home patch, `cordis.patch.yml`, and switches off
dsh's upload of its session logs to its model API ([install.md](install.md), section 13, which
also says what is not recorded):

```text
scope-recall attach --host dsh --instance-root D:\ScopeRecall\dsh ^
    --root D:\ScopeRecall\shared --entry dsh --display-name "DeepSeek Harness" ^
    --grants-like all --capture-like desk ^
    --runtime-config-from <an attached home>\scope-recall\runtime-config.json
scope-recall apply-install --host dsh --instance-root D:\ScopeRecall\dsh ^
    --agent-id <the store's agent id> --python D:\ScopeRecall\dsh-venv\Scripts\python.exe ^
    --env-file <the file with the embedding key>
```

Quit every dsh before `apply-install` and start it again after. Its prompt hook asks the entry's
resident recall server, as WorkBuddy's does, which outlives a dsh process and ends
`resident_recall_minutes` after the last prompt (120 by default). Take the rows out with
`apply-uninstall` before `detach`; `detach` alone leaves dsh's patch file as it is. For dsh on
another machine, attach its entry on the store machine and use the
[remote client installer](remote-entries.md#dsh-on-the-client-machine), not a local `apply-install`.

## A store of clients alone

Clients can share a store that no Hermes home is attached to. The first client attached to a new
store has no entry to take grants like, so it is given neither `--grants-like` nor
`--capture-like`: it gets the owner grants of a fresh installation, the ones a Hermes home
attached without `--grants-from` gets, and brings their owner scope into the store
(`new_scopes: 1`). It reads, writes and captures there as the owner at this machine. Each client
attached after it names it, or any entry attached since, as above. Codex, Claude Code, WorkBuddy
and dsh can each be the first:

```text
scope-recall init-shared --root D:\ScopeRecall\shared
scope-recall attach --host workbuddy --instance-root D:\ScopeRecall\workbuddy ^
    --root D:\ScopeRecall\shared --entry workbuddy --display-name WorkBuddy ^
    --runtime-config-from D:\ScopeRecall\routes.json
scope-recall attach --host dsh --instance-root D:\ScopeRecall\dsh ^
    --root D:\ScopeRecall\shared --entry dsh --display-name "DeepSeek Harness" ^
    --grants-like all --capture-like workbuddy ^
    --runtime-config-from D:\ScopeRecall\routes.json
```

With no attached home to take routes from, `--runtime-config-from` names a runtime config you
wrote with the model routes ([configuration.md](configuration.md)); `attach` binds what it writes
to the store whatever that file's `binding` block names, and the first client attached with one
gives the shared worker its routes. Then install each client as its section says, and give the
shared worker its credentials and register it as for a Hermes entry
([above](#make-a-store-and-attach-the-first-agent)). A first client given `--grants-like` or
`--capture-like` is refused, and so is a later one without them.

A Hermes home attached to such a store later meets the clients in that scope when it is attached
without `--grants-from`, or with the grants of an installation made with the default
`--agent-workspace` (`hermes`): its owner rows read, write and capture into the clients' scope, so
it brings no scope of its own (`new_scopes: 0`), reads what the clients were told, and they read
what it is told, with nothing to change. An installation whose owner rows read and capture
elsewhere (one made with another `--agent-workspace`, say) keeps its scopes: its owner and the
clients do not hear each other, and `attach` refuses a later client that captures where the
clients do, naming that entry's owner rows. Change the grants on both sides
([Changing an entry's grants](#changing-an-entrys-grants)): let each owner row of the Hermes entry
read the clients' scope, and each client's owner row read the Hermes entry's owner scope. A client
attached after that with `--grants-like all` reads both.

## Check

```text
scope-recall entries --root D:\ScopeRecall\shared
scope-recall doctor --host hermes --instance-root <home>
```

`entries` lists each entry, whether its home still points here, and when the store last heard
from it; it reads each home's pointer holding that home's lock, one home at a time. `doctor` on an
attached home reports the shared store it belongs to, the store's health
and the shared worker's registration.

## Changing an entry's grants

An entry keeps the grants it was attached with: its owner principals, the capture scope of each
audience kind, and its exact audience rows. An `attach` run again from the entry's home keeps them
too, a change made here included, with or without other entries attached, and needs none of the
options grants are taken from (`--grants-from`, `--grants-like`, `--capture-like`); given, they are
not used, and its result says so (`grants_kept`). To add a route (a gateway DM of the owner), let a
row read another scope (a conversation another route captured into, or an older scope), or take a
row away, change them from an edited copy:

```text
scope-recall grants --instance-root <home> --out D:\grants-desk.json
rem edit D:\grants-desk.json
scope-recall grants --instance-root <home> --grants-from D:\grants-desk.json
rem stop the entry's host (its gateway, or the client)
scope-recall grants --instance-root <home> --grants-from D:\grants-desk.json --apply
rem start it again, then doctor
```

The first command writes the entry's grants to a new file. The second checks the edited copy and
shows the change and writes nothing: the rows that go and the rows that come, each whole, with
the scopes it reads, writes and captures into (two rows may share a route, and each counts);
whether the rows kept changed order (`rows_reordered`); each route a row names that would read,
write or capture otherwise, as its sessions bind before and after (`routes_changed`: of the rows a
session matches, the first that captures into a scope it may write decides, so the order of rows
counts, and an owner row grants only with its principal); the owner principals; the scopes the
entry gains or gives up; and any owner row that grants nothing.
With `--apply` it writes the store's manifest, and the entry's runtime config when the scopes it
binds change, keeping a copy of each file under `receipts\`, with `grants-before.json`, the
grants as they were: applied the same way, it puts them back. A write that fails, the receipt's
included, puts the copies back (a file it could not put back is named with its copy, to copy by
hand); run again, the command also rebinds an entry config that a failed run left behind. From
reading the home's pointer to its last write it holds the home's lock and the store's
`commands.lock`, so no other shared-store command writes in between
([above](#make-a-store-and-attach-the-first-agent)).

A Hermes entry attached from a home whose installer never approved Hermes Desktop or
`hermes --tui` has no grant for that surface, and `apply-install --local-platform` refuses an
entry; its owner's sessions there bind no scope, and the host log and `doctor`
(`local_session_unbound`) say so. Add the surface as the installer would: the owner principal
`{"platform": "desktop", "user_id": "local"}` and an `owner_private` row on `desktop`, user
`local`, chat type `private`, chat `local`, thread `main`, an empty `gateway_session_key` and the
entry's `agent_workspace`, reading, writing and capturing into the scope the entry's CLI owner row
captures into. That route is also where the conversations Hermes's local session transport runs
for the gateway's own account bind ([install.md](install.md), section 4). An owner's login is
added the same way, the login in place of `local` as the principal's user and the row's user and
chat.

A row matches a session field by field, as at attach: `platform`, `user_id`, `chat_type`,
`chat_id`, `thread_id`, `gateway_session_key` (empty does not pin the host's session key) and
`agent_workspace`; there is no wildcard. An `owner_private` row grants something only when its
platform and user are among the owner principals, so add the principal with the row; one that
names no principal grants nothing, and the change lists it (`owner_rows_granting_nothing`), as
`doctor` does (`audience_owner_unverified`). Write and capture scopes must be among the row's
read scopes. Only scopes the store already holds are named: a scope comes into the store with an
entry that holds it. A change that would leave a scope held by no entry is refused: what is in it
would leave every binding, the worker's included; keep it on a row that only reads, or `detach`
the entry. So the store's own scopes, and the shared worker's config, never change here, and the
grants before a change apply back. A client (Codex, Claude Code, WorkBuddy, dsh) takes whoever
types into it for the owner, so its own route keeps an owner row, of an owner principal, that
reads and captures; a conversation row beside it is a grant like any other. Memories keep their
scopes and their entry; only who reads and writes them changes. Whoever can run this can grant
any route the store's memory, as whoever can write the manifest always could: it is an operator
command, never a host's.

## Detach, and going back

```text
scope-recall detach --instance-root <home>
```

`detach` removes the pointer and the entry's runtime config (copies are kept under
`receipts\`). The entry stays on record: its memories stay in the store and keep its mark. To
return an instance to its own store, detach it, rename `scope-recall.local-<date>` back to
`scope-recall`, install the package version that store was last opened with, re-enable its
autostart and start its gateway.

## Moving the store

1. Pause the shared worker (`autostart pause`), stop every attached host and a WorkBuddy or dsh entry's resident
   recall server (`scope-recall resident stop`), and take a `backup`. The server reads the store, and on Windows its open
   files keep the old root from being removed.
2. Copy the whole root directory to the new place.
3. `scope-recall adopt --root <new root>`. It checks the store is the one its manifest names,
   records the new directory in the store, the manifest and the worker's config, and says what
   to do next.
4. Register the worker again (`autostart enable` with the new config path) and fill in `.env`
   for the new machine.
5. Attach every agent again from its home. An entry whose old home no longer points at this
   store can be attached from a new home under the same id.

## Upgrading

The shared worker and every entry run the same package version. Upgrade the worker's
environment first, then each entry's, and run `plan-install`/`apply-install` on each attached
home as after any upgrade; they recognize the pointer. Until the last one is done, `doctor` on
any entry may report `version_mismatch`. Each running process is judged against the package it
was loaded from, so upgrading one entry never makes another entry's process look stale.

## Not yet

`recall` has no per-entry filter, and a deletion receipt does not list entries.

A deletion an agent's own store made before `import-entry` covers that agent's copies only. The
same thing told to another agent, in the same scope, comes in with that agent's store; delete it
again through any entry once both are imported.
