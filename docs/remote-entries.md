# A client on another machine

Claude Code or Codex on a second machine (a work computer) can be an entry of the shared store on this
one. Its memories are the store's, recalled by every entry with the owner's grants, and each recalled item
names the entry it came in through, so "工作机 Claude Code" stays apart from this machine's Claude Code.

Nothing of the store moves. The client machine runs a small forwarder and holds only the entry's token:
each hook goes to the entry's server here over HTTP, and this machine's handler records it, as it does for a
local client. A Claude Code client reads its own session record on its machine and sends what the record
shows being said, never the record itself. The entry's MCP tools are served over streamable HTTP. Tool calls
and tool output are not recorded, as for a local client.

The server listens on one private address this machine has on a network both machines are in (a tailnet),
never on every interface, and refuses any request without the entry's token. The token is made on the client
machine and stays there; this machine keeps its SHA-256.

## On this machine

1. Attach an entry for each client, as for a local one, with its own home and a name to tell it apart:

   ```text
   scope-recall attach --host claude-code --instance-root F:\ScopeRecall\workpc-claude-code --root F:\ScopeRecall\shared --entry workpc-claude-code --display-name "工作机 Claude Code" --grants-like all --capture-like tianshu
   ```

2. Record where it is served and the digest the client printed (step 3 below):

   ```text
   python -m scope_recall.adapters.codex.remote_server configure --home F:\ScopeRecall\workpc-claude-code --host claude-code --listen 100.64.0.5 --port 18765 --token-sha256 <hex>
   ```

3. Serve it from an environment that has the `codex` extra, at logon and again when it stops. `--env-file`
   names the file holding the embedding key the runtime config declares; it is read in place:

   ```text
   python -m scope_recall.adapters.codex.remote_server serve --home F:\ScopeRecall\workpc-claude-code --host claude-code --env-file <file>
   ```

4. Let the client machine reach the port: an inbound firewall rule for that port, from the client's private
   address only.

Each entry has its own port and its own server process. A server runs the installed package, so it is
stopped with the other processes on the store for an upgrade (`package-upgrade`) and started after it.

## On the client machine

1. Install the package in a virtual environment. Claude Code must be 2.1.196 or later: a prompt from an
   earlier one carries no prompt id and is refused.
2. Write `client.json` with absolute paths:

   ```json
   {"url": "http://100.64.0.5:18765", "host": "claude-code", "token_file": "C:/Users/me/.scope-recall-remote/claude-code/token", "state_dir": "C:/Users/me/.scope-recall-remote/claude-code/state"}
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

## When the server cannot be reached

A hook answers at once with nothing, so the client is never held up; it has no recall for that turn.
Claude Code loses no message: its record carries them to the next Stop that reaches the server, and the
cursor on the client moves only as far as the server stored. A Codex hook is kept in a spool on the client
and sent, with the moment it happened, by a process a later hook starts once the server answers again; a hook
sent twice is the same source, not two.

## A new client machine

The entry belongs to the store, not to the machine. On the new machine repeat the client steps with a new
token, and on this machine run `configure` again with its digest: the old token stops working at once and the
entry's memories stay under its name.

## Limits

Over a relayed path a request takes one to three round trips of the network. Codex gives each hook 2 s, so on
a slow path its prompts may come back without recall, and its messages are then sent from the spool.
