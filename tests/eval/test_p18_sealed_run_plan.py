from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from p18_score_report import ALLOCATION_SEED, IDENTITY_MAP_SHA256, authoritative_query_pair_indices, frozen_public_identity_map
from p18_sealed_run_plan import SealedPlanError, _select_query_pairs, build_units_from_rows


PUBLIC_FIXTURE = Path(__file__).with_name("public_fixture.jsonl")


def _public_rows() -> list[dict]:
    source_rows = [json.loads(line) for line in PUBLIC_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows: list[dict] = []
    for pair_index in range(1, 121):
        for condition in (1, 2):
            source = source_rows[(pair_index + condition) % len(source_rows)]
            history = [
                {
                    "event_id": f"TEST-public-{pair_index}-{condition}-1",
                    "sequence": 1,
                    "source_type": event["source_type"],
                    "speaker_role": event["speaker_role"],
                    "text": event["text"],
                    "occurred_at": event.get("occurred_at", "2026-01-01T00:00:00Z"),
                }
                for event in source["history"]
            ]
            rows.append({
                "dataset_id": "PUBLIC-TEST",
                "record_key": f"TEST-public-record-{pair_index}-{condition}",
                "pair_id": f"TEST-pair-{pair_index}",
                "history": history,
                "query": {
                    "clean_session": True,
                    "query_id": f"TEST-query-{pair_index}-{condition}",
                    "session_id": f"TEST-session-{pair_index}-{condition}",
                    "text": source["query"]["text"],
                },
                "simulation": True,
            })
    return rows


def test_public_fixture_builds_full_core_and_fixed_host_units() -> None:
    plan = build_units_from_rows(_public_rows())
    assert plan["allocation"]["status"] == "READY_FOR_FORMAL_FREEZE"
    assert plan["allocation"]["core_conditions_all_arms"] == 960
    assert plan["allocation"]["query_conditions_per_host_arm"] == 80
    assert plan["allocation"]["journey_operations_per_host_arm"] == 48
    assert len(plan["core_by_arm"]) == 960
    assert len(plan["entries"]) == 8
    assert all(entry["unit_count"] == 128 for entry in plan["entries"])
    assert all(entry["query_unit_count"] == 80 for entry in plan["entries"])
    assert all(entry["journey_operation_count"] == 48 for entry in plan["entries"])
    for entry in plan["entries"]:
        query_units = [unit for unit in entry["units"] if unit["kind"] == "host_query"]
        journey_units = [unit for unit in entry["units"] if unit["kind"] == "journey_operation"]
        assert all(set(unit["model_input"]) == {"history", "query"} for unit in query_units)
        assert all(set(unit["model_input"]["query"]) == {"text"} for unit in query_units)
        assert all(
            (set(unit["model_input"]) == {"source_events"}
             if unit["operation_kind"] == "source_capture"
             else set(unit["model_input"]) == ({"query"} if unit["operation_kind"] == "host_turn" else set()))
            for unit in journey_units
        )


def test_public_plan_does_not_accept_gold_named_output(tmp_path: Path) -> None:
    with pytest.raises(SealedPlanError, match="unsafe_output_root"):
        from p18_sealed_run_plan import _safe_output_root

        _safe_output_root(tmp_path / "gold-output")


def _stratum_metadata(prefix: str, *, padded: bool) -> dict[str, dict[str, str]]:
    metadata = {}
    classes = ("positive", "positive", "positive", "positive", "negative", "negative", "negative", "ambiguous", "ambiguous", "ambiguous")
    for index in range(1, 121):
        group = f"B{(index - 1) // 10 + 1:02d}"
        pair_id = f"{prefix}{index:03d}" if padded else f"{prefix}{index}"
        metadata[pair_id] = {"pair_id": pair_id, "group_id": group, "core_class": classes[(index - 1) % 10]}
    return metadata


def _legacy_pair_id_hash_select(metadata: dict[str, dict[str, str]]) -> list[str]:
    groups = tuple(f"B{i:02d}" for i in range(1, 13))
    classes = ("positive", "negative", "ambiguous")
    extra = {"B01", "B04", "B07", "B10"}
    selected: list[str] = []
    for group in groups:
        for class_name in classes:
            candidates = [item for item in metadata.values() if item["group_id"] == group and item["core_class"] == class_name]
            candidates.sort(key=lambda item: (hashlib.sha256((ALLOCATION_SEED + "\n" + item["pair_id"]).encode("utf-8")).hexdigest(), item["pair_id"]))
            required = 2 if class_name == "positive" and group in extra else 1
            selected.extend(item["pair_id"] for item in candidates[:required])
    return selected


def test_host_selection_is_metadata_driven_and_stable() -> None:
    metadata = _stratum_metadata("TEST-pair-", padded=False)
    expected = _select_query_pairs(metadata)
    shuffled = dict(reversed(list(metadata.items())))
    assert _select_query_pairs(shuffled) == expected
    assert len(expected) == 40
    assert [int(pair_id.rsplit("-", 1)[1]) for pair_id in expected] == list(authoritative_query_pair_indices())


def test_query_selection_follows_identity_map_not_dataset_pair_hash() -> None:
    metadata = _stratum_metadata("P", padded=True)
    selected = _select_query_pairs(metadata)
    assert [int(pair_id[1:]) for pair_id in selected] == list(authoritative_query_pair_indices())
    legacy = _legacy_pair_id_hash_select(metadata)
    assert selected != legacy
    padded = _select_query_pairs(_stratum_metadata("TEST-pair-", padded=True))
    assert [int(pair_id.rsplit("-", 1)[1]) for pair_id in padded] == list(authoritative_query_pair_indices())


def test_query_selection_rejects_pair_ids_without_public_index() -> None:
    metadata = _stratum_metadata("P", padded=True)
    metadata["opaque-pair"] = {"pair_id": "opaque-pair", "group_id": "B01", "core_class": "positive"}
    with pytest.raises(SealedPlanError, match="pair_id_public_index_required"):
        _select_query_pairs(metadata)


def test_host_query_ordinals_follow_sorted_identity_map() -> None:
    plan = build_units_from_rows(_public_rows())
    first_index = authoritative_query_pair_indices()[0]
    query_01 = next(unit for unit in plan["entries"][0]["units"] if unit["unit_id"].endswith("-query-01-c1"))
    association = next(item for item in plan["associations"] if item["unit_id"] == query_01["unit_id"])
    assert association["pair_id"] == f"TEST-pair-{first_index}"
    assert plan["allocation"]["identity_map_sha256"] == IDENTITY_MAP_SHA256
    assert plan["allocation"]["rule"] == "frozen_public_identity_map_sorted_40_pair_host_subset"
    assert plan["allocation"]["public_query_pair_ids"] == frozen_public_identity_map()["query_pair_ids"]
