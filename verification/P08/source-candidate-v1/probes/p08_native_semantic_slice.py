"""P08 TRAIN-only native semantic slice over frozen Gemini transport vectors.

Reads calibrated train pairs and precomputed 3072-D embeddings from locally
verified frozen policy and vectors-manifest inputs.  Never calls an embedding
API, never opens validation/SEALED/gold/eval fixtures, and never claims blind
validation or full semantic acceptance.

Run with the isolated Lance environment when executing the semantic path:
``.execution/TEST-P08-native/Scripts/python.exe probes/p08_native_semantic_slice.py``
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import stat
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

E2E_ROOT = ROOT / ".execution" / "TEST-P08-native-e2e"
DEFAULT_TRAIN_PATH = ROOT / ".execution" / "TEST-P08-CALIBRATION-v5" / "train.jsonl"
TRAIN_SHA256 = "f43518d9af0ed155f021ed937671d01db9a502650bf873968c0154acd2de1f6c"
TRANSPORT_SPACE_ID = "dcffe30132272913fcec22269c66184e2da0086bbf5d7d20dae9bb8453bac4d2"
DIMENSIONS = 3072
EXPECTED_ROWS = 24
EXPECTED_LINEAGE = 72
ROLES = ("source", "positive_query", "negative_query")
TASK_BY_ROLE = {
    "source": "RETRIEVAL_DOCUMENT",
    "positive_query": "RETRIEVAL_QUERY",
    "negative_query": "RETRIEVAL_QUERY",
}
FORBIDDEN_SEGMENTS = frozenset({"validation", "sealed", "gold", "eval"})
SCOPE_ID = "TEST-scope"
BRANCH_ID = "TEST-main"
AGENT_ID = "TEST-agent"
INSTALLATION_ID = "TEST-installation"
_START = time.perf_counter()


class ProbeClock:
    def utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def monotonic(self) -> float:
        return time.monotonic()


def nfkc_input_sha256(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFKC", text).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_vector_sha256(vector: Sequence[float]) -> str:
    payload = json.dumps(list(vector), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    mode = getattr(info, "st_mode", getattr(info, "st_file_mode", 0))
    return stat.S_ISLNK(mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def path_guard_issue(path: Path) -> str | None:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    for current in (candidate, *candidate.parents):
        if _is_reparse_point(current):
            return "reparse_or_symlink_target" if current == candidate else "reparse_or_symlink_ancestor"
    resolved = candidate.resolve()
    parts = {part.casefold() for part in resolved.parts}
    blocked = sorted(parts.intersection(FORBIDDEN_SEGMENTS))
    if blocked:
        return f"forbidden_path_segment:{blocked[0]}"
    return None


def _finite_vector(values: object) -> tuple[float, ...] | None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    out: list[float] = []
    for value in values:
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            return None
        out.append(float(value))
    if len(out) != DIMENSIONS:
        return None
    if not any(abs(v) > 0.0 for v in out):
        return None
    return tuple(out)


def _normalize_lineage(
    manifest: Mapping[str, Any],
    *,
    train_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    raw = manifest.get("lineage", manifest.get("entries"))
    if raw is None:
        raise ValueError("lineage_missing")
    if isinstance(raw, Mapping):
        items = []
        try:
            ordered_keys = sorted(raw, key=lambda item: int(item))
        except (TypeError, ValueError) as exc:
            raise ValueError("lineage_index_keys_invalid") from exc
        for key in ordered_keys:
            if not isinstance(raw[key], Mapping):
                raise ValueError(f"lineage_entry_invalid:{key}")
            entry = dict(raw[key])
            entry.setdefault("index", int(key))
            items.append(entry)
    elif isinstance(raw, list):
        if any(not isinstance(entry, Mapping) for entry in raw):
            raise ValueError("lineage_entry_invalid")
        items = [dict(entry) for entry in raw]
    else:
        raise ValueError("lineage_shape_invalid")
    if len(items) != EXPECTED_LINEAGE:
        raise ValueError(f"lineage_count:{len(items)}")
    normalized: list[dict[str, Any]] = []
    for position, entry in enumerate(items):
        index = int(entry.get("index", position))
        if index != position:
            raise ValueError(f"lineage_index_gap:{position}:{index}")
        role = str(entry.get("role", ""))
        if role not in TASK_BY_ROLE:
            raise ValueError(f"lineage_role_invalid:{role}")
        row_index = entry.get("row_index", entry.get("pair_index", entry.get("row")))
        if row_index is None:
            row_index = position // 3
        row_index = int(row_index)
        if row_index < 0 or row_index >= EXPECTED_ROWS:
            raise ValueError(f"lineage_row_index:{row_index}")
        expected_role = ROLES[position % 3]
        if role != expected_role:
            raise ValueError(f"lineage_role_order:{position}:{role}")
        text = entry.get("text", entry.get("input"))
        if text is None and train_rows is not None:
            row = train_rows[row_index]
            text = row["text"] if role == "source" else row[role]
        if type(text) is not str or not text:
            raise ValueError(f"lineage_text_missing:{position}")
        input_sha = entry.get(
            "input_sha256",
            entry.get("nfkc_input_sha256", entry.get("text_sha256")),
        )
        if type(input_sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", input_sha):
            raise ValueError(f"lineage_input_sha_invalid:{position}")
        if input_sha != nfkc_input_sha256(text):
            raise ValueError(f"lineage_input_sha_mismatch:{position}")
        task_type = entry.get("task_type", entry.get("taskType", TASK_BY_ROLE[role]))
        if task_type != TASK_BY_ROLE[role]:
            raise ValueError(f"lineage_task_type:{position}:{task_type}")
        normalized.append(
            {
                "index": index,
                "row_index": row_index,
                "role": role,
                "text": text,
                "input_sha256": input_sha,
                "task_type": task_type,
                "vector_sha256": entry.get("vector_sha256"),
            }
        )
    return normalized


def _resolve_repo_path(anchor: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute():
        return rel.resolve()
    for base in (anchor.parent, ROOT):
        candidate = (base / rel).resolve()
        if candidate.is_file():
            return candidate
    return (ROOT / rel).resolve()


def _cross_check_train_lineage(rows: list[dict[str, Any]], lineage: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if len(rows) != EXPECTED_ROWS:
        return issues
    for row_index, row in enumerate(rows):
        base = row_index * 3
        source = lineage[base]
        positive = lineage[base + 1]
        negative = lineage[base + 2]
        if source["text"] != row["text"]:
            issues.append(f"train_source_text_mismatch:{row_index}")
        if positive["text"] != row["positive_query"]:
            issues.append(f"train_positive_query_mismatch:{row_index}")
        if negative["text"] != row["negative_query"]:
            issues.append(f"train_negative_query_mismatch:{row_index}")
        if int(source["row_index"]) != row_index:
            issues.append(f"lineage_row_index_mismatch:{row_index}")
    return issues


def _policy_receipts(policy: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = policy.get("source_receipts", ())
    if not isinstance(raw, list):
        return ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _receipt_for_path(policy: Mapping[str, Any], path: Path) -> dict[str, Any] | None:
    resolved = path.resolve()
    for receipt in _policy_receipts(policy):
        raw_path = receipt.get("path")
        if type(raw_path) is not str:
            continue
        candidate = _resolve_repo_path(path, raw_path)
        if candidate == resolved:
            return receipt
    return None


def _artifact_descriptor(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    policy: Mapping[str, Any],
) -> tuple[str, str]:
    artifact = manifest.get("vector_artifact") or manifest.get("vectors_artifact") or manifest.get("artifact")
    if isinstance(artifact, Mapping):
        rel = artifact.get("path", artifact.get("relative_path", artifact.get("file")))
        expected_sha = artifact.get("sha256", artifact.get("digest"))
        if type(rel) is str and type(expected_sha) is str:
            return rel, expected_sha
    for path_key, sha_key in (
        ("vector_artifact_path", "vector_artifact_sha256"),
        ("vectors_path", "vectors_sha256"),
        ("vectors_file", "vectors_file_sha256"),
    ):
        rel = manifest.get(path_key)
        expected_sha = manifest.get(sha_key)
        if type(rel) is str and type(expected_sha) is str:
            return rel, expected_sha

    # The current frozen P08 composition manifest records lineage while its
    # frozen policy binds the sibling vectors.json receipt.  Permit that
    # shape only when both receipts are present and path/hash-bound.
    manifest_receipt = _receipt_for_path(policy, manifest_path)
    for receipt in _policy_receipts(policy):
        raw_path = receipt.get("path")
        expected_sha = receipt.get("sha256")
        if type(raw_path) is not str or type(expected_sha) is not str:
            continue
        if Path(raw_path).name.casefold() == "vectors.json":
            artifact_path = _resolve_repo_path(manifest_path, raw_path)
            if manifest_receipt is not None and manifest_receipt.get("sha256") == sha256_file(manifest_path):
                return str(artifact_path), expected_sha
    raise ValueError("vector_artifact_missing")


def _load_vector_artifact(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    policy: Mapping[str, Any],
) -> tuple[dict[int, tuple[float, ...]], Path, str]:
    rel, expected_sha = _artifact_descriptor(manifest, manifest_path, policy)
    artifact_path = _resolve_repo_path(manifest_path, rel)
    issue = path_guard_issue(artifact_path)
    if issue:
        raise ValueError(issue)
    if not artifact_path.is_file():
        raise ValueError("vector_artifact_missing_file")
    actual_sha = sha256_file(artifact_path)
    if actual_sha != expected_sha:
        raise ValueError("vector_artifact_sha_mismatch")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    vectors = payload.get("vectors", payload.get("entries", payload))
    by_index: dict[int, tuple[float, ...]] = {}
    if isinstance(vectors, Mapping):
        for key, value in vectors.items():
            vector = _finite_vector(value.get("embedding", value.get("vector", value)) if isinstance(value, Mapping) else value)
            if vector is None:
                raise ValueError(f"vector_invalid:{key}")
            by_index[int(key)] = vector
    elif isinstance(vectors, list):
        for position, entry in enumerate(vectors):
            if isinstance(entry, Mapping):
                index = int(entry.get("index", entry.get("lineage_index", position)))
                vector_value = entry.get("embedding", entry.get("vector"))
            else:
                # The calibration receipt uses vectors.json with a plain
                # ordered list of arrays.
                index = position
                vector_value = entry
            vector = _finite_vector(vector_value)
            if vector is None:
                raise ValueError(f"vector_invalid:{index}")
            by_index[index] = vector
    else:
        raise ValueError("vector_artifact_shape_invalid")
    if len(by_index) != EXPECTED_LINEAGE:
        raise ValueError(f"vector_count:{len(by_index)}")
    for index in range(EXPECTED_LINEAGE):
        if index not in by_index:
            raise ValueError(f"vector_index_missing:{index}")
    return by_index, artifact_path, expected_sha


def _policy_threshold(policy: Mapping[str, Any]) -> float:
    for key in ("selected_threshold", "threshold", "vector_threshold"):
        value = policy.get(key)
        if value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f"threshold_invalid:{key}")
        threshold = float(value)
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold_out_of_range")
        return threshold
    raise ValueError("threshold_missing")


def _verify_policy(path: Path, expected_sha: str) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    guard = path_guard_issue(path)
    if guard:
        return {}, [guard]
    if not path.is_file():
        return {}, ["policy_missing"]
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        issues.append("policy_sha_mismatch")
        return {}, issues
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, [f"policy_parse:{type(exc).__name__}"]
    if not isinstance(policy, Mapping):
        return {}, ["policy_shape_invalid"]
    policy = dict(policy)
    if policy.get("frozen") is not True:
        issues.append("policy_not_frozen")
    if policy.get("validation_authorized") is True:
        issues.append("policy_validation_authorized")
    space_id = policy.get("space_id")
    if space_id != TRANSPORT_SPACE_ID:
        issues.append("policy_space_mismatch")
    embedding = policy.get("embedding_space")
    if not isinstance(embedding, Mapping) or embedding.get("dimensions") != DIMENSIONS:
        issues.append("policy_dimensions_mismatch")
    if not isinstance(embedding, Mapping) or embedding.get("model") != "gemini-embedding-001":
        issues.append("policy_model_mismatch")
    if not isinstance(embedding, Mapping) or embedding.get("metric") != "cosine":
        issues.append("policy_metric_mismatch")
    if not isinstance(embedding, Mapping) or embedding.get("input_preprocessing", {}).get("id") != "nfkc-v1":
        issues.append("policy_preprocessing_mismatch")
    receipts = policy.get("source_receipts", ())
    train_bound = False
    if isinstance(receipts, list):
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            if str(receipt.get("sha256", "")) == TRAIN_SHA256:
                train_bound = True
    if not train_bound:
        issues.append("policy_dataset_unbound")
    try:
        _policy_threshold(policy)
    except ValueError as exc:
        issues.append(str(exc))
    return policy, issues


def _verify_vectors_manifest(
    path: Path,
    expected_sha: str,
    policy: Mapping[str, Any],
    *,
    train_rows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, tuple[float, ...]], list[str]]:
    issues: list[str] = []
    guard = path_guard_issue(path)
    if guard:
        return {}, [], {}, [guard]
    if not path.is_file():
        return {}, [], {}, ["vectors_manifest_missing"]
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        return {}, [], {}, ["vectors_manifest_sha_mismatch"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, [], {}, [f"vectors_manifest_parse:{type(exc).__name__}"]
    if not isinstance(manifest, Mapping):
        return {}, [], {}, ["vectors_manifest_shape_invalid"]
    manifest = dict(manifest)
    manifest_space = manifest.get(
        "space_id",
        manifest.get("embedding_space_id", manifest.get("final_space_id")),
    )
    if manifest.get("frozen") is not True:
        # composition-manifest.json predates the final freeze field.  It is
        # still admissible only when the frozen policy explicitly binds this
        # exact manifest path/hash and also binds the sibling vectors receipt.
        bound = _receipt_for_path(policy, path)
        sibling_vectors = any(
            isinstance(receipt.get("path"), str)
            and Path(receipt["path"]).name.casefold() == "vectors.json"
            and type(receipt.get("sha256")) is str
            for receipt in _policy_receipts(policy)
        )
        if bound is None or bound.get("sha256") != sha256_file(path) or not sibling_vectors:
            issues.append("vectors_manifest_not_frozen")
        else:
            manifest["frozen_via_policy_receipt"] = True
    if manifest_space not in (None, TRANSPORT_SPACE_ID):
        issues.append("vectors_manifest_space_mismatch")
    dataset_sha = manifest.get("dataset_sha256", manifest.get("train_sha256"))
    if dataset_sha is not None and dataset_sha != TRAIN_SHA256:
        issues.append("vectors_manifest_dataset_sha_mismatch")
    try:
        lineage = _normalize_lineage(manifest, train_rows=train_rows)
        vectors, artifact_path, artifact_sha = _load_vector_artifact(manifest, path, policy)
    except ValueError as exc:
        issues.append(str(exc))
        return manifest, [], {}, issues
    if train_rows:
        issues.extend(_cross_check_train_lineage(train_rows, lineage))
    for entry in lineage:
        index = entry["index"]
        vector = vectors[index]
        expected_vector_sha = entry.get("vector_sha256")
        if expected_vector_sha is not None and expected_vector_sha != compact_vector_sha256(vector):
            issues.append(f"vector_sha_mismatch:{index}")
    if policy and policy.get("space_id") != (manifest_space or TRANSPORT_SPACE_ID):
        issues.append("policy_manifest_space_divergence")
    artifact_receipt = _receipt_for_path(policy, artifact_path)
    if artifact_receipt is not None and artifact_receipt.get("sha256") != artifact_sha:
        issues.append("policy_vector_artifact_sha_divergence")
    if artifact_receipt is None:
        issues.append("policy_vector_artifact_unbound")
    manifest["resolved_vector_artifact"] = str(artifact_path)
    manifest["resolved_vector_artifact_sha256"] = artifact_sha
    return manifest, lineage, vectors, issues


def _verify_train(path: Path = DEFAULT_TRAIN_PATH) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    guard = path_guard_issue(path)
    if guard:
        return [], [guard]
    if not path.is_file():
        return [], ["train_missing"]
    actual_sha = sha256_file(path)
    if actual_sha != TRAIN_SHA256:
        issues.append("train_sha_mismatch")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            issues.append(f"train_json_invalid:{line_number}")
            continue
        if not isinstance(row, Mapping):
            issues.append(f"train_row_shape_invalid:{line_number}")
            continue
        row = dict(row)
        for field in ("id", "category", "text", "positive_query", "negative_query", "require_zero_overlap"):
            if field not in row:
                issues.append(f"train_field_missing:{line_number}:{field}")
        if not isinstance(row.get("id"), str) or not isinstance(row.get("category"), str):
            issues.append(f"train_identity_invalid:{line_number}")
        for field in ("text", "positive_query", "negative_query"):
            if not isinstance(row.get(field), str) or not row.get(field, "").strip():
                issues.append(f"train_text_invalid:{line_number}:{field}")
        if type(row.get("require_zero_overlap")) is not bool:
            issues.append(f"train_zero_overlap_invalid:{line_number}")
        rows.append(row)
    if len(rows) != EXPECTED_ROWS:
        issues.append(f"train_row_count:{len(rows)}")
    if [row.get("id") for row in rows] != [f"unit-{index:03d}" for index in range(1, EXPECTED_ROWS + 1)]:
        issues.append("train_id_order_invalid")
    return rows, issues


def _bounded_local_checks() -> dict[str, Any]:
    checks = {
        "imports": False,
        "forbidden_path_guard": path_guard_issue(ROOT / "fixtures" / "validation" / "x") == "forbidden_path_segment:validation",
        "nfkc_sha_stable": nfkc_input_sha256(" 测试 ") == nfkc_input_sha256("测试"),
        "train_defaults": str(DEFAULT_TRAIN_PATH).endswith("train.jsonl"),
        "transport_space_id": TRANSPORT_SPACE_ID,
    }
    try:
        from scope_recall.core.recall_policy import SPACE_ID  # noqa: F401

        checks["imports"] = True
        checks["core_space_id"] = SPACE_ID
        checks["core_space_matches_transport"] = SPACE_ID == TRANSPORT_SPACE_ID
    except Exception as exc:
        checks["import_error"] = type(exc).__name__
    return checks


def _write_receipt(run_dir: Path, receipt: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "probe-result.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _stdout_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "semantic_ran",
        "blind_validation_ran",
        "semantic_acceptance_claim",
        "reason",
        "positive_recall",
        "negative_rejection",
        "semantic_negative_rejection",
        "zero_overlap",
        "run_dir",
        "elapsed_seconds",
    )
    return {key: receipt[key] for key in keys if key in receipt}


class ManifestQueryEmbedding:
    """Replay frozen query vectors by normalized-input SHA only."""

    def __init__(self, mapping: Mapping[str, tuple[float, ...]]) -> None:
        self._mapping = dict(mapping)

    def embed_query(self, text: str, *, remaining_seconds: float) -> tuple[float, ...]:
        if remaining_seconds <= 0:
            raise RuntimeError("query embedding budget exhausted")
        digest = nfkc_input_sha256(text)
        try:
            return self._mapping[digest]
        except KeyError:
            raise KeyError(f"unknown normalized-input sha: {digest}") from None


class SearchCallRecorder:
    def __init__(self, store) -> None:
        self._store = store
        self.calls: list[dict[str, object]] = []
        self.active_trace: QueryTrace | None = None

    def search(self, vector: list[float], *, scope_id: str, limit: int) -> list[dict]:
        call = {
            "scope_id": scope_id,
            "limit": limit,
            "vector_dim": len(vector),
            "vector_sha256": compact_vector_sha256(vector),
        }
        self.calls.append(call)
        if self.active_trace is not None:
            self.active_trace.native_calls.append(dict(call))
        return self._store.search(vector, scope_id=scope_id, limit=limit)


class QueryTrace:
    """In-loop evidence for all channels without a second retrieval call."""

    def __init__(self, *, pair_id: str, role: str, query: str) -> None:
        self.pair_id = pair_id
        self.role = role
        self.query = query
        self.raw_candidates: list[dict[str, Any]] = []
        self.policy_decisions: list[dict[str, Any]] = []
        self.hard_identifier_checks: list[dict[str, Any]] = []
        self.native_calls: list[dict[str, Any]] = []


class TracingPolicy:
    """Delegate the frozen policy while retaining admission decisions."""

    def __init__(self, base) -> None:
        self.base = base
        self.rrf_k = base.rrf_k
        self.active_trace: QueryTrace | None = None

    def vector_admission(self, candidate):
        admitted, reason = self.base.vector_admission(candidate)
        if self.active_trace is not None:
            self.active_trace.policy_decisions.append(
                {
                    "channel": "vector",
                    "candidate": _candidate_payload(candidate),
                    "admitted": admitted,
                    "reason": reason,
                }
            )
        return admitted, reason

    def lexical_admission(self, candidate, query: str, *, exact: bool = False):
        admitted, reason = self.base.lexical_admission(candidate, query, exact=exact)
        if self.active_trace is not None:
            self.active_trace.policy_decisions.append(
                {
                    "channel": candidate.source,
                    "candidate": _candidate_payload(candidate),
                    "admitted": admitted,
                    "reason": reason,
                }
            )
        return admitted, reason


class TracingStorageReader:
    """Record SQLite channel candidates and the production hard-id check."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.active_trace: QueryTrace | None = None

    def _record(self, channel: str, candidates) -> tuple:
        result = tuple(candidates or ())
        if self.active_trace is not None:
            self.active_trace.raw_candidates.extend(
                {"channel": channel, "candidate": _candidate_payload(candidate)}
                for candidate in result
            )
        return result

    def exact(self, tx, context, *, limit: int):
        return self._record("exact_ref", self.inner.exact(tx, context, limit=limit))

    def lexical(self, tx, context, *, limit: int):
        return self._record("lexical", self.inner.lexical(tx, context, limit=limit))

    def recent(self, tx, context, *, limit: int):
        return self._record("recent_raw", self.inner.recent(tx, context, limit=limit))

    def related(self, tx, candidate, *, limit: int):
        return self._record("relation", self.inner.related(tx, candidate, limit=limit))

    def epoch(self, tx):
        return self.inner.epoch(tx)

    def hydrate(self, tx, candidate, context):
        obj = self.inner.hydrate(tx, candidate, context)
        if self.active_trace is not None and obj is not None and candidate.source != "exact_ref":
            from scope_recall.core.recall_policy import hard_identifiers, identifiers_compatible

            if hard_identifiers(context.query):
                self.active_trace.hard_identifier_checks.append(
                    {
                        "candidate": _candidate_payload(candidate),
                        "compatible": identifiers_compatible(context.query, obj.content),
                    }
                )
        return obj

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def _build_query_mapping(lineage: list[dict[str, Any]], vectors: dict[int, tuple[float, ...]]) -> dict[str, tuple[float, ...]]:
    mapping: dict[str, tuple[float, ...]] = {}
    for entry in lineage:
        if entry["role"] not in {"positive_query", "negative_query"}:
            continue
        digest = entry["input_sha256"]
        vector = vectors[entry["index"]]
        previous = mapping.get(digest)
        if previous is not None and previous != vector:
            raise ValueError(f"conflicting_query_mapping:{digest}")
        mapping[digest] = vector
    return mapping


def _expected_partitions(context, *, logical_scope_id: str, embedding_space: str) -> set[str]:
    from scope_recall.adapters.lance import physical_partition_scope_id

    combos = (
        (context.project_id, context.branch_id),
        (context.project_id, None),
        (None, context.branch_id),
        (None, None),
    )
    return {
        physical_partition_scope_id(
            agent_id=context.binding.agent_id,
            installation_id=context.binding.installation_id,
            embedding_space=embedding_space,
            logical_scope_id=logical_scope_id,
            project_id=project_id,
            branch_id=branch_id,
        )
        for project_id, branch_id in combos
    }


def _event(row_id: str, text: str, when: str) -> dict[str, Any]:
    return {
        "protocol_version": "1.1",
        "source_event_key": f"TEST-P08-semantic/{row_id}",
        "source_revision": 1,
        "origin": "external_document",
        "role": "document",
        "content": text,
        "occurred_at": when,
        "recorded_at": when,
        "time_precision": "instant",
        "capture_state": "complete",
        "evidence_refs": [],
        "dataset_id": "P08-CALIBRATION-v5",
    }


def _candidate_payload(candidate) -> dict[str, Any]:
    return {
        "kind": candidate.kind,
        "ref": candidate.ref,
        "revision": candidate.revision,
        "source_channel": candidate.source,
        "rank": candidate.rank,
        "lexical_score": candidate.lexical_score,
        "vector_score": candidate.vector_score,
        "vector_id": candidate.vector_id,
        "embedding_space": candidate.embedding_space,
        "fusion_score": candidate.fusion_score,
    }


def _item_payload(item) -> dict[str, Any]:
    return {
        "ref": item.ref,
        "revision": item.revision,
        "kind": item.kind,
        "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
        "source_kinds": list(getattr(item, "source_kinds", ()) or ()),
    }


def _channel_diagnostics(trace: QueryTrace, pipeline_result) -> list[dict[str, Any]]:
    """Merge raw channel observations and policy decisions from one search."""

    from collections import defaultdict

    final_candidates = {candidate.key: _candidate_payload(candidate) for candidate in pipeline_result.candidates}
    final_items = {(item.ref, item.revision) for item in pipeline_result.items}
    records: dict[tuple[tuple[str, str, int], str], dict[str, Any]] = {}

    def add(channel: str, payload: Mapping[str, Any], *, decision: Mapping[str, Any] | None = None) -> None:
        key = (
            str(payload.get("kind", "")),
            str(payload.get("ref", "")),
            int(payload.get("revision", 0)),
        )
        slot = records.setdefault(
            (key, channel),
            {
                "source_channel": channel,
                "rank": [],
                "lexical_scores": [],
                "vector_scores": [],
                "admissions": [],
            },
        )
        rank = payload.get("rank")
        if rank is not None and rank not in slot["rank"]:
            slot["rank"].append(rank)
        for field, target in (("lexical_score", "lexical_scores"), ("vector_score", "vector_scores")):
            value = payload.get(field)
            if value is not None and value not in slot[target]:
                slot[target].append(value)
        if decision is not None:
            admission = {"admitted": bool(decision.get("admitted")), "reason": decision.get("reason")}
            if admission not in slot["admissions"]:
                slot["admissions"].append(admission)

    for observation in trace.raw_candidates:
        add(str(observation["channel"]), observation["candidate"])
    for decision in trace.policy_decisions:
        add(str(decision["channel"]), decision["candidate"], decision=decision)
    for key, payload in final_candidates.items():
        add(str(payload["source_channel"]), payload)

    hard_by_key: dict[tuple[str, str, int], list[bool]] = defaultdict(list)
    for check in trace.hard_identifier_checks:
        payload = check["candidate"]
        key = (str(payload["kind"]), str(payload["ref"]), int(payload["revision"]))
        hard_by_key[key].append(bool(check["compatible"]))

    by_key: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for (key, _channel), slot in records.items():
        slot["rank"] = sorted(slot["rank"])
        slot["lexical_scores"] = sorted(slot["lexical_scores"], reverse=True)
        slot["vector_scores"] = sorted(slot["vector_scores"], reverse=True)
        slot["lexical_score"] = slot["lexical_scores"][0] if len(slot["lexical_scores"]) == 1 else slot["lexical_scores"]
        slot["vector_score"] = slot["vector_scores"][0] if len(slot["vector_scores"]) == 1 else slot["vector_scores"]
        by_key[key].append(slot)

    result: list[dict[str, Any]] = []
    for key in sorted(by_key):
        kind, ref, revision = key
        final = final_candidates.get(key)
        compatibilities = hard_by_key.get(key, [])
        result.append(
            {
                "kind": kind,
                "ref": ref,
                "revision": revision,
                "final_candidate": final is not None,
                "final_item": (ref, revision) in final_items,
                "channels": sorted(by_key[key], key=lambda item: item["source_channel"]),
                "hard_identifier_checked": bool(compatibilities),
                "hard_identifier_rejected": any(not compatible for compatible in compatibilities),
            }
        )
    return result


def _zero_overlap(query: str, source_text: str) -> dict[str, Any]:
    from scope_recall.core.events import lexical_terms, query_terms

    q_terms = set(query_terms(query))
    s_terms = set(lexical_terms(source_text))
    overlap = sorted(q_terms.intersection(s_terms))
    return {
        "query_terms": sorted(q_terms),
        "lexical_terms": sorted(s_terms),
        "overlap": overlap,
        "overlap_empty": not overlap,
    }


def _validate_slice_inputs(
    rows: list[dict[str, Any]],
    lineage: list[dict[str, Any]],
    vectors: dict[int, tuple[float, ...]],
    *,
    eligibility_negative_ids: frozenset[str],
) -> tuple[int, int]:
    """Validate the row/lineage contract before opening the native pipeline."""

    row_count = len(rows)
    if row_count <= 0:
        raise ValueError("slice_rows_empty")
    expected_lineage_count = row_count * len(ROLES)
    if len(lineage) != expected_lineage_count:
        raise ValueError(f"slice_lineage_count:{len(lineage)}:{expected_lineage_count}")

    row_ids: list[str] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"slice_row_shape:{row_index}")
        row_id = row.get("id")
        if type(row_id) is not str or not row_id:
            raise ValueError(f"slice_row_id:{row_index}")
        if row_id in row_ids:
            raise ValueError(f"slice_row_id_duplicate:{row_id}")
        row_ids.append(row_id)
        for field in ("text", "positive_query", "negative_query"):
            if type(row.get(field)) is not str or not row[field].strip():
                raise ValueError(f"slice_row_field:{row_index}:{field}")

    unknown_eligibility_ids = sorted(set(eligibility_negative_ids).difference(row_ids))
    if unknown_eligibility_ids:
        raise ValueError(f"eligibility_negative_id_unknown:{','.join(unknown_eligibility_ids)}")

    expected_vector_indices = set(range(expected_lineage_count))
    if set(vectors) != expected_vector_indices:
        raise ValueError(
            f"slice_vector_indices:{len(vectors)}:{expected_lineage_count}"
        )

    for position, entry in enumerate(lineage):
        if not isinstance(entry, Mapping):
            raise ValueError(f"slice_lineage_shape:{position}")
        expected_role = ROLES[position % len(ROLES)]
        expected_row_index = position // len(ROLES)
        if entry.get("index") != position:
            raise ValueError(f"slice_lineage_index:{position}:{entry.get('index')}")
        if entry.get("role") != expected_role:
            raise ValueError(f"slice_lineage_role:{position}:{entry.get('role')}")
        if entry.get("row_index") != expected_row_index:
            raise ValueError(
                f"slice_lineage_row_index:{position}:{entry.get('row_index')}"
            )
        expected_text = rows[expected_row_index]["text" if expected_role == "source" else expected_role]
        if entry.get("text") != expected_text:
            raise ValueError(f"slice_lineage_text:{position}")
        if entry.get("input_sha256") != nfkc_input_sha256(expected_text):
            raise ValueError(f"slice_lineage_input_sha:{position}")
        if entry.get("task_type") != TASK_BY_ROLE[expected_role]:
            raise ValueError(f"slice_lineage_task_type:{position}")

    return row_count, row_count - len(eligibility_negative_ids)


def _run_semantic_slice(
    *,
    run_dir: Path,
    policy: Mapping[str, Any],
    lineage: list[dict[str, Any]],
    vectors: dict[int, tuple[float, ...]],
    rows: list[dict[str, Any]],
    threshold: float,
    transport_space_id: str,
    eligibility_negative_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    eligibility_negative_ids = frozenset(eligibility_negative_ids)
    row_count, semantic_negative_total = _validate_slice_inputs(
        rows,
        lineage,
        vectors,
        eligibility_negative_ids=eligibility_negative_ids,
    )

    from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorPort, LanceVectorRecord
    from scope_recall.contracts import InstanceBinding, TrustedContext
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.core.recall_policy import RecallPolicy, SPACE_ID, hard_identifiers, identifiers_compatible
    from scope_recall.core.retrieval import SearchContext
    from scope_recall.lance_process_store import ProcessLanceVectorStore

    if SPACE_ID != transport_space_id:
        raise RuntimeError("core_space_mismatch")

    clock = ProbeClock()
    query_mapping = _build_query_mapping(lineage, vectors)
    sqlite_dir = run_dir / "sqlite"
    lance_dir = run_dir / "lance"
    binding = InstanceBinding(AGENT_ID, INSTALLATION_ID, sqlite_dir, frozenset({SCOPE_ID}), True)
    recall_policy = RecallPolicy(vector_threshold=threshold)
    traced_policy = TracingPolicy(recall_policy)
    store = ProcessLanceVectorStore(lance_dir, table_name="TEST_vectors", dimensions=DIMENSIONS)
    store.open()
    writer = LanceIndexWriter(store)
    recorder = SearchCallRecorder(store)
    embedding_port = ManifestQueryEmbedding(query_mapping)
    port = LanceVectorPort(
        recorder,
        embedding_port,
        expected_embedding_space=transport_space_id,
        clock=clock.monotonic,
    )
    core = MemoryCore(
        CoreConfig(binding),
        clock=clock,
        vectors=port,
        retrieval_policy=traced_policy,
    )
    traced_reader = TracingStorageReader(core.recall_pipeline.storage_reader)
    core.recall_pipeline.storage_reader = traced_reader
    units: list[dict[str, Any]] = []
    source_records: list[LanceVectorRecord] = []

    try:
        core.initialize()
        for row_index, row in enumerate(rows):
            project_id = f"TEST-P08-unit-{row_index + 1:03d}"
            context = TrustedContext(
                binding,
                f"TEST-P08-session-{row_index + 1:03d}",
                frozenset({SCOPE_ID}),
                "external_document",
                project_id=project_id,
                branch_id=BRANCH_ID,
            )
            # Keep every fixture source behind the query clock.  A later row
            # must not become a future source merely because the local wall
            # clock happens to be before 12:xx UTC.
            when = "2026-09-01T00:00:00Z"
            capture = core.record_event(
                context,
                _event(str(row["id"]), row["text"], when),
                scope_id=SCOPE_ID,
                remaining_seconds=10,
            )
            if capture.disposition != "inserted" or not capture.event_refs:
                raise RuntimeError(f"record_event_failed:{row['id']}:{capture.disposition}")
            source_ref = capture.event_refs[0]
            stored = core.source(context, source_ref.ref, source_ref.revision)
            if stored is None or stored.event["content"] != row["text"]:
                raise RuntimeError(f"sqlite_content_mismatch:{row['id']}")
            source_lineage = lineage[row_index * 3]
            if source_lineage["text"] != row["text"]:
                raise RuntimeError(f"lineage_source_text_mismatch:{row['id']}")
            source_records.append(
                LanceVectorRecord(
                    object_kind="event",
                    object_ref=source_ref.ref,
                    object_revision=source_ref.revision,
                    vector_id=f"TEST-P08-v5-{row_index + 1:03d}-source",
                    embedding_space=transport_space_id,
                    embedding=vectors[source_lineage["index"]],
                    scope_id=SCOPE_ID,
                    agent_id=AGENT_ID,
                    installation_id=INSTALLATION_ID,
                    project_id=project_id,
                    branch_id=BRANCH_ID,
                    updated_at=when,
                )
            )
            units.append(
                {
                    "row_index": row_index,
                    "pair_id": row["id"],
                    "category": row.get("category"),
                    "project_id": project_id,
                    "source_ref": source_ref.ref,
                    "source_revision": source_ref.revision,
                    "content_sha256": stored.content_sha256,
                    "require_zero_overlap": bool(row.get("require_zero_overlap")),
                    "context": context,
                    "source_text": row["text"],
                    "capture": {
                        "disposition": capture.disposition,
                        "durability": capture.durability,
                        "lexical_state": capture.lexical_state,
                        "semantic_state": capture.semantic_state,
                        "mutation": capture.mutation,
                    },
                }
            )

        # One explicit projection write through the one existing native helper.
        writer.upsert_records(source_records)
        native_row_count = store.count_rows()
        if native_row_count != row_count:
            raise RuntimeError(f"native_source_row_count:{native_row_count}")

        for unit in units:
            unit["ingest_status"] = core.status(unit["context"])

        per_query: list[dict[str, Any]] = []
        native_partitions: set[str] = set()
        positives_hit = 0
        negatives_rejected = 0
        semantic_negatives_rejected = 0
        hard_identifier_observations: list[dict[str, Any]] = []
        lexical_defects: list[dict[str, Any]] = []
        negative_false_positives: list[dict[str, Any]] = []
        zero_overlap_cases: list[dict[str, Any]] = []
        read_only_unchanged = True

        for unit in units:
            context = unit["context"]
            before_status = unit["ingest_status"]
            for role, query_field in (("positive", "positive_query"), ("negative", "negative_query")):
                query_text = rows[unit["row_index"]][query_field]
                trace = QueryTrace(pair_id=unit["pair_id"], role=role, query=query_text)
                traced_policy.active_trace = trace
                traced_reader.active_trace = trace
                recorder.active_trace = trace
                started = time.perf_counter()
                request = {
                    "protocol_version": "1.1",
                    "request_id": f"TEST-P08-{unit['pair_id']}-{role}",
                    "query": query_text,
                    "mode": "current",
                    "max_items": 6,
                    "budget_tokens": 1200,
                }
                search_context = SearchContext.from_request(
                    request,
                    context,
                    now=clock.utc_now(),
                    deadline=clock.monotonic() + 5,
                )
                try:
                    pipeline_started = time.perf_counter()
                    pipeline_result = core.recall_pipeline.search(search_context)
                    pipeline_elapsed = time.perf_counter() - pipeline_started
                finally:
                    traced_policy.active_trace = None
                    traced_reader.active_trace = None
                    recorder.active_trace = None

                after_status = core.status(context)
                unchanged = (
                    after_status.memory_epoch == before_status.memory_epoch
                    and after_status.pending_work == before_status.pending_work
                )
                read_only_unchanged = read_only_unchanged and unchanged
                if not unchanged:
                    raise RuntimeError("read_only_epoch_or_queue_changed")
                admitted_source = any(
                    item.ref == unit["source_ref"] and item.revision == unit["source_revision"]
                    for item in pipeline_result.items
                )
                channel_diag = _channel_diagnostics(trace, pipeline_result)
                expected_diag = next(
                    (
                        item
                        for item in channel_diag
                        if item["ref"] == unit["source_ref"]
                        and item["revision"] == unit["source_revision"]
                    ),
                    None,
                )
                hard_id_requested = bool(hard_identifiers(query_text))
                hard_id_compatible = identifiers_compatible(query_text, unit["source_text"])
                hard_id_rejected = hard_id_requested and not hard_id_compatible
                if hard_id_requested:
                    hard_identifier_observations.append(
                        {
                            "pair_id": unit["pair_id"],
                            "role": role,
                            "query": query_text,
                            "compatible": hard_id_compatible,
                            "admitted": admitted_source,
                            "candidate_hard_identifier_rejected": bool(
                                expected_diag and expected_diag["hard_identifier_rejected"]
                            ),
                        }
                    )

                vector_scores: list[float] = []
                vector_rejected_below_threshold = False
                lexical_one_term_admitted = False
                if expected_diag is not None:
                    for channel in expected_diag["channels"]:
                        values = channel.get("vector_scores", ())
                        if isinstance(values, list):
                            vector_scores.extend(float(value) for value in values)
                        elif values not in (None, ""):
                            vector_scores.append(float(values))
                        if channel["source_channel"] == "vector":
                            vector_rejected_below_threshold = vector_rejected_below_threshold or any(
                                decision.get("reason") == "vector_below_threshold"
                                and not decision.get("admitted")
                                for decision in channel.get("admissions", ())
                            )
                        if channel["source_channel"] == "lexical":
                            lexical_one_term_admitted = lexical_one_term_admitted or (
                                any(decision.get("admitted") for decision in channel.get("admissions", ()))
                                and any(float(value) == 1.0 for value in channel.get("lexical_scores", ()))
                            )
                lexical_defect = (
                    role == "negative"
                    and admitted_source
                    and vector_rejected_below_threshold
                    and lexical_one_term_admitted
                    and not hard_id_rejected
                )
                if lexical_defect:
                    defect = {
                        "pair_id": unit["pair_id"],
                        "category": unit["category"],
                        "query": query_text,
                        "vector_scores": vector_scores,
                        "threshold": threshold,
                        "defect": "lexical_one_term_rescue_after_vector_below_threshold",
                        "channels": channel_diag,
                    }
                    lexical_defects.append(defect)
                if role == "negative" and admitted_source:
                    negative_false_positives.append(
                        {
                            "pair_id": unit["pair_id"],
                            "category": unit["category"],
                            "query": query_text,
                            "hard_identifier_rejected": hard_id_rejected,
                            "lexical_one_term_rescue": lexical_defect,
                            "channels": channel_diag,
                        }
                    )

                if role == "positive" and admitted_source:
                    positives_hit += 1
                if role == "negative" and not admitted_source:
                    negatives_rejected += 1
                    if unit["pair_id"] not in eligibility_negative_ids:
                        semantic_negatives_rejected += 1

                overlap_report = None
                if role == "positive" and unit["require_zero_overlap"]:
                    overlap_report = _zero_overlap(query_text, unit["source_text"])
                    zero_overlap_cases.append(
                        {
                            "pair_id": unit["pair_id"],
                            "query": query_text,
                            **overlap_report,
                            "positive_admitted": admitted_source,
                        }
                    )

                native_partitions.update(call["scope_id"] for call in trace.native_calls)
                per_query.append(
                    {
                        "pair_id": unit["pair_id"],
                        "row_index": unit["row_index"],
                        "role": role,
                        "query": query_text,
                        "query_input_sha256": nfkc_input_sha256(query_text),
                        "expected_source_ref": unit["source_ref"],
                        "expected_project_id": unit["project_id"],
                        "pair_source_admitted": admitted_source,
                        "final_item_identities": [_item_payload(item) for item in pipeline_result.items],
                        "final_candidates": [_candidate_payload(candidate) for candidate in pipeline_result.candidates],
                        "channel_diagnostics": channel_diag,
                        "gaps": list(pipeline_result.gaps),
                        "hard_identifier_requested": hard_id_requested,
                        "hard_identifier_compatible": hard_id_compatible,
                        "hard_identifier_rejected": hard_id_rejected,
                        "zero_overlap": overlap_report,
                        "native_search_calls": len(trace.native_calls),
                        "native_partitions": sorted({call["scope_id"] for call in trace.native_calls}),
                        "sqlite_before": {
                            "memory_epoch": before_status.memory_epoch,
                            "pending_work": before_status.pending_work,
                        },
                        "sqlite_after": {
                            "memory_epoch": after_status.memory_epoch,
                            "pending_work": after_status.pending_work,
                        },
                        "timing": {
                            "pipeline_seconds": round(pipeline_elapsed, 6),
                            "total_seconds": round(time.perf_counter() - started, 6),
                        },
                    }
                )

        semantic_status = "TRAIN_COMPLETED_WITH_DEFECTS" if lexical_defects else "TRAIN_COMPLETED"
        semantic_report = {
            "status": semantic_status,
            "scope": "TRAIN_ONLY",
            "semantic_ran": True,
            "blind_validation_ran": False,
            "validation_authorized": False,
            "semantic_acceptance_claim": False,
            "source_content_path": "MemoryCore.record_event -> SQLite source_events -> RetrievalStorage.hydrate",
            "sqlite_instance": str(sqlite_dir),
            "single_core_pipeline": True,
            "single_process_lance_helper": True,
            "lance_writer_calls": 1,
            "embedding_api_calls": 0,
            "eligibility_negative_ids": sorted(eligibility_negative_ids),
            "transport_space_id": transport_space_id,
            "core_space_id": SPACE_ID,
            "vector_threshold": threshold,
            "vector_space_checks": {
                "transport_space_id": transport_space_id,
                "core_space_id": SPACE_ID,
                "core_matches_transport": SPACE_ID == transport_space_id,
                "dimensions": DIMENSIONS,
                "version": "p08-v1 physical partitions",
            },
            "units": [
                {
                    "pair_id": unit["pair_id"],
                    "project_id": unit["project_id"],
                    "source_ref": unit["source_ref"],
                    "source_revision": unit["source_revision"],
                    "content_sha256": unit["content_sha256"],
                    "capture": unit["capture"],
                }
                for unit in units
            ],
            "per_query": per_query,
            "metrics": {
                "positive_recall": {
                    "hit": positives_hit,
                    "total": row_count,
                    "rate": round(positives_hit / row_count, 6),
                },
                "negative_rejection": {
                    "rejected": negatives_rejected,
                    "total": row_count,
                    "rate": round(negatives_rejected / row_count, 6),
                },
                "semantic_negative_rejection_excluding_hard_identifier": {
                    "rejected": semantic_negatives_rejected,
                    "total": semantic_negative_total,
                    "rate": round(semantic_negatives_rejected / semantic_negative_total, 6)
                    if semantic_negative_total
                    else 0.0,
                    "excluded_pair_ids": sorted(eligibility_negative_ids),
                },
                "zero_overlap": {
                    "cases": zero_overlap_cases,
                    "total": len(zero_overlap_cases),
                    "native_queries": len(zero_overlap_cases),
                    "overlap_empty": all(case["overlap_empty"] for case in zero_overlap_cases),
                    "positive_hit": sum(1 for case in zero_overlap_cases if case["positive_admitted"]),
                    "positive_recall": round(
                        sum(1 for case in zero_overlap_cases if case["positive_admitted"])
                        / len(zero_overlap_cases),
                        6,
                    )
                    if zero_overlap_cases
                    else 0.0,
                },
                "negative_false_positives": negative_false_positives,
            },
            "identifier_eligibility_observations": hard_identifier_observations,
            "lexical_below_threshold_one_term_defects": lexical_defects,
            "read_only_epoch_queue_unchanged": read_only_unchanged,
            "native_evidence": {
                "source_vector_rows": native_row_count,
                "search_call_count": sum(item["native_search_calls"] for item in per_query),
                "physical_partitions": sorted(native_partitions),
                "per_query_native_calls": [item["native_search_calls"] for item in per_query],
                "query_vector_dimensions": [],
            },
            "elapsed_seconds": round(time.perf_counter() - _START, 6),
        }
        # Replace the intentionally empty comprehension above with dimensions
        # observed by the recorder, without retaining raw vector values.
        semantic_report["native_evidence"]["query_vector_dimensions"] = sorted(
            {int(call["vector_dim"]) for call in recorder.calls if "vector_dim" in call}
        )
        (run_dir / "semantic-report.json").write_text(
            json.dumps(semantic_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return semantic_report
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P08 TRAIN-only native semantic slice")
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--vectors-manifest", type=Path, required=True)
    parser.add_argument("--vectors-manifest-sha256", required=True)
    parser.add_argument("--train-file", type=Path, default=DEFAULT_TRAIN_PATH)
    args = parser.parse_args(argv)

    run_dir = E2E_ROOT / f"semantic-{time.time_ns()}"
    local_checks = _bounded_local_checks()
    issues: list[str] = []

    for label, path, digest in (
        ("policy", args.policy_file, args.policy_sha256),
        ("vectors_manifest", args.vectors_manifest, args.vectors_manifest_sha256),
    ):
        guard = path_guard_issue(path)
        if guard:
            issues.append(f"{label}:{guard}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            issues.append(f"{label}_sha_format_invalid")

    train_rows, train_issues = _verify_train(args.train_file)
    issues.extend(train_issues)

    policy: dict[str, Any] = {}
    policy_issues: list[str] = []
    lineage: list[dict[str, Any]] = []
    vectors: dict[int, tuple[float, ...]] = {}
    manifest_issues: list[str] = []
    frozen_ready = False
    threshold: float | None = None

    if not issues:
        policy, policy_issues = _verify_policy(args.policy_file, args.policy_sha256)
        issues.extend(policy_issues)
        if policy and not policy_issues:
            _, lineage, vectors, manifest_issues = _verify_vectors_manifest(
                args.vectors_manifest,
                args.vectors_manifest_sha256,
                policy,
                train_rows=train_rows,
            )
            issues.extend(manifest_issues)
            if lineage and vectors and not manifest_issues:
                frozen_ready = True
                threshold = _policy_threshold(policy)

    core_matches = bool(local_checks.get("core_space_matches_transport"))
    if frozen_ready and not core_matches:
        issues.append("core_space_mismatch")

    if issues or not frozen_ready or threshold is None:
        invalid_markers = ("_mismatch", "_invalid", "forbidden_", "conflicting_", "core_space_mismatch")
        non_frozen_markers = ("not_frozen", "missing", "missing_file", "frozen_inputs_incomplete")
        has_invalid = any(any(marker in issue for marker in invalid_markers) for issue in issues)
        has_non_frozen = any(any(marker in issue for marker in non_frozen_markers) for issue in issues) or not frozen_ready
        status = "INPUT_INVALID" if has_invalid else "LOCAL_CHECK_ONLY" if has_non_frozen else "INPUT_INVALID"
        receipt = {
            "status": status,
            "semantic_ran": False,
            "blind_validation_ran": False,
            "semantic_acceptance_claim": False,
            "reason": issues[0] if issues else "frozen_inputs_incomplete",
            "issues": issues,
            "local_checks": local_checks,
            "train_path": str(args.train_file),
            "train_sha256_expected": TRAIN_SHA256,
            "transport_space_id": TRANSPORT_SPACE_ID,
            "run_dir": str(run_dir),
            "elapsed_seconds": round(time.perf_counter() - _START, 6),
        }
        _write_receipt(run_dir, receipt)
        print(json.dumps(_stdout_summary(receipt), ensure_ascii=False, sort_keys=True))
        return 0 if status == "LOCAL_CHECK_ONLY" else 2

    try:
        semantic = _run_semantic_slice(
            run_dir=run_dir,
            policy=policy,
            lineage=lineage,
            vectors=vectors,
            rows=train_rows,
            threshold=threshold,
            transport_space_id=TRANSPORT_SPACE_ID,
            eligibility_negative_ids=frozenset({"unit-021"}),
        )
        receipt = {
            "status": semantic["status"],
            "semantic_ran": True,
            "blind_validation_ran": False,
            "semantic_acceptance_claim": False,
            "scope": "TRAIN_ONLY",
            "policy_file": str(args.policy_file),
            "vectors_manifest": str(args.vectors_manifest),
            "positive_recall": semantic["metrics"]["positive_recall"],
            "negative_rejection": semantic["metrics"]["negative_rejection"],
            "semantic_negative_rejection": semantic["metrics"]["semantic_negative_rejection_excluding_hard_identifier"],
            "zero_overlap": semantic["metrics"]["zero_overlap"],
            "lexical_defect_count": len(semantic["lexical_below_threshold_one_term_defects"]),
            "native_search_calls": semantic["native_evidence"]["search_call_count"],
            "run_dir": str(run_dir),
            "elapsed_seconds": semantic["elapsed_seconds"],
        }
        _write_receipt(run_dir, receipt)
        print(json.dumps(_stdout_summary(receipt), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        failure = {
            "status": "FAIL",
            "semantic_ran": False,
            "blind_validation_ran": False,
            "semantic_acceptance_claim": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "run_dir": str(run_dir),
            "elapsed_seconds": round(time.perf_counter() - _START, 6),
        }
        _write_receipt(run_dir, failure)
        E2E_ROOT.mkdir(parents=True, exist_ok=True)
        (E2E_ROOT / "last-failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(_stdout_summary(failure), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
