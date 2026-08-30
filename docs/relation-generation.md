# Relation policy generations

Program 2 adds a feature-gated execution path for relation-policy changes. The
packaged default is `relation_policy_generation_enabled: false`, which keeps the
Program 0 containment runner unchanged. Enabling the flag changes only the
background adapter; current relation trust still comes from the containment
state and ordinary recall keeps all other signals when relation work is blocked.

Each policy revision freezes the old and new blocked-entity snapshots and
receipt hashes, their symmetric delta, the source corpus revision, a candidate
upper bound, and a complete item-set hash. Candidate discovery uses affected
entity postings and generated neighbors. It retains at most `cap + 1`; crossing
the cap blocks the whole generation and creates no partial items. Pair identity
is the canonical tuple of scope, revision, and sorted memory ids.

Items are inserted only while their generation is building. Identity fields
and the finalized set are immutable. Claims use exact worker, token, generation,
and expiry checks. Expired work returns only to bounded retry, attempts have a
hard maximum, cursor progress is monotonic, terminal items cannot revive, and a
new corpus revision creates a new generation while explicitly superseding old
nonterminal work.

Generated edges remain in the existing `memory_relations` table. The adapter
records separate immutable provenance only for edges emitted by its exact batch;
manual or reviewed relations are not relabeled as generated. A stale, poisoned,
or cap-blocked generation keeps the current relation signal untrusted and never
falls back to all-scope pair enumeration.

Rollback is the packaged false flag. An exact Program 2 poison target may be
returned to the Program 0 atomic runner; a cap-blocked target is returned only
after its configured cap increases beyond the recorded attempt. Snapshot,
receipt, policy-version, generation-state, and current corpus checks must all
match before that handoff. A failed handoff cannot reopen on every scheduler
tick, and the immutable Program 2 history remains available for audit.
