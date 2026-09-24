# One store for every agent

A shared store is one Scope Recall store that several agents read and write. Each agent is an
*entry* of it: its home holds only a pointer, and every source it captures is marked with its
entry. What the owner tells one agent, another can recall, and the recall says which agent it
came in through. A deletion through any entry is gone for all of them. Moving the memory to
another machine is copying one directory.

A home without a pointer keeps its own store and binds as its own installation says, as before
(3.2.0 changes how some gateway sessions bind for every home; see the changelog).

## What is where

```
<root>\                       the shared store; everything memory needs is in here
  installation.json           the store's id, and every entry's grants
  memory.sqlite3              the one store (schema 1110 or later)
  vectors\<space>\            vector companions of the one store
  runtime-config.json         the shared worker's config: every scope, the worker's model routes
  .env                        the shared worker's credentials (you write this)
  receipts\                   one receipt per command, and a copy of every file it replaced
<home>\scope-recall\
  attachment.json             the pointer: the root, the entry id, its display name
  runtime-config.json         the entry's own model routes, used for query vectors at recall
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
scope-recall init-shared --root F:\ScopeRecall\shared
```

To attach a Hermes instance that has its own store today, stop its gateway first (and its
worker: `autostart pause`), then move its `scope-recall` directory aside and attach:

```text
scope-recall backup --database <home>\scope-recall\memory.sqlite3 --output <backup-dir>\memory.sqlite3
ren <home>\scope-recall scope-recall.local-20260922
scope-recall attach --host hermes --instance-root <home> --root F:\ScopeRecall\shared ^
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
lexically only (`vector_recall_unavailable`).

Then give the shared worker its credentials in `<root>\.env` and register it once, with an
interpreter that has the same package version as every entry:

```text
scope-recall autostart enable --config F:\ScopeRecall\shared\runtime-config.json ^
    --python F:\ScopeRecall\shared-venv\Scripts\python.exe --env-file F:\ScopeRecall\shared\.env
```

An entry never starts a worker of its own. Leave the instance's own autostart paused or
removed; the shared store's worker does all background work.

Attaching another agent later adds its scopes to the store. Agents already running keep working
without a restart.

## Bringing an agent's memories along

`attach` starts an agent on the shared store without what its own store held. To bring that in,
stop every attached host and pause the shared worker, then run for each agent:

```text
scope-recall import-entry --root F:\ScopeRecall\shared --entry tianshu --from <home>\scope-recall.local-<date>
```

The old store is opened read-only and copied in one transaction: sources, facts and their history,
episodes, candidates, deletions and blocks. Every imported source is marked with the entry and
gets an id of its own, since stores migrated from 2.x can share ids for different content; its
key and session take the entry's prefix, and what the old store had forgotten stays forgotten.
A fact whose slot is already filled is left out, and the receipt under `receipts\` names it; its
sources are imported. No vector is copied: the embeddings the old store had are queued for the
shared worker, and until it has made them those memories are found by their words.
`--dry-run` runs the whole import and rolls it back. A store is imported once per entry; a
second run is refused, and so is a store that was not this entry's home's.

## Attach Codex or Claude Code

A local coding assistant brings no grants of its own: whoever types into it on this machine is
the owner, as at the Hermes CLI. `attach` gives it the owner grants of Hermes entries already
attached (`--grants-like`, their ids comma separated, or `all`): it reads every scope their owner
rows read and may write where they may. What the owner types into it is captured into the scope
another entry's owner captures into (`--capture-like`), which must be one every owner row of
every attached Hermes entry reads, so that every agent hears it; otherwise `attach` refuses and
names the rows that would not. No other entry's grants change and the worker keeps running.

```text
scope-recall attach --host claude-code --instance-root F:\ScopeRecall\claude-code ^
    --root F:\ScopeRecall\shared --entry claude-code --display-name "Claude Code" ^
    --grants-like all --capture-like tianshu ^
    --runtime-config-from <an attached home>\scope-recall\runtime-config.json
scope-recall apply-install --host claude-code --target-plugin-dir %USERPROFILE%\.claude\skills\scope-recall ^
    --instance-root F:\ScopeRecall\claude-code --agent-id <the store's agent id> ^
    --python F:\ScopeRecall\claude-code-venv\Scripts\python.exe --env-file <the file with the embedding key>
```

The plugin under `~/.claude/skills/` loads in every new Claude Code session of that user, the
desktop app's included; `claude plugin disable scope-recall@skills-dir` stops it. Its hooks record
each prompt as the owner's and each final reply as Claude Code's, and put what is remembered in
front of the prompt; tool calls are not recorded. A turn is recorded under its prompt id, which
Claude Code sends from 2.1.196. Claude Code runs a hook command through a shell (Git Bash, or
PowerShell without it), so keep the interpreter, the home and the env file on ASCII paths without
spaces; `apply-install` refuses others. Its MCP tools read the store. Changing a memory through
them is refused, because the tools cannot tell which conversation asks: correct or delete through
a Hermes agent or Codex.

A Codex that keeps its own store today: pause its autostart, move `codex-installation.json` and
`data` aside, attach it with `--host codex` (its routes are the moved `data\runtime-config.json`),
and run `apply-install --host codex` without `--project-root`. Its hooks and MCP server then name
the home and serve every workspace. Refresh Codex's plugin cache and approve the changed hooks in
Codex. The moved store's memories are not imported.

`doctor --host codex|claude-code --instance-root <home>` and `detach` work for these entries as
for a Hermes home.

## Check

```text
scope-recall entries --root F:\ScopeRecall\shared
scope-recall doctor --host hermes --instance-root <home>
```

`entries` lists each entry, whether its home still points here, and when the store last heard
from it. `doctor` on an attached home reports the shared store it belongs to, the store's health
and the shared worker's registration.

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

1. Pause the shared worker (`autostart pause`), stop every attached host, and take a `backup`.
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
