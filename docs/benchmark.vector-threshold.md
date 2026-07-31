# Vector-only threshold calibration

Scope Recall's packaged default is calibrated for `gemini-embedding-001` at 3072 dimensions. The source fixture is `benchmarks/vector_only_threshold_calibration_v1.json`.

## Method

- 24 public synthetic queries.
- Each query has one answer, one topically similar but non-answering hard negative, and one unrelated negative.
- 72 query/document scores were generated in one batch through the packaged OpenAI-compatible Gemini embedder.
- No live memory, user data, private path, account identifier, or credential was sent to the embedding endpoint or stored in the fixture.
- False positives cost twice as much as false negatives.
- Candidate thresholds must retain at least 0.80 recall. Ties are broken by higher F1, then the higher threshold.

## Result

At the former `0.65` default:

- precision: `0.621622`
- recall: `0.958333`
- false positives: `14`
- false negatives: `1`
- weighted error: `29`

At the selected `0.70` default:

- precision: `0.769231`
- recall: `0.833333`
- false positives: `6`
- false negatives: `4`
- weighted error: `16`

The selected threshold cuts the weighted error by about 44.8% while retaining the required recall floor. The repository test recomputes these metrics from the score fixture and requires the packaged default to match the selected threshold.

## Boundary and limitations

This benchmark calibrates only the vector-only admission threshold. Hybrid retrieval also uses lexical/BM25 signals, current-state intent evidence, entity scope, freshness, and source weighting. A relevant candidate below `0.70` can still enter through a lexical signal; a high-scoring topical non-answer may still require intent or entity filtering.

The fixture is model- and dimension-specific. Changing the embedding model, dimensions, query/document prefixes, or prompt profile requires a new vector generation and a fresh calibration fixture rather than reusing these scores.
