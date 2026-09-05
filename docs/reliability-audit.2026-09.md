# September 2026 reliability audit

This audit accompanies the fixes for issues #65–#69. Issue #41 is outside this
change. The review concentrates on protocol parsing, writer ownership, relation
maintenance, candidate lifecycle authority, and Windows vector failure isolation.

## Findings fixed

| Failure boundary | Root cause | Repair |
| --- | --- | --- |
| L4 response (#65) | A hidden reason-length limit discarded otherwise valid verdicts. | Share the prompt/parser budget, sanitize and bound valid reasons, retain strict schema validation. |
| L4 response audit | JSON decoding accepted contradictory duplicate fields. | Reject duplicate keys as protocol errors, including either verdict order. |
| Relation work (#66) | A failure without corresponding change work could never be consumed and blocked scope snapshots. | Reconstruct current-truth work under a higher generation, record supersession, rebuild within bounded retries, and refresh both old/current scopes. |
| Writer ownership (#66) | A preflight veto could call resume despite no quiesce, allowing resume failure to fence a healthy writer. | Resume only after quiescing actually began; ordinary activity vetoes are debug events. |
| HTTP requests (#67) | Main LLM paths inherited urllib's default client identity. | Add a truthful Scope Recall User-Agent at the shared safe transport boundary; retain explicit provider headers and redirect policy. |
| Online review (#68) | CLI required a writer lease held by the running gateway; the gateway lacked candidate actions. | Add typed promote/archive commands to its existing memory dispatcher, default to dry-run, and reuse lifecycle/audit/outbox transactions. |
| Store receipt (#68) | Successful writes were described as promoted even after policy routed them to candidate. | Project the persisted lifecycle and admission identity into the receipt. |
| Post-commit receipt audit | A failed lifecycle lookup could turn an already committed store into a tool error and invite duplicate submissions. | Preserve the committed outcome and id, mark lifecycle unknown, and log only the error type without retrying the write. |
| Windows native runtime (#69) | Runtime native operations shared a process with model libraries; a hard Arrow crash could terminate Hermes. | Use a private persistent vector helper, lazy local-model imports, bounded framed transport and explicit recovery. |
| Helper recovery audit | A dead helper could consume repeated outbox attempts; native stdout could corrupt the protocol. | Mark it as requiring reopen before further claims, preserve retry work, and isolate protocol output from native stdout. |

## Preserved contracts

- SQLite owns truth. Physical vector writes follow durable outbox intent; native
  failure never triggers truth deletion or an active-generation backend switch.
- Online review resolves writable scope before returning snapshots, rejects
  already processed candidates and Fact-owned projections, and supports revision
  comparison. Audit failure rolls the entire mutation back.
- Recovered relation failures are rebuilt from current truth. Deleted and moved
  rows are covered, as are poisoned rows that reach a terminal retry budget.
- Core tool count and cost ceilings stay unchanged. The reviewed schema snapshot
  is 9,588 characters / 2,397 estimated tokens; see [D-013](tool-profiles.2.0.md).

## Remaining architecture debt and validation limits

- Compatibility adapters still accept structural `Any` hosts and use reflective
  lookup of legacy provider methods. Existing architecture tests contain this
  boundary. Replacing it requires a deliberate compatibility change; the review
  did not demonstrate an authority bypass through it.
- Explicit offline Doctor/migration/repair scripts can still load native
  dependencies in their own CLI processes. The new isolation covers gateway and
  desktop vector runtime paths. These standalone tools could later share one
  isolated backend interface.
- The Windows native Lance dependency can fail when generated data-file paths
  exceed the platform's long-path boundary. Both the existing in-process store
  and the helper reproduced this with 261/265-character data paths; unchanged
  migration tests passed with a shorter storage root. Use short storage paths
  on affected Windows installations.
- Worker frames are limited to 64 MiB and requests to 60 seconds. Bulk APIs such
  as `list_records` still return a complete result, so very large migrations
  need a future paginated or chunked interface rather than an unbounded frame.
- A failed helper requires explicit repair or provider restart. It stays marked
  `needs_repair` and cannot serve vector queries or consume further replay
  attempts until reopened. Lexical recall and durable SQLite state remain usable.
- Tests inject hard exits and timeouts and exercise real LanceDB I/O, including
  a physical commit followed by an exit before acknowledgement. The original
  reporter's desktop dump and exact native wheel combination were not reproduced.
- HTTP tests inspect actual requests on a local endpoint. Acceptance by a
  particular Cloudflare deployment depends on its policy and was not probed with
  the reporter's credentials.

The issue reporters' reproduction and diagnosis contributions are recorded in
[CONTRIBUTORS.md](../CONTRIBUTORS.md) and commit co-author trailers. This bounded
review is not a claim that every possible repository defect has been excluded.
