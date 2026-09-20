# Autonomous memory candidate: frozen scope

Baseline: Epsilon's private 3.0.0 wheel SHA-256
`68bd9c70807b8364a2d99b755e29a83437ecf23020b82c01aaf3d42e0732c2a5`.
The baseline source and byte manifest are preserved in `<work root>/SR-AUTO-20260912`.

This iteration implements the user's September 12 request: memory must work
without a person reviewing candidates or clearing Journal queues. Sources remain
evidence; recording a source does not require an expensive model operation.

Acceptance requirements:

1. Selective admission preserves authorized sources and lexical retrieval;
   obvious noise and repeated processing do not create expensive work. Important
   corrections and meaningful unknown content remain eligible.
2. Background work has bounded resource use, finite retry/recovery behavior,
   truthful failed/pending/completed status, and durable process receipts.
3. Small current preferences/constraints and task context appear automatically,
   under the same scope, version, deletion and token-budget gates as retrieval.
   Background context alone is not evidence that the user's question is answered.
4. Explicit natural corrections/retractions become effective without a model;
   negation, questions, future/conditional changes and uncertain targets cannot
   silently replace a fact. Existing temporal/history/deletion behavior remains.
5. Bounded directed retrieval supports task restoration and evidence gaps.
   Compression/session lifecycle preserves source identity and avoids duplicates.
6. Lifecycle policies never delete live supporting evidence or infer fact truth
   from popularity. Low-value/expired work reaches a finite documented outcome.
7. A reproducible scenario suite covers useful admission, noise, correction,
   deletion, unrelated queries, task continuation, budget limits and recovery.
   Synthetic/local checks and real model/host checks are reported separately.
8. Final source, wheel, isolated installation and test receipts are hash-bound;
   version and usage documentation describe the actual delivered behavior.

Work occurs in an independent source tree and new TEST environments. Production
instance replacement, old database cutover, public release, and peer-instance
configuration are outside this iteration. No existing source or live memory is
removed. Historical acceptance reports remain historical.
