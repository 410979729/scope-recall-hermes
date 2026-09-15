from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest

from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.composition import CoreConfig, MemoryCore
from p18_history_loader import (
    PUBLIC_SYNTHETIC_HISTORY,
    HistoryLoaderError,
    build_public_manifest,
    history_event_dtos,
    load_raw_history,
)


class _FixedClock:
    def __init__(self) -> None:
        self.tick = 0

    def utc_now(self) -> str:
        self.tick += 1
        return f"2026-09-06T13:00:{self.tick:02d}Z"

    def monotonic(self) -> float:
        self.tick += 1
        return float(self.tick)


def _core(tmp_path: Path) -> tuple[MemoryCore, TrustedContext]:
    binding = InstanceBinding(
        "P18-loader-agent",
        "P18-loader-installation",
        (tmp_path / "core").resolve(),
        frozenset({"P18-scope"}),
        True,
    )
    core = MemoryCore(CoreConfig(binding), clock=_FixedClock())
    core.initialize()
    context = TrustedContext(binding, "P18-loader-session", frozenset({"P18-scope"}), "human_direct")
    return core, context


def _counts(core: MemoryCore, context: TrustedContext) -> tuple[int, int, int, int]:
    with core.storage.read(context) as tx:
        conn = tx._check()
        return tuple(
            int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("source_events", "claims", "claim_versions", "work_items")
        )


def test_public_manifest_generates_imported_dtos_without_claims() -> None:
    manifest = build_public_manifest()
    dtos = history_event_dtos(manifest)
    assert len(dtos) == len(PUBLIC_SYNTHETIC_HISTORY) == 2
    assert [item.order for item in dtos] == [1, 2]
    assert all(item.event["origin"] == "imported" for item in dtos)
    assert all(item.event["dataset_id"] == "P18-SYNTHETIC-PUBLIC" for item in dtos)
    assert all(len(item.fingerprint) == 64 for item in dtos)


def test_hash_mismatch_and_control_field_leakage_are_rejected() -> None:
    manifest = build_public_manifest()
    bad_hash = dict(manifest, manifest_sha256="0" * 64)
    with pytest.raises(HistoryLoaderError, match="manifest hash mismatch"):
        history_event_dtos(bad_hash)

    bad_control = copy.deepcopy(manifest)
    bad_control["records"][0]["gold"] = "forbidden"
    with pytest.raises(HistoryLoaderError, match="control/oracle"):
        history_event_dtos(bad_control)


def test_unknown_source_origin_is_rejected_and_never_defaults_to_human() -> None:
    bad = copy.deepcopy(build_public_manifest())
    bad["records"][0]["source_original_origin"] = None
    with pytest.raises(HistoryLoaderError, match="source_original_origin"):
        history_event_dtos(bad)
    explicit_unknown = copy.deepcopy(PUBLIC_SYNTHETIC_HISTORY)
    explicit_unknown[0]["source_original_origin"] = "origin_unknown"
    dtos = history_event_dtos(build_public_manifest(explicit_unknown))
    assert dtos[0].event["source_original_origin"] == "origin_unknown"


def test_only_c_arm_imports_and_normal_core_capture_queues_once(tmp_path: Path) -> None:
    core, context = _core(tmp_path)
    manifest = build_public_manifest()
    unsupported = load_raw_history(core, context, manifest, scope_id="P18-scope", arm_id="A")
    assert unsupported.status == "UNSUPPORTED"
    assert unsupported.reason == "raw_history_core_import_only"
    assert _counts(core, context) == (0, 0, 0, 0)

    first = load_raw_history(core, context, manifest, scope_id="P18-scope", arm_id="C")
    assert first.status == "PASS"
    assert first.records_seen == 2
    assert first.inserted == 2
    assert first.duplicates == 0
    assert first.queued_work_items == 4
    assert len(first.source_refs) == 2
    assert _counts(core, context) == (2, 0, 0, 4)

    second = load_raw_history(core, context, manifest, scope_id="P18-scope", arm_id="C")
    assert second.status == "PASS"
    assert second.inserted == 0
    assert second.duplicates == 2
    assert second.source_refs == first.source_refs
    assert second.queued_work_items == 4
    assert _counts(core, context) == (2, 0, 0, 4)


def test_non_test_binding_is_rejected_before_raw_import(tmp_path: Path) -> None:
    binding = InstanceBinding(
        "P18-production-shaped-agent",
        "P18-production-shaped-installation",
        (tmp_path / "core").resolve(),
        frozenset({"P18-scope"}),
        False,
    )
    core = MemoryCore(CoreConfig(binding), clock=_FixedClock())
    core.initialize()
    context = TrustedContext(binding, "P18-session", frozenset({"P18-scope"}), "human_direct")
    with pytest.raises(HistoryLoaderError, match="isolated test binding"):
        load_raw_history(core, context, build_public_manifest(), scope_id="P18-scope", arm_id="C")
    with sqlite3.connect(binding.data_directory / "memory.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
