# Scope Recall for Hermes

Local, source-backed long-term memory for Hermes. It recalls what you told an agent together with your original
words and their time, keeps corrections as history instead of overwriting them, and can share one store with your
other agents: further Hermes profiles, Codex and Claude Code.

## Install

```sh
hermes plugins install scope-recall
hermes memory setup
```

Select `scope-recall`, then start a new session. Setup creates `~/.scope-recall/shared` or reuses the home's
existing attachment, installs the memory skill and sets `memory.provider` in `config.yaml`, leaving every other
line unchanged. A new installation uses original-message retrieval with model routes disabled and a zero budget.
Its memory is yours in the Hermes CLI and in Hermes Desktop on this machine; `hermes --tui` needs your approval
first ([local surfaces](../../docs/install/hermes.md#local-surfaces)).

## Tools

- `recall`: what is remembered for the current question, current or as of a date, with sources.
- `inspect` and `trace`: one remembered item or its source message, and how an item was derived.
- `revise` and `forget`: correct a remembered fact, keeping the old version as history, or delete with a preview
  of everything the deletion takes with it.
- `profile` and `entity`: current facts by category, or about one person, project or object.
- `handoff_write`, `handoff_list`, `handoff_accept` and `handoff_cancel`: pass work to another agent on the same
  store.
- `status`: store status and any capability the current setup lacks.

## What it does on your machine

- **Storage**: captured messages and the memories derived from them live in a local SQLite store under
  `~/.scope-recall/shared`. Nothing leaves the machine by default.
- **Network**: none by default. If you configure an embedding or consolidation model route, excerpts of captured
  messages are sent to that endpoint, within the budget you set.
- **Credentials**: keys for configured routes are read through Hermes's own secret source for the active profile.
  The plugin never reads `.env` or other credential files and stores no keys.
- **Background work**: short, bounded worker processes started by Hermes activity. No system service, login item or
  scheduled task is registered.
- **Files outside the plugin directory**: the store, the memory skill in the Hermes home, and the `memory.provider`
  line of `config.yaml`.

## More

[Hermes installation guide](../../docs/install/hermes.md) · [Full documentation](../../README.md) · MIT license
