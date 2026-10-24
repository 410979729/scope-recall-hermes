# Manage your memory in a browser

Run the command on the computer that holds your memory store:

```console
scope-recall ui --root /path/to/shared-store
```

For a host installation with its own store, use the same selection as the
memory command:

```console
scope-recall ui --host hermes --instance-root /path/to/host-home
```

The command prints a link and opens it in your default browser. If the browser
does not open, copy the printed link into a browser on this computer. Keep the
terminal open while using the page. Press Ctrl+C in that terminal to stop it;
the link then stops working. Starting again creates a fresh link. Treat the
link as access to your memory and keep it private.

The interface uses Chinese when the browser's preferred language is Chinese,
and English otherwise. The language selector switches between both languages.

## What the pages do

- **Overview** shows database and vector size, source count, pending work and
  failures, using the same measurements as doctor. Refresh reads the current
  status; it does not start or retry background work.
- **Browse and search** accepts literal words and the memory command's kind,
  scope, agent, session and inclusive UTC time filters. Continue follows the
  same bounded scan, including pages with no match while more objects remain.
  Open an item to read its content, source links and version history. Continue
  under history loads older versions. Open source links to read original words.
- **Corrections and deletion** start from an item's detail view. A derived
  fact can be corrected or withdrawn; original words cannot be edited. Mute,
  unmute and delete display their affected items and sources before execution.
  To delete a session or time range, enter its scope, agent, session and time filters in Browse and search
  and choose Delete session or time range. Review the preview, then choose
  Confirm this action. Search words and kind do not narrow a session deletion.
  Cancel leaves memory unchanged. A preview expires after
  ten minutes; if the underlying selection changes, create a new preview.
  Each confirmed action displays its receipt. Deletion uses the existing
  deletion and background purge process; a receipt can still show pending work.
- **Private mode** shows the current whole-store mode and active rules. Choose
  an exact session, absolute project directory, or the whole store to inspect,
  enable or close its rules. Full mode blocks capture, recall and outbound work;
  read-only blocks capture but permits recall. A whole-store rule can have a
  duration in seconds. Closing one target leaves overlapping rules in force.
  Review and confirm each switch. See [private mode](private-mode.md) for its
  time and project boundaries.
  A whole-store full rule also refuses detail and history reads through the
  existing owner API; close the rule from this page before opening those views.
- **Export** writes visible memory to a new directory or a new path ending in
  `.zip`, with optional retained attachments. Type the destination on this
  computer, review it and confirm. An existing destination is refused. See
  [portable export](export.md) for the files included. An export is not a full
  disaster recovery backup.
- **Backup status** shows the newest verified scheduled bundle for this copy,
  deletion-mirror diagnostics, and any scheduled-backup problem. It does not
  create, rotate or restore backups. See [backup and recovery](backup-restore.md)
  for those operations. A manually named bundle is not part of scheduled bundle
  discovery.

Receipts and previews belong to the running page process and are kept for ten
minutes, up to the most recent 128 of each. Persistent audit and deletion
records remain managed by the store's existing interfaces.

## How it stays local

The server listens only on `127.0.0.1`, using a free port chosen by the operating
system. There is no option to expose it on a network address. Every page,
script, stylesheet and API request needs the random token created at startup,
and the Host header must name `127.0.0.1` or `localhost` with that exact port.
Changing memory requires POST with a token in a request header or form field;
cookies and a token in the URL alone cannot authorize a change. When the
browser supplies an Origin, it must match the local page address.

Memory is displayed as text, including any HTML or script someone wrote in a
conversation. Scripts and styles ship inside the package; there are no external
page resources, inline scripts or external services used by this page. Browser
policy prevents framing and referrer disclosure, and responses are not cached.
These protections do not protect against another program or person who already
has access to this computer and the printed link.
