# A client on another machine

Claude Code or Codex on a second machine (a work computer) can be an entry of the shared store on this
one. Its memories are the store's, recalled by every entry with the owner's grants, and each recalled item
names the entry it came in through, so "Work Claude Code" stays apart from this machine's Claude Code.
WorkBuddy can be such a client as well (`--host workbuddy` below, `"host": "workbuddy"` in `client.json`); it is
forwarded and its session record read like the `claude-code` host's, and where its setup differs is said below.
dsh can also be a remote entry (`--host dsh`, `"host": "dsh"`); its native plugin sends the turn's messages
inside the Stop payload instead of reading a transcript file.

Nothing of the store moves. The client machine runs a small forwarder and holds only the entry's token:
each hook goes to the entry's server here over HTTP, and this machine's handler records it, as it does for a
local client. A Claude Code client reads its own session record on its machine and sends what the record
shows being said, never the record itself; the server opens no path a request names. The entry's MCP tools
are served over streamable HTTP. Tool calls and tool output are not recorded: the remote plugin does not
forward them, where a local Codex client records its tool calls.

The server listens on one private address this machine has on a network both machines are in (a tailnet),
never on every interface, and refuses any request without the entry's token. The token is made on the client
machine and stays there; this machine keeps its SHA-256.
Plain HTTP is accepted, including non-loopback addresses: use it only over a trusted private/encrypted network
such as a tailnet, not the public internet. Use an HTTPS endpoint when that network cannot protect the token.

## On this machine

1. Attach an entry for each client, as for a local one, with its own home and a name to tell it apart:

   ```text
   scope-recall attach --host claude-code --instance-root D:\ScopeRecall\work-claude-code --root D:\ScopeRecall\shared --entry work-claude-code --display-name "Work Claude Code" --grants-like all --capture-like desk
   ```

2. Record where it is served and the digest the client printed (step 3 below):

   ```text
   python -m scope_recall.adapters.codex.remote_server configure --home D:\ScopeRecall\work-claude-code --host claude-code --listen 100.64.0.10 --port 18765 --token-sha256 <hex>
   ```

3. Serve it from an environment that has the `codex` extra, at logon and again when it stops. `--env-file`
   names the file holding the embedding key the runtime config declares; it is read in place:

   ```text
   python -m scope_recall.adapters.codex.remote_server serve --home D:\ScopeRecall\work-claude-code --host claude-code --env-file <file>
   ```

   The server keeps one handler for its prompts' recall, with its LanceDB table open and its embedding
   worker connected, while each request's own handler stores the hook (as the entry's MCP server on this
   machine does for the hooks here: [shared-store.md](shared-store.md)).

   The server has no console. Each hook it handles (its event, the handler's reason, the capture's error code
   when it failed, how far a record was stored, the time taken with the shares of making its handler, the
   capture, attaching the handler's runtime and closing it (the rest is the recall), and for a prompt how its
   kept recall went: `warm recall answered`, `busy`, `slow`, `late`,
   `without_vectors:<gap>` or `failed:<reason>`), each request refused for want of the token and its own
   errors go to `<home>\scope-recall\remote-server.log`, kept to about 1 MB with two older copies.

   On Windows start it with a `pythonw.exe` that opens no console, such as the one in a virtual environment
   made by `python -m venv`. uv 0.12.1 writes `Scripts\pythonw.exe` as a copy of its console launcher: the
   server then runs in a console that Windows Terminal shows as a window, and closing that window stops it.

4. Let the client machine reach the port: an inbound firewall rule for that port, from the client's private
   address only.

Each entry has its own port and its own server process. A server runs the installed package, so it is
stopped with the other processes on the store for an upgrade (`package-upgrade`) and started after it.
For a WorkBuddy client, name `--host workbuddy` in `attach`, `configure` and `serve`.
For dsh, use `--host dsh` in all three; the attached home is on the server, not on the client machine.

## On the client machine

1. Install the package in a virtual environment. Claude Code must be 2.1.196 or later: a prompt from an
   earlier one carries no prompt id and is refused. Claude Code runs a hook through a shell, so its
   interpreter and `client.json` must be on paths of ASCII letters, digits and `._-/:` only; `install`
   refuses others.
2. Write `client.json` with absolute paths:

   ```json
   {"url": "http://100.64.0.10:18765", "host": "claude-code", "token_file": "C:/Users/me/.scope-recall-remote/claude-code/token", "state_dir": "C:/Users/me/.scope-recall-remote/claude-code/state"}
   ```

3. Make the token and give its digest to this machine's step 2. Only the digest is printed:

   ```text
   python -m scope_recall.adapters.codex.remote_client token --config <client.json>
   ```

4. Write the plugin:

   ```text
   python -m scope_recall.adapters.codex.remote_client install --config <client.json> --plugin-dir <dir>
   ```

   Claude Code: `<dir>` is `~/.claude/skills/scope-recall`; it loads in the next session. Codex: `<dir>` is
   `~/plugins/scope-recall-codex`, listed in the personal marketplace (`~/.agents/plugins/marketplace.json`)
   and enabled in Codex, which then asks you to approve its hooks.

   The plugin's `.mcp.json` carries the token too, as the header the host sends to `/mcp`; like the token file
   it stays in your profile.

   WorkBuddy has no plugin: `<dir>` is its own home (`%USERPROFILE%\.workbuddy`), and `install` adds the hooks for
   `UserPromptSubmit`, `Stop` and `SessionEnd` (15, 10 and 10 s) to `settings.json` there and the server
   `scope-recall`, with the token header, to `mcp.json`. Everything else in those files stays, each file it
   changes is copied to `state_dir\backups\<time>\` first, and running it again changes nothing; a new token is
   written into the same server. It refuses beside another Scope Recall hook (a local entry's or another client's),
   a `scope-recall` server that names another address, or a file that is not plain JSON. WorkBuddy runs the hooks
   through Git Bash: keep the interpreter and `client.json` on paths of printable ASCII without `"`, `$`, `` ` ``
   or `\`. Quit WorkBuddy before `install` and start it again after, then approve the MCP server `scope-recall`
   in its MCP settings, where it waits for approval.
   To take the client out, quit WorkBuddy and delete the server `scope-recall` and the three hooks that run
   `remote_client` from those files, or put back the copies from `state_dir\backups\` if nothing else changed
   there since.

### dsh on the client machine

Use the client steps above with `"host": "dsh"`. Quit every dsh profile first, then pass its existing home
(`$DSH_HOME`, normally `~/.dsh`) as `--plugin-dir`:

```text
python -m scope_recall.adapters.codex.remote_client install --config <client.json> --plugin-dir <dsh-home>
```

The installer writes `scope-recall/dsh-plugin/index.mjs` and merges two native rows into `cordis.patch.yml`:
`scope-recall` runs the Python forwarder with `remoteConfig: <absolute client.json>`; `mcp-scope-recall` uses
`@deepseek-ai/dsh-mcp-client`, `transport: streamable-http`, `<url>/mcp` and the token's Authorization header.
The plugin's `home` is the client's `state_dir` (status and pending messages only); no local store attachment,
model key or `envFile` is needed. Without `remoteConfig` the plugin still uses the same-machine hook entry.
Recall and Stop retain bounded waits (9 and 20 s by default); a failed recall does not block the turn indefinitely.

Other rows and user overrides are preserved; changed existing files are copied under `state_dir/backups/`.
Reinstall is idempotent and refreshes the MCP token after token rotation. Another entry's managed block or an
unowned Scope Recall row is refused rather than silently replaced. As with a local install, session-log upload
to dsh's model API is switched off. Keep the patch and its backups private: the MCP header contains the token.
Restart dsh and check `dsh --profile headless --dump-config` for both rows, without sharing the token-bearing dump.
To remove it, quit dsh and remove only the `SCOPE_RECALL_DSH_START`/`END` block and installed plugin file;
leave the separate privacy block in place. Do not discard pending messages unless you intend to lose them.

## When the server cannot be reached

A hook whose connection has not opened in 3 s gives up and answers with nothing; for a minute after that no
hook tries, so while the server is away a prompt is held up once a minute at most, and it has no recall.
A Claude Code session's record carries what was said to that session's next Stop that reaches the server,
and the cursor on the client moves only as far as the server stored; what a session had not sent when it
ended stays unsent, and a message the store was too busy to take when its hook came (the server log says
`not stored`) is stored from the record at the next Stop. A Codex hook is written to a spool on the client
before it is sent and removed once the server has answered for it; what stays is sent, with the moment it
happened, by a process a later hook starts once the server answers again. A hook sent twice is the same source,
not two. A hook whose message the busy store could not take stays in the spool (the server log says `not stored,
to be sent again`), and one the server refuses for good (HTTP 400 or 413: malformed, too large) is dropped
rather than kept, since it would be refused again and would stop every later flush. The spool keeps 256 hooks
and drops its oldest past that. What did not get through, what the spool sent and what it dropped is logged in
the client's `state_dir\remote-client.log`.

dsh does **not** use the Codex forwarder's spool. Its native plugin already keeps unacknowledged messages in
`state_dir/scope-recall/dsh-spool`; failed/empty responses do not advance its acknowledgement cursor. The next
Stop or the plugin's bounded background sweep can replay them. The existing retention limit still applies
(5,000 messages per session or 14 days); this is not unlimited delivery or a second remote queue. Check
`state_dir/scope-recall/dsh-plugin-status.json` and the forwarder's log when capture stays pending.

A message is dated by the client's clock, the moment its hook ran there, which is what makes a hook sent again
the same source. When a request's latest time is more than a minute ahead of this machine's, every time in it
moves back by that lead, so the latest is this machine's now and a turn keeps its order: a recall finds nothing
dated after its now, so a message dated a day ahead would have stayed hidden for a day (a hook that such a client
sends twice may then be stored twice). When it was stored, when its work falls due and a recall's now are
this machine's clock, so a client clock that runs fast or slow does not hold back the work on its messages.
That request-time adjustment covers the forwarder's hook time and file-record envelope; dsh's nested message
timestamps retain the existing dsh transcript handling. Keep the two machines' clocks synchronized.

The client connects to the server itself and never through a proxy: `HTTP_PROXY` or a system proxy on the
client machine is for the internet and cannot reach the tailnet address. Claude Code's and Codex's own MCP
connection to `/mcp` follows their proxy settings, so the server's address must be in the client machine's
`NO_PROXY` (an address, not only a range: not every client reads `100.64.0.0/10`). WorkBuddy follows these
variables and the system's proxy, and matches `NO_PROXY` by name or address only.

## A new client machine

The entry belongs to the store, not to the machine. On the new machine repeat the client steps with a new
token, and on this machine run `configure` again with its digest and restart the server: the old token stops
working and the entry's memories stay under its name. A firewall rule that names the old machine's address
needs the new one.

## Limits

Over a relayed path a request takes one to three round trips of the network: 0.4-1.2 s through a Tailscale relay
on 2026-09-27, about 50 ms direct. A prompt's hook waits at most 15 s, as a local one does, and the server's
work on it at most the entry's `hook_processing_seconds` (6 s unless set lower); both are ceilings, and the
prompt goes on as soon as recall is done. Codex allows SessionEnd and Interrupt at most 3 s, so on a slow path
those may time out; the hook is already in the spool then, and is sent from there.
