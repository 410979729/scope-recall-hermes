# Deletion and suppression contract

Online event/Claim governance runs in the same SQLite transaction as its
deletion operation, dependency blocks, epoch change and late work invalidation.

`forget` requires current captured human authorization, the exact target versions,
scope/project/branch ownership and explicit references for a batch. Unknown and
unauthorized targets share the same error. Repeating the same authorized operation
returns its original receipt without a new write, including after the authorizing
source has been erased. The receipt states the requested references, actual scopes,
project/branch, affected object count, minimum content unit and each cleanup layer.

`suppress` blocks automatic use and preserves explicitly requested reads. It does
not become a permanent unsuppress when a later conversation discusses the object.
Dependent captures and proposals inherit suppression; an exact scoped fresh mention
of a suppressed Claim also stays suppressed for automatic retrieval.

`delete` immediately denies all existing event/Claim representations, source
expansion and historical reads. The minimum captured content unit is a complete
source group, including versions and missing segments that might arrive later.
Deleting a derived Claim therefore also blocks its supporting source groups and
their other dependent Claims: the application cannot retain the quoted private
source and still promise that retrieval will not repeat it. This can affect other
facts captured in the same message. The deletion receipt reports this broader
dependency count. No redacted source is fabricated as a new human occurrence.
The authorizing command is included because it can repeat the target's content.
In a shared store ([shared-store.md](shared-store.md)) there is one copy of every
memory, so a `forget` through any entry applies to every entry.
A capture still waiting in the capture inbox when a delete is recorded is cancelled
when it holds a deleted message (its text, one of its segments as stored, a later
version of the same source, or a part of it sent without its first); the scope's
other waiting captures are stored, another message waiting under the deleted
message's key included. Under a deleted
message's key, a later version, a part sent without its first and a copy of the
message are refused and leave the inbox; another message under that key is stored
under a key of its own. A copy is decided on the whole message; after the purge,
only digests of the deleted words (as stored, without whitespace, and their letters
and digits) are left to decide it by, so a copy with other words added is then stored
as another message. A delete purged before 3.4.0 kept no such digests, and
whatever comes under its key is refused. A copy of a suppressed or deleted message stored under a new key,
after its key collided with another message's, is stored suppressed, its whole
source group with it.
The dependency closure also covers episodes, artifacts and references; a
dependency of any other kind fails closed (`unresolved_dependency_kind`).

All content exits rehydrate SQLite objects. `release_objects` refuses what was read
before a deletion or suppression in the reader's scopes was recorded
(`retraction_after`), and checks the current source/Claim revision, read blocks and
automatic suppression at its final read transaction. It never returns a cached or
vector-stored body. Source search
and counts enforce scope/project/branch before ranking or limiting. Project-global
objects remain readable in their authorized scope; other projects and branches do
not. An epoch move alone refuses nothing, since every capture moves it; it is not a
count of visible items either.
Text already delivered to the host lies outside a later local transaction.

Physical SQLite cleanup runs separately after online blocking, in the worker's purge
work, under the store's writer boundary. It clears active raw content, source
keys/extras, Claim payloads, evidence quotations/locations and lexical rows: the
source's postings, the index terms no other source holds and a deleted candidate's
trigger terms. Minimal opaque dependency IDs and governance records remain, with
digests of the deleted words (as stored, without whitespace, and their letters and
digits) by which a later copy under the message's key is refused. A cleanup failure
rolls back physical changes while the earlier read block remains effective.

Every writable connection to the store overwrites with zeros what it deletes or
replaces (SQLite's `secure_delete`), so the free space of a page keeps no copy:
neither what the cleanup replaced nor the inbox row a stored capture leaves. Once the
cleanup has committed, the purge copies the write-ahead log into the store and
truncates it (`wal_checkpoint(TRUNCATE)`): no frame from before the cleanup is left
in `memory.sqlite3-wal`, the store's pages hold what the cleanup wrote, and the
receipt's `sqlite_history` layer reads `removed`. The log grows again with the
commits that follow, the purge's own receipt among them, and none of them holds the
deleted words; `memory.sqlite3-shm` holds no page. A reader still on an older
snapshot holds the truncation back: the purge waits for it no longer than the
store's timeout, leaves `sqlite_history` at `pending` and stays queued for a later
worker pass, and is done once every layer it removes is removed. A vector companion
holds vectors and opaque identities and refuses a row that carries text. Vector
active/history files, attachments, backup inventory and host-owned sources each
retain their actual pending, unknown or external state.

A purge does not remove what lies outside the store's files: backups and their
copies, text already delivered to a host, and what a storage device keeps of blocks
that were overwritten or released; no SSD erasure is claimed. Nor does it reach what
an earlier release's writes left in free space without overwriting it, such as the
inbox row of a capture that release stored.

Restore admission is an explicit installation maintenance capability, separate
from an ordinary `TrustedContext`, even when that context contains all scopes.
`InstallationMaintenance` is runtime-only; model DTOs and normal project adapters
must never create it. The maintenance caller obtains a fresh content-free ledger
and its hash from the latest authorized store independently of the old snapshot.
`begin_restore` durably writes a binding/checkpoint marker before the snapshot copy.
Every normal read, write and initialize refuses while that marker exists.

Replaying a verified ledger reinstates deletion/suppression, missing-group fences,
withdrawals and terminal intention states before reopening. Objects missing from
the old snapshot receive absence blocks. Restored governance state does not invent
a missing human source or ordinary correction history. Physical cleanup is repeated
on the restored file; status from the latest file is not treated as evidence about
the old copy. The resulting epoch exceeds both snapshot and latest-checkpoint
epochs, preventing cache ABA. Failed replay or commit leaves admission closed and
permits retry.

The operator's backup and restore are the `backup` and `restore` commands
([install.md](install.md), section 9). `restore` exports the ledger from the store
it replaces and backs that store up. `begin_restore` then writes the ledger beside
the store (`restore-ledger.json`) and the marker after it, each through a file of
its own that is synced and moved into place, so a crash leaves either whole or not
there. The snapshot, held open in one read transaction from its checks on, is then
copied over the store through SQLite's backup API, and the replay truncates the
write-ahead log, which held the snapshot's pages, before admission reopens: a
reader still on an older snapshot holds that back (`wal_busy`), and admission stays
closed. A run cut short after admission closed is finished by running `restore`
again: the kept ledger, or the store itself while nothing has replaced it, is
checked against the marker before anything else is written, and a marker left
partial is written again while nothing has replaced the store. A snapshot copied
back by hand, without its ledger, brings back what was forgotten after it was
taken. The ledger's format (`scope-recall-deletion-ledger/2`) names what it holds
whatever schema the exporting release writes, so the release that reads a
snapshot's schema replays the ledger a newer release exported; a ledger of another
format, or of this one with other fields, is refused (`RESTORE_UNVERIFIED`,
`ledger_format` or a `*_shape` field) and admission stays closed.

The complete suppression checkpoint includes inherited source and derived-object flags. Replay replaces reversible
suppression with this latest state, while deletion blocks remain monotone. The original ledger format is still read
and is prepared when rolling back to a pre-management snapshot; it can add blocks but cannot release an older mute.
