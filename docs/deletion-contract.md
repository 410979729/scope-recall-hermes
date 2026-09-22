# Deletion and suppression contract

The P06 implementation owns online event/Claim governance in the same SQLite
transaction as its deletion operation, dependency blocks, epoch change and late
work invalidation. It does not claim final P15 maintenance or P18 host acceptance.

`forget` requires current captured human authorization, the exact target versions,
scope/project/branch ownership and explicit references for a batch. Unknown and
unauthorized targets share the same error. Repeating the same authorized operation
returns its original receipt without a new write, including after the authorizing
source has been erased. The receipt states the requested references, actual scopes,
project/branch, affected object count, minimum content unit and each cleanup layer.

`suppress` blocks automatic use and preserves explicitly requested reads. It does
not become a permanent unsuppress when a later conversation discusses the object.
Dependent captures and proposals inherit suppression; an exact scoped fresh mention
of a suppressed Claim also stays suppressed for automatic retrieval. Semantic
paraphrase handling belongs to the subsequent consolidation/recall implementation.

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
Future Episode, reference and artifact repositories must extend the dependency
closure before accepting their object types; currently unknown types fail closed.

All content exits rehydrate SQLite objects. `release_objects` checks the epoch,
current source/Claim revision, read blocks and automatic suppression at its final
read transaction. It never returns a cached or vector-stored body. Source search
and counts enforce scope/project/branch before ranking or limiting. Project-global
objects remain readable in their authorized scope; other projects and branches do
not. Global epoch changes are an invalidation signal, not a count of visible items.
Text already delivered to the host lies outside a later local transaction.

Physical SQLite cleanup runs separately after online blocking, under the existing
writer boundary. It clears active raw content, source keys/extras, Claim payloads,
evidence quotations/locations and lexical rows. Minimal opaque dependency IDs and
governance records remain. A cleanup failure rolls back physical changes while the
earlier read block remains effective. SQLite history, vector active/history files,
attachments, backup inventory and host-owned sources each retain their actual
pending, unknown or external state. No SSD erasure or complete physical removal is
claimed; P15 performs explicit checkpoint/VACUUM and file inventory work.

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
permits retry. P15 still owns the complete backup identity/manifest, snapshot copy,
ordinary data completeness and operator-facing restore workflow.

The isolated tests retain actual failures and reruns. They cover online C08/C09/
C10/C11/C20/C30 paths, missing late segments, terminal intentions, withdrawal
restoration, project/branch isolation, cleanup/commit failure and retry. They use
real SQLite backup/transactions and controlled raw sources; no model or host
semantic acceptance is inferred from these tests.
