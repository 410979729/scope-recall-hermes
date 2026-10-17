# Contributors

Scope Recall thanks contributors who improve its implementation, report defects,
reproduce failures, and test fixes.

The reliability fixes following 2.0.1 credit these issue reporters as commit
co-authors for their reproduction and diagnosis work:

| Contributor | Contribution |
| --- | --- |
| [JohnYinl](https://github.com/JohnYinl) | Reproduced the hidden L4 reason-length failure and tested prompt/parser remedies in [#65](https://github.com/410979729/scope-recall-hermes/issues/65). |
| [849506054](https://github.com/849506054) | Traced orphan relation-frequency failures, retry storms, and writer fencing in [#66](https://github.com/410979729/scope-recall-hermes/issues/66). |
| [ycheng87](https://github.com/ycheng87) | Reproduced missing User-Agent failures on protected nightly endpoints and supplied a local fix in [#67](https://github.com/410979729/scope-recall-hermes/issues/67). |
| [yzy806806](https://github.com/yzy806806) | Identified the online candidate-review gap and misleading store lifecycle receipts in [#68](https://github.com/410979729/scope-recall-hermes/issues/68). |
| [gfpyc](https://github.com/gfpyc) | Supplied Windows desktop Arrow crash evidence, affected configuration, and a working SQLite-backend workaround in [#69](https://github.com/410979729/scope-recall-hermes/issues/69). |

Since 3.x, these contributors' code was merged:

| Contributor | Contribution |
| --- | --- |
| [JohnYinl](https://github.com/JohnYinl) | The metered Responses API consolidation route ([#90](https://github.com/410979729/scope-recall-hermes/pull/90)), candidate evaluation windows anchored to saved evidence ([#91](https://github.com/410979729/scope-recall-hermes/pull/91)) and calendar-month auxiliary budgets ([#239](https://github.com/410979729/scope-recall-hermes/pull/239)). |
| [panxuewen0101](https://github.com/panxuewen0101) | The plain-HTTP endpoint policy for loopback model servers, with an explicit opt-in for other hosts ([#226](https://github.com/410979729/scope-recall-hermes/issues/226), [#227](https://github.com/410979729/scope-recall-hermes/pull/227)), and the vector rehearsal that sees the import roots of a package-manager Hermes ([#251](https://github.com/410979729/scope-recall-hermes/issues/251), [#252](https://github.com/410979729/scope-recall-hermes/pull/252)). |
| [Adam13y](https://github.com/Adam13y) | Known-answer tests for three recall invariants ([#213](https://github.com/410979729/scope-recall-hermes/issues/213), [#217](https://github.com/410979729/scope-recall-hermes/pull/217)). |

And these reports, each with its reproduction, led to fixes:

| Contributor | Contribution |
| --- | --- |
| [erosaishen](https://github.com/erosaishen) | Codex desktop's request for a suggested next message stored as the owner's words, with the rows and the filters that missed it ([#256](https://github.com/410979729/scope-recall-hermes/issues/256)). |
| [phfqy](https://github.com/phfqy) | Four reports with reproductions and live measurements ([#236](https://github.com/410979729/scope-recall-hermes/issues/236)), and the limits that kept thinking models from consolidation ([#240](https://github.com/410979729/scope-recall-hermes/issues/240)). |
| [panxuewen0101](https://github.com/panxuewen0101) | The request-size cap that refused candidate evaluations before sending them ([#237](https://github.com/410979729/scope-recall-hermes/issues/237)), and no supported way to change an attached entry's grants ([#246](https://github.com/410979729/scope-recall-hermes/issues/246)). |
| [SaberAlterP](https://github.com/SaberAlterP) | The desktop app's reminders stored as the owner's words ([#248](https://github.com/410979729/scope-recall-hermes/issues/248)), and automatic recall repeating what the model still had ([#249](https://github.com/410979729/scope-recall-hermes/issues/249)). |
| [Enferlain](https://github.com/Enferlain) | A consolidation route that refused a model server on the same machine ([#244](https://github.com/410979729/scope-recall-hermes/issues/244)). |
| [bcbwdfhl](https://github.com/bcbwdfhl) | Memory tools offered to dsh that could never succeed there ([#245](https://github.com/410979729/scope-recall-hermes/issues/245)). |
| [kurodokuro](https://github.com/kurodokuro) | Failed embeddings that no command could re-open once their provider answered well again ([#255](https://github.com/410979729/scope-recall-hermes/issues/255)). |
| [tutan0558](https://github.com/tutan0558) | Console windows flashing at a worker's teardown ([#222](https://github.com/410979729/scope-recall-hermes/issues/222)) and at every scheduled wake ([#238](https://github.com/410979729/scope-recall-hermes/issues/238)). |
| [849506054](https://github.com/849506054) | Evidence from the end of a session that waited for the next session ([#214](https://github.com/410979729/scope-recall-hermes/issues/214)). |
| [momolee-deep](https://github.com/momolee-deep) | A Hermes agent on Gemini that could not use its memory tools ([#216](https://github.com/410979729/scope-recall-hermes/issues/216)). |

See the [commit history](https://github.com/410979729/scope-recall-hermes/commits/main/)
for implementation authors and additional contributions.
