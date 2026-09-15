"""Pure helpers for reproducible LoCoMo benchmark runs.

Network clients and Scope Recall lifecycle wiring stay in the thin CLI runner;
this module owns deterministic dataset, prompt, checkpoint, and metric logic.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def parse_judgment(raw: str) -> bool | None:
    """Return a binary label, or ``None`` when the judge did not produce one.

    Infrastructure failures and malformed responses must never be silently
    converted into wrong answers because that corrupts the benchmark score.
    """

    text = str(raw or "").strip()
    if not text:
        return None

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(pairs) != len({key for key, _value in pairs}):
            raise ValueError("duplicate JSON object key")
        return dict(pairs)

    try:
        payload = json.loads(text, object_pairs_hook=strict_object)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        if set(payload) != {"label"}:
            return None
        label = payload.get("label")
        if label == "CORRECT":
            return True
        if label == "WRONG":
            return False
        return None
    if text == "CORRECT":
        return True
    if text == "WRONG":
        return False
    return None


def extract_answer(raw: str) -> str | None:
    """Extract the required explicit answer line, otherwise mark it invalid."""

    text = str(raw or "").strip()
    matches = list(re.finditer(r"(?im)^\s*ANSWER\s*:\s*(.*)$", text))
    if not matches:
        return None
    answer = matches[-1].group(1).strip().strip('"')
    return answer or None


def conversation_records(sample: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize one LoCoMo conversation without dropping multimodal evidence."""

    conversation = sample.get("conversation")
    if not isinstance(conversation, dict):
        return []
    session_keys = sorted(
        (
            key
            for key in conversation
            if re.fullmatch(r"session_\d+", str(key))
        ),
        key=lambda value: int(str(value).rsplit("_", 1)[1]),
    )
    records: list[dict[str, str]] = []
    for session_key in session_keys:
        event_time = str(
            conversation.get(f"{session_key}_date_time") or ""
        ).strip()
        turns = conversation.get(session_key)
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            text = str(turn.get("text") or "").strip()
            visual_context = str(turn.get("blip_caption") or "").strip()
            visual_query = str(turn.get("query") or "").strip()
            if not any((text, visual_context, visual_query)):
                continue
            records.append(
                {
                    "dia_id": str(turn.get("dia_id") or "").strip(),
                    "session": str(session_key),
                    "event_time": event_time,
                    "speaker": str(turn.get("speaker") or "User").strip(),
                    "text": text,
                    "visual_context": visual_context,
                    "visual_query": visual_query,
                }
            )
    return records


QUESTION_IDENTITY_FIELDS = (
    "sample_id",
    "question_index",
    "question",
    "gold_answer",
    "gold_evidence_ids",
    "category_id",
    "category",
    "current_date",
)


def _json_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def _canonical_questions(
    questions: list[dict[str, Any]],
    *,
    artifact: str,
) -> dict[str, dict[str, Any]]:
    canonical: dict[str, dict[str, Any]] = {}
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError(f"{artifact} canonical question is not an object")
        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not question_id or question_id in canonical:
            raise ValueError(f"{artifact} canonical question_id is blank or duplicate")
        canonical[question_id] = question
    return canonical


def _validate_artifact_identity(
    *,
    artifact: str,
    row: Any,
    canonical: dict[str, dict[str, Any]],
    identity_fields: tuple[str, ...] = QUESTION_IDENTITY_FIELDS,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(row, dict):
        raise ValueError(f"{artifact} artifact row is not an object")
    question_id = row.get("question_id")
    if not isinstance(question_id, str) or not question_id:
        raise ValueError(f"{artifact} artifact question_id is blank or not a string")
    expected = canonical.get(question_id)
    if expected is None:
        raise ValueError(f"{artifact} artifact has unexpected question_id {question_id}")
    drifted = [
        field
        for field in identity_fields
        if field not in row or row[field] != expected.get(field)
    ]
    if drifted:
        raise ValueError(
            f"{artifact} artifact identity drift for {question_id}: {','.join(drifted)}"
        )
    return question_id, row


def validate_retrieval_artifacts(
    questions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    require_complete: bool,
) -> dict[str, dict[str, Any]]:
    """Validate retrieval checkpoints against canonical identity and row schema."""

    canonical = _canonical_questions(questions, artifact="retrieval")
    observed: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        question_id, row = _validate_artifact_identity(
            artifact="retrieval",
            row=raw_row,
            canonical=canonical,
        )
        if question_id in observed:
            raise ValueError(f"retrieval artifact duplicate question_id {question_id}")
        if not isinstance(row.get("query_variants"), list) or not all(
            isinstance(value, str) and value for value in row["query_variants"]
        ):
            raise ValueError(f"retrieval artifact invalid query_variants for {question_id}")
        if not _json_number(row.get("retrieval_latency_seconds")):
            raise ValueError(f"retrieval artifact invalid latency for {question_id}")
        if not isinstance(row.get("results"), list) or not all(
            isinstance(value, dict) for value in row["results"]
        ):
            raise ValueError(f"retrieval artifact invalid results for {question_id}")
        for field in ("retrieval_metrics", "funnel_trace", "evidence_set_trace"):
            if not isinstance(row.get(field), dict):
                raise ValueError(f"retrieval artifact invalid {field} for {question_id}")
        observed[question_id] = row
    if require_complete:
        missing = sorted(set(canonical) - set(observed))
        if missing:
            raise ValueError(
                f"retrieval artifact incomplete: {len(missing)} missing questions"
            )
    return observed


def validate_query_plan_artifacts(
    questions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    planner_model: str,
    planner_categories: set[int],
    require_complete: bool,
) -> dict[str, dict[str, Any]]:
    """Validate planner checkpoints before they can alter retrieval queries."""

    all_questions = _canonical_questions(questions, artifact="query-plan")
    canonical = {
        question_id: row
        for question_id, row in all_questions.items()
        if int(row.get("category_id") or 0) in planner_categories
    }
    observed: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        question_id, row = _validate_artifact_identity(
            artifact="query-plan",
            row=raw_row,
            canonical=canonical,
            identity_fields=("sample_id", "category_id"),
        )
        if question_id in observed:
            raise ValueError(f"query-plan artifact duplicate question_id {question_id}")
        if row.get("model") != planner_model:
            raise ValueError(f"query-plan artifact model drift for {question_id}")
        if type(row.get("model_valid")) is not bool or type(
            row.get("fallback_used")
        ) is not bool:
            raise ValueError(f"query-plan artifact invalid flags for {question_id}")
        if row["fallback_used"] is row["model_valid"]:
            raise ValueError(f"query-plan artifact conflicting flags for {question_id}")
        variants = row.get("variants")
        if not isinstance(variants, list) or not variants or not all(
            isinstance(value, str) and bool(value.strip()) for value in variants
        ):
            raise ValueError(f"query-plan artifact invalid variants for {question_id}")
        if not isinstance(row.get("error"), str):
            raise ValueError(f"query-plan artifact invalid error for {question_id}")
        if not _json_number(row.get("latency_seconds")):
            raise ValueError(f"query-plan artifact invalid latency for {question_id}")
        if not isinstance(row.get("completed_at"), str) or not row["completed_at"]:
            raise ValueError(f"query-plan artifact invalid completed_at for {question_id}")
        observed[question_id] = row
    if require_complete:
        missing = sorted(set(canonical) - set(observed))
        if missing:
            raise ValueError(
                f"query-plan artifact incomplete: {len(missing)} missing questions"
            )
    return observed


def validate_result_artifacts(
    questions: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    answer_model: str,
    judge_model: str,
    evidence_mode: str,
    require_complete: bool = False,
) -> list[dict[str, Any]]:
    """Validate every persisted model attempt before resume or reporting."""

    canonical = _canonical_questions(questions, artifact="result")
    scored_counts: dict[str, int] = defaultdict(int)
    validated: list[dict[str, Any]] = []
    statuses = {"scored", "invalid_answerer", "invalid_judge", "invalid_transport"}
    for raw_row in rows:
        question_id, row = _validate_artifact_identity(
            artifact="result",
            row=raw_row,
            canonical=canonical,
        )
        if row.get("answer_model") != answer_model or row.get("judge_model") != judge_model:
            raise ValueError(f"result artifact model drift for {question_id}")
        if row.get("evidence_mode") != evidence_mode:
            raise ValueError(f"result artifact evidence mode drift for {question_id}")
        if type(row.get("attempt_round")) is not int or row["attempt_round"] < 1:
            raise ValueError(f"result artifact invalid attempt_round for {question_id}")
        if not isinstance(row.get("retrieval_metrics"), dict) or not isinstance(
            row.get("query_variants"), list
        ) or not all(isinstance(value, str) for value in row["query_variants"]):
            raise ValueError(f"result artifact invalid retrieval context for {question_id}")
        if not isinstance(row.get("started_at"), str) or not row["started_at"]:
            raise ValueError(f"result artifact invalid started_at for {question_id}")
        status = row.get("status")
        if status not in statuses:
            raise ValueError(f"result artifact invalid status for {question_id}")
        if status == "scored":
            scored_counts[question_id] += 1
            if scored_counts[question_id] > 1:
                raise ValueError(f"result artifact duplicate scored row for {question_id}")
            correct = row.get("correct")
            if type(correct) is not bool:
                raise ValueError(f"result artifact invalid correct type for {question_id}")
            if not isinstance(row.get("predicted_answer"), str) or not row[
                "predicted_answer"
            ]:
                raise ValueError(f"result artifact invalid prediction for {question_id}")
            expected_label = "CORRECT" if correct else "WRONG"
            if row.get("judge_label") != expected_label:
                raise ValueError(f"result artifact judge label drift for {question_id}")
            for field in ("answer_latency_seconds", "judge_latency_seconds"):
                if not _json_number(row.get(field)):
                    raise ValueError(f"result artifact invalid {field} for {question_id}")
            if not isinstance(row.get("completed_at"), str) or not row["completed_at"]:
                raise ValueError(f"result artifact invalid completed_at for {question_id}")
        elif status == "invalid_answerer":
            if not isinstance(row.get("error"), str) or not _json_number(
                row.get("answer_latency_seconds")
            ):
                raise ValueError(f"result artifact invalid answerer row for {question_id}")
        elif status == "invalid_judge":
            if (
                not isinstance(row.get("predicted_answer"), str)
                or not row["predicted_answer"]
                or not isinstance(row.get("raw_judge"), str)
                or not _json_number(row.get("answer_latency_seconds"))
                or not _json_number(row.get("judge_latency_seconds"))
            ):
                raise ValueError(f"result artifact invalid judge row for {question_id}")
        elif (
            not isinstance(row.get("error"), str)
            or not isinstance(row.get("completed_at"), str)
            or not row["completed_at"]
        ):
            raise ValueError(f"result artifact invalid transport row for {question_id}")
        validated.append(row)
    if require_complete:
        missing = sorted(set(canonical) - set(scored_counts))
        if missing:
            raise ValueError(f"result artifact incomplete: {len(missing)} missing questions")
    return validated


def completed_question_ids(rows: list[dict[str, Any]]) -> set[str]:
    """Return only questions with a valid persisted judgment."""

    return {
        str(row.get("question_id") or "").strip()
        for row in rows
        if str(row.get("status") or "").strip() == "scored"
        and str(row.get("question_id") or "").strip()
    }


def score_results(
    rows: list[dict[str, Any]],
    *,
    expected_question_ids: set[str],
    artifacts_validated: bool = False,
) -> dict[str, Any]:
    """Compute accuracy; unvalidated checkpoint rows can never be complete."""

    expected = {str(value) for value in expected_question_ids if str(value)}
    by_question: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        question_id = str(row.get("question_id") or "").strip()
        if question_id:
            by_question.setdefault(question_id, []).append(row)

    chosen: dict[str, dict[str, Any]] = {}
    for question_id, attempts in by_question.items():
        scored = [
            attempt
            for attempt in attempts
            if str(attempt.get("status") or "").strip() == "scored"
        ]
        if len(scored) > 1:
            raise ValueError(f"duplicate scored result for {question_id}")
        chosen[question_id] = scored[0] if scored else attempts[-1]

    scored_rows = [
        chosen[question_id]
        for question_id in sorted(expected & chosen.keys())
        if str(chosen[question_id].get("status") or "").strip() == "scored"
    ]
    invalid_rows = [
        chosen[question_id]
        for question_id in sorted(expected & chosen.keys())
        if str(chosen[question_id].get("status") or "").strip().startswith("invalid_")
    ]
    missing = sorted(expected - chosen.keys())
    unexpected = sorted(chosen.keys() - expected)
    correct = sum(1 for row in scored_rows if row.get("correct") is True)
    scored_count = len(scored_rows)
    expected_count = len(expected)

    category_names = sorted(
        {
            str(row.get("category") or "unknown")
            for row in chosen.values()
        }
    )
    categories: dict[str, dict[str, Any]] = {}
    for category in category_names:
        category_expected_rows = [
            row
            for question_id, row in chosen.items()
            if question_id in expected
            and str(row.get("category") or "unknown") == category
        ]
        category_scored = [
            row
            for row in category_expected_rows
            if str(row.get("status") or "").strip() == "scored"
        ]
        category_correct = sum(
            1 for row in category_scored if row.get("correct") is True
        )
        categories[category] = {
            "observed": len(category_expected_rows),
            "scored": len(category_scored),
            "correct": category_correct,
            "accuracy": (
                category_correct / len(category_scored)
                if category_scored
                else None
            ),
        }

    return {
        "expected_questions": expected_count,
        "scored_questions": scored_count,
        "correct_questions": correct,
        "invalid_questions": len(invalid_rows),
        "missing_questions": len(missing),
        "unexpected_questions": len(unexpected),
        "artifact_rows_validated": bool(artifacts_validated),
        "coverage": scored_count / expected_count if expected_count else 1.0,
        "accuracy": correct / scored_count if scored_count else None,
        "complete": (
            bool(artifacts_validated)
            and
            scored_count == expected_count
            and not invalid_rows
            and not missing
            and not unexpected
        ),
        "missing_ids": missing,
        "unexpected_ids": unexpected,
        "categories": categories,
    }


def summarize_provider_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Return a path-free allowlist of benchmark-relevant provider health."""

    raw_background = stats.get("background_writer")
    background: dict[str, Any] = (
        raw_background if isinstance(raw_background, dict) else {}
    )
    raw_vector = stats.get("vector")
    vector: dict[str, Any] = raw_vector if isinstance(raw_vector, dict) else {}
    raw_embedder = vector.get("embedder")
    embedder: dict[str, Any] = (
        raw_embedder if isinstance(raw_embedder, dict) else {}
    )

    def count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "provider": str(stats.get("provider") or "")[:64],
        "total_memories": count(stats.get("total_memories")),
        "shared_scope_memories": count(stats.get("shared_scope_memories")),
        "background_writer": {
            "thread_alive": bool(background.get("thread_alive")),
            "failed_writes": count(background.get("failed_writes")),
            "unreported_failures": count(background.get("unreported_failures")),
            "last_error_type": str(background.get("last_error_type") or "")[:128],
        },
        "vector": {
            "enabled": bool(vector.get("enabled")),
            "ready": bool(vector.get("ready")),
            "status": str(vector.get("status") or "")[:64],
            "backend": str(vector.get("backend") or "")[:64],
            "row_count": count(vector.get("row_count")),
            "unique_id_count": count(vector.get("unique_id_count")),
            "duplicate_row_count": count(vector.get("duplicate_row_count")),
            "embedder": {
                "provider": str(embedder.get("provider") or "")[:64],
                "model": str(embedder.get("model") or "")[:256],
                "dimensions": count(embedder.get("dimensions")),
            },
        },
    }


def validate_ingestion_receipt(
    receipt: dict[str, Any],
    current_stats: dict[str, Any],
) -> None:
    """Fail closed when a resumable benchmark home drifted from its receipt."""

    raw_receipt_stats = receipt.get("stats")
    receipt_stats: dict[str, Any] = (
        raw_receipt_stats if isinstance(raw_receipt_stats, dict) else {}
    )
    expected_stats = summarize_provider_stats(receipt_stats)
    current = summarize_provider_stats(current_stats)
    try:
        expected_memories = max(0, int(receipt.get("stored_memories") or 0))
    except (TypeError, ValueError):
        expected_memories = 0
    if current["total_memories"] != expected_memories:
        raise RuntimeError(
            "benchmark ingestion home memory row count does not match its receipt"
        )

    expected_vector = expected_stats["vector"]
    current_vector = current["vector"]
    if not expected_vector["enabled"]:
        return
    if not current_vector["enabled"] or not current_vector["ready"]:
        raise RuntimeError(
            "benchmark ingestion home vector companion is not ready as recorded"
        )
    if current_vector["backend"] != expected_vector["backend"]:
        raise RuntimeError(
            "benchmark ingestion home vector backend does not match its receipt"
        )
    if current_vector["row_count"] != expected_vector["row_count"]:
        raise RuntimeError(
            "benchmark ingestion home vector row count does not match its receipt"
        )
    if current_vector["unique_id_count"] != expected_vector["unique_id_count"]:
        raise RuntimeError(
            "benchmark ingestion home vector identity count does not match its receipt"
        )
    if current_vector["embedder"] != expected_vector["embedder"]:
        raise RuntimeError(
            "benchmark ingestion home embedder identity does not match its receipt"
        )


@contextmanager
def managed_provider(provider: Any, **initialize_kwargs: Any) -> Iterator[Any]:
    """Initialize one benchmark provider and always quiesce its workers."""

    provider.initialize(**initialize_kwargs)
    try:
        yield provider
    finally:
        provider.shutdown(timeout=10.0)


def render_record(record: dict[str, str]) -> str:
    """Render an atomic dialogue turn with explicit temporal/media provenance."""

    header = (
        "[LoCoMo evidence"
        f" | id={record.get('dia_id', '')}"
        f" | session={record.get('session', '')}"
        f" | event_time={record.get('event_time', '')}]"
    )
    lines = [
        header,
        f"{record.get('speaker', 'User')}: {record.get('text', '')}".rstrip(),
    ]
    if record.get("visual_context"):
        lines.append(f"Visual context: {record['visual_context']}")
    if record.get("visual_query"):
        lines.append(f"Visual question: {record['visual_query']}")
    return "\n".join(lines)


def store_record(provider: Any, record: dict[str, str]) -> str:
    """Store one benchmark record and require an auditable memory-id receipt."""

    dia_id = str(record.get("dia_id") or "unknown")
    raw = provider.handle_tool_call(
        "scope_recall_store",
        {
            "content": render_record(record),
            "target": "memory",
            "memory_type": "episodic",
            "importance": 0.8,
            "semantic_merge": False,
            "entities": [record.get("speaker", "User")],
            "tags": [
                "locomo-benchmark",
                "locomo-atomic-turn",
                f"dia:{dia_id}",
                f"session:{record.get('session', '')}",
            ],
        },
    )
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"failed to store LoCoMo evidence {dia_id}: invalid receipt"
        ) from exc
    memory_id = str(payload.get("id") or "").strip()
    accepted = any(
        payload.get(key) is True for key in ("stored", "duplicate", "merged")
    )
    if not memory_id or not accepted or payload.get("error"):
        raise RuntimeError(
            f"failed to store LoCoMo evidence {dia_id}: {payload}"
        )
    return memory_id


def session_chunks(
    records: list[dict[str, str]],
    *,
    chunk_size: int = 4,
    overlap: int = 1,
) -> list[dict[str, Any]]:
    """Build overlapping raw-turn chunks without crossing session boundaries."""

    bounded_size = max(1, int(chunk_size or 1))
    bounded_overlap = max(0, min(int(overlap or 0), bounded_size - 1))
    step = bounded_size - bounded_overlap
    sessions: list[tuple[str, list[tuple[int, dict[str, str]]]]] = []
    by_session: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for event_order, record in enumerate(records, 1):
        session = str(record.get("session") or "unknown")
        if session not in by_session:
            by_session[session] = []
            sessions.append((session, by_session[session]))
        by_session[session].append((event_order, record))

    chunks: list[dict[str, Any]] = []
    for session, session_records in sessions:
        chunk_index = 0
        start = 0
        while start < len(session_records):
            selected = session_records[start : start + bounded_size]
            if not selected:
                break
            first_order = selected[0][0]
            selected_records = [record for _order, record in selected]
            chunk_id = f"{session}:chunk:{chunk_index:04d}"
            dia_ids = [
                str(record.get("dia_id") or "")
                for record in selected_records
                if str(record.get("dia_id") or "")
            ]
            body = "\n\n".join(render_record(record) for record in selected_records)
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "session": session,
                    "event_time": str(selected_records[0].get("event_time") or ""),
                    "event_order": first_order,
                    "dia_ids": dia_ids,
                    "entities": list(
                        dict.fromkeys(
                            str(record.get("speaker") or "User")
                            for record in selected_records
                        )
                    ),
                    "content": (
                        f"[LoCoMo session chunk | id={chunk_id} | "
                        f"event_time={selected_records[0].get('event_time', '')} | "
                        f"evidence_ids={','.join(dia_ids)}]\n{body}"
                    ),
                }
            )
            if start + bounded_size >= len(session_records):
                break
            start += step
            chunk_index += 1
    return chunks


def store_chunk(provider: Any, chunk: dict[str, Any]) -> str:
    """Store one multi-resolution raw-turn chunk and validate its receipt."""

    chunk_id = str(chunk.get("chunk_id") or "unknown")
    raw = provider.handle_tool_call(
        "scope_recall_store",
        {
            "content": str(chunk.get("content") or ""),
            "target": "memory",
            "memory_type": "episodic",
            "importance": 0.82,
            "semantic_merge": False,
            "entities": list(chunk.get("entities") or []),
            "tags": [
                "locomo-benchmark",
                "locomo-session-chunk",
                f"chunk:{chunk_id}",
                f"session:{chunk.get('session', '')}",
            ],
        },
    )
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"failed to store LoCoMo chunk {chunk_id}: invalid receipt"
        ) from exc
    memory_id = str(payload.get("id") or "").strip()
    accepted = any(
        payload.get(key) is True for key in ("stored", "duplicate", "merged")
    )
    if not memory_id or not accepted or payload.get("error"):
        raise RuntimeError(f"failed to store LoCoMo chunk {chunk_id}: {payload}")
    return memory_id


CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}


def last_session_date(sample: dict[str, Any]) -> str:
    """Return the last session timestamp using numeric session ordering."""

    conversation = sample.get("conversation")
    if not isinstance(conversation, dict):
        return ""
    keys = sorted(
        (
            key
            for key in conversation
            if re.fullmatch(r"session_\d+_date_time", str(key))
        ),
        key=lambda value: int(str(value).split("_")[1]),
    )
    return str(conversation.get(keys[-1]) or "").strip() if keys else ""


def question_rows(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize LoCoMo questions into stable, resumable benchmark identities."""

    rows: list[dict[str, Any]] = []
    for sample_index, sample in enumerate(dataset):
        sample_id = str(sample.get("sample_id") or f"sample-{sample_index}")
        current_date = last_session_date(sample)
        questions = sample.get("qa")
        if not isinstance(questions, list):
            continue
        for question_index, question in enumerate(questions):
            if not isinstance(question, dict):
                continue
            category_id = int(question.get("category") or 0)
            rows.append(
                {
                    "question_id": f"{sample_id}:q{question_index:04d}",
                    "sample_id": sample_id,
                    "question_index": question_index,
                    "question": str(question.get("question") or "").strip(),
                    "gold_answer": str(question.get("answer") or "").strip(),
                    "gold_evidence_ids": [
                        str(value)
                        for value in (question.get("evidence") or [])
                        if str(value)
                    ],
                    "category_id": category_id,
                    "category": CATEGORY_NAMES.get(category_id, "unknown"),
                    "current_date": current_date,
                }
            )
    return rows


def stratified_question_sample(
    rows: list[dict[str, Any]],
    *,
    per_category: int,
) -> list[dict[str, Any]]:
    """Select each category round-robin across conversations."""

    limit = max(0, int(per_category or 0))
    if limit <= 0:
        return list(rows)
    category_order: list[int] = []
    sample_order: dict[int, list[str]] = defaultdict(list)
    grouped: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        category = int(row.get("category_id") or 0)
        sample_id = str(row.get("sample_id") or "")
        if category not in category_order:
            category_order.append(category)
        if sample_id not in sample_order[category]:
            sample_order[category].append(sample_id)
        grouped[category][sample_id].append(row)

    selected: list[dict[str, Any]] = []
    for category in category_order:
        offsets = {sample_id: 0 for sample_id in sample_order[category]}
        while sum(offsets.values()) < limit:
            made_progress = False
            for sample_id in sample_order[category]:
                offset = offsets[sample_id]
                candidates = grouped[category][sample_id]
                if offset >= len(candidates):
                    continue
                selected.append(candidates[offset])
                offsets[sample_id] = offset + 1
                made_progress = True
                if sum(offsets.values()) >= limit:
                    break
            if not made_progress:
                break
    return selected


def build_answer_prompt(
    *,
    question: str,
    category: str,
    evidence: str,
    current_date: str,
) -> str:
    """Build the category-aware LoCoMo answer prompt used for every run."""

    category_rule = {
        "multi-hop": (
            "Combine multiple evidence memories when the question requires a bridge, "
            "comparison, list, or total. For counts, enumerate the distinct supporting "
            "items first, then count them; do not guess from a partial list. For shared "
            "or common interests, build one set per subject and return their intersection."
        ),
        "temporal": (
            "The evidence event_time says when the message was sent, not necessarily "
            "when the referenced event happened. Resolve explicit and relative dates in "
            "the message body against event_time; then answer the referenced event date. "
            "Reason through before/after and elapsed time exactly, keeping the most "
            "precise date supported by the evidence."
        ),
        "open-domain": (
            "Use the memories as grounding and general world knowledge only for the "
            "open-domain inference requested by the question. Answer with hedged "
            "language ('likely', 'probably') when the evidence supports a direction "
            "but is not conclusive; do not answer unknown when the evidence supports "
            "a direction. For hypothetical activities or recommendations, identify "
            "every constraint and give one concrete option that satisfies their "
            "intersection; a partial match is not enough. Do not invent personal facts."
        ),
        "single-hop": (
            "Locate the one evidence memory that directly answers the question and copy "
            "the required fact precisely without unrelated detail."
        ),
        "adversarial": (
            "If the requested fact is not supported by the evidence, answer unknown rather "
            "than accepting a misleading premise."
        ),
    }.get(category, "Use the evidence carefully and do not invent missing facts.")
    return f"""You answer questions about a long-running conversation.

Rules:
1. Verify that names, entities, activities, and dates in the evidence refer to the subject asked about; do not conflate similar events.
2. Synthesize all relevant memories, but ignore distractors.
3. {category_rule}
4. If the evidence is insufficient, answer `unknown`.
5. Answer only the facts requested. Exclude adjacent plans, goals, or examples that do not directly satisfy the question.
6. Return one concise final line in the form `ANSWER: <answer>`. Do not expose chain-of-thought.

Question category: {category}
Current conversation date: {current_date or "unknown"}
Question: {question}

Evidence memories:
{evidence}

ANSWER:"""


def build_judge_prompt(
    *,
    question: str,
    gold_answer: str,
    predicted_answer: str,
) -> str:
    """Build a strict semantic-equivalence judge prompt with JSON-only output."""

    return f"""Judge semantic equivalence: decide whether the predicted answer means the same as the reference answer for the question.

Rules:
- Mark CORRECT when the prediction contains every required fact from the reference, allowing paraphrases, synonyms, harmless extra precision, and equivalent date formats.
- Extra detail is allowed only when it does not contradict the reference.
- For lists and counts, require all distinct required items and the correct total.
- Mark WRONG for a different entity, event, date, number, negation, or an incomplete answer that omits a required fact.
- `unknown`, `not provided`, or similar is CORRECT only when the reference itself says the information is not provided.
- Ignore style and grammar. Judge meaning, not exact string overlap.

Question: {question}
Reference answer: {gold_answer}
Predicted answer: {predicted_answer}

Return exactly one JSON object and no prose:
{{"label":"CORRECT"}}
or
{{"label":"WRONG"}}"""


_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "did",
    "do",
    "does",
    "done",
    "for",
    "from",
    "had",
    "has",
    "have",
    "her",
    "his",
    "how",
    "in",
    "is",
    "kind",
    "likely",
    "make",
    "many",
    "might",
    "of",
    "on",
    "or",
    "regards",
    "the",
    "their",
    "that",
    "this",
    "they",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "while",
    "which",
    "who",
    "why",
    "with",
    "would",
}


def build_query_planner_prompt(*, question: str, category: str) -> str:
    """Ask a fast model for search-only rewrites, never an answer."""

    return f"""Generate memory-search query variants for a long conversation.

Rules:
- Do not answer the question and do not assert that any guessed fact is true.
- Preserve exact person names, dates, places, and relationship direction.
- For multi-hop or comparison questions, create one query per subject or evidence aspect.
- Expand abstract wording with concrete evidence terms and ordinary synonyms that might appear in dialogue.
- For inferential/open-domain questions, hypothesis queries are allowed: search for concrete indicators or activities that could jointly satisfy every constraint, without claiming they are true.
- Keep each query short and independently searchable. Return at most five unique variants.

Category: {category}
Question: {question}

Return JSON only: {{"queries":["query one","query two"]}}"""


def parse_query_plan(
    raw: str,
    *,
    primary_query: str,
    max_variants: int = 5,
) -> list[str]:
    """Parse a bounded query plan; malformed planner output becomes no variants."""

    text = str(raw or "").strip()
    if not text:
        return []
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match is not None:
        text = match.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    values = payload.get("queries") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    primary = str(primary_query or "").strip().casefold()
    output: list[str] = []
    seen: set[str] = {primary} if primary else set()
    for value in values:
        query = " ".join(str(value or "").strip().split())[:1000]
        normalized = query.casefold()
        if not query or normalized in seen:
            continue
        seen.add(normalized)
        output.append(query)
        if len(output) >= max(1, min(7, int(max_variants or 1))):
            break
    return output


def build_query_variants(question: str, *, category: str) -> list[str]:
    """Create cheap entity/clause variants for evidence-set retrieval."""

    text = str(question or "").strip()
    if not text:
        return []
    entities: list[str] = []
    for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text):
        entity = match.group(0).strip()
        if entity.casefold() in _QUERY_STOPWORDS:
            continue
        if entity not in entities:
            entities.append(entity)
    entity_tokens = {
        token.casefold()
        for entity in entities
        for token in re.findall(r"[A-Za-z]+", entity)
    }
    topic_tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", text):
        normalized = re.sub(r"['’]s$", "", token.casefold())
        if normalized in _QUERY_STOPWORDS or normalized in entity_tokens:
            continue
        if normalized not in topic_tokens:
            topic_tokens.append(normalized)
    topic = " ".join(topic_tokens[:8])
    variants: list[str] = []
    for entity in entities[:4]:
        candidate = f"{entity} {topic}".strip()
        if candidate and candidate.casefold() != text.casefold():
            variants.append(candidate)

    if category in {"multi-hop", "temporal"}:
        clauses = re.split(
            r"(?i)\b(?:and|before|after|while|compared\s+to|versus|vs\.?|then)\b|[,;]",
            text,
        )
        for clause in clauses:
            candidate = " ".join(clause.strip(" ?.").split())
            if len(candidate.split()) < 2:
                continue
            if candidate.casefold() == text.casefold():
                continue
            variants.append(candidate)

    output: list[str] = []
    seen: set[str] = set()
    for candidate in variants:
        normalized = candidate.casefold()
        if not candidate or normalized in seen:
            continue
        seen.add(normalized)
        output.append(candidate[:1000])
        if len(output) >= 7:
            break
    return output


def retrieval_metrics(
    results: list[dict[str, Any]],
    *,
    gold_evidence_ids: list[str],
    memory_map: dict[str, dict[str, Any]],
    cutoffs: tuple[int, ...] = (5, 10, 20, 50),
) -> dict[str, dict[str, Any]]:
    """Measure evidence recall by mapping memory/chunk ids to dialogue ids."""

    gold = {str(value) for value in gold_evidence_ids if str(value)}
    metrics: dict[str, dict[str, Any]] = {}
    for cutoff in cutoffs:
        retrieved: set[str] = set()
        for result in results[: max(0, int(cutoff))]:
            memory_id = str(result.get("id") or "")
            mapping = memory_map.get(memory_id) or {}
            retrieved.update(
                str(value)
                for value in (mapping.get("dia_ids") or [])
                if str(value)
            )
        matched = gold & retrieved
        metrics[str(cutoff)] = {
            "retrieved_evidence_ids": sorted(retrieved),
            "any_recall": bool(matched) if gold else None,
            "all_recall": gold <= retrieved if gold else None,
            "recall_fraction": len(matched) / len(gold) if gold else None,
        }
    return metrics


def format_evidence(
    results: list[dict[str, Any]],
    *,
    memory_map: dict[str, dict[str, Any]],
    max_chars: int,
    chronological: bool,
) -> str:
    """Format retrieved memories while preserving rank and event provenance."""

    rows: list[tuple[int, int, str]] = []
    for rank, result in enumerate(results, 1):
        memory_id = str(result.get("id") or "")
        mapping = memory_map.get(memory_id) or {}
        dia_ids = [str(value) for value in (mapping.get("dia_ids") or [])]
        event_order = int(mapping.get("event_order") or rank)
        event_time = str(mapping.get("event_time") or "").strip()
        score = float(result.get("score") or 0.0)
        content = str(result.get("content") or result.get("summary") or "").strip()
        event_time_note = (
            f" | event_time={event_time} (when the message was sent, not necessarily "
            "when the referenced event happened)"
            if event_time
            else ""
        )
        block = (
            f"[retrieval_rank={rank} | memory_id={memory_id} | "
            f"evidence_ids={','.join(dia_ids)} | score={score:.4f}{event_time_note}]\n{content}"
        )
        rows.append((event_order, rank, block))
    bounded_chars = max(1, int(max_chars or 1))
    selected_rows: list[tuple[int, int, str]] = []
    used = 0
    for row in rows:
        block = row[2]
        additional = len(block) + (2 if selected_rows else 0)
        if used + additional > bounded_chars:
            break
        selected_rows.append(row)
        used += additional
    if chronological:
        selected_rows.sort(key=lambda row: (row[0], row[1]))
    return (
        "\n\n".join(row[2] for row in selected_rows)
        if selected_rows
        else "(no evidence retrieved)"
    )
