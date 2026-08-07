"""Runtime configuration loading diagnostics."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scope_recall.config import (
    DEFAULT_CONFIG,
    load_runtime_config,
    load_runtime_config_errors,
    save_runtime_config,
)
from scope_recall.doctor_common import load_runtime_config as doctor_load_runtime_config
from scope_recall.memory_ops import (
    _relation_local_neighbor_limit,
    _relation_pair_budget,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _path_tail(path: str) -> tuple[str, ...]:
    return tuple(str(path).replace("\\", "/").split("/")[-2:])


def _leaf_items(value: object, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], object]]:
    if isinstance(value, dict):
        items: list[tuple[tuple[str, ...], object]] = []
        for key, child in value.items():
            items.extend(_leaf_items(child, (*prefix, str(key))))
        return items
    return [(prefix, value)]


def _nested_override(path: tuple[str, ...], value: object) -> dict[str, object]:
    assert path
    result: dict[str, object] = {}
    cursor = result
    for key in path[:-1]:
        child: dict[str, object] = {}
        cursor[key] = child
        cursor = child
    cursor[path[-1]] = value
    return result


def _dotted_value(config: dict[str, object], path: tuple[str, ...]) -> object:
    value: object = config
    for key in path:
        assert isinstance(value, dict)
        value = value[key]
    return value


def test_runtime_config_rejects_unknown_and_invalid_typed_overrides(tmp_path: Path):
    plugin_dir = tmp_path / "plugin"
    storage_dir = tmp_path / "scope-recall"
    plugin_dir.mkdir()
    storage_dir.mkdir()
    (plugin_dir / "config.json").write_text(
        json.dumps(
            {
                "journal": {"max_entries_per_digest": 500},
                "retrieval": {"min_score": 0.18},
            }
        ),
        encoding="utf-8",
    )
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "journal": {
                    "max_entries_per_digets": 999,
                    "max_entries_per_digest": "999",
                },
                "retrival": {"min_score": 0.99},
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(plugin_dir, storage_dir)
    errors = load_runtime_config_errors(config)

    assert config["journal"]["max_entries_per_digest"] == 500
    assert "max_entries_per_digets" not in config["journal"]
    assert "retrival" not in config
    assert {(item["kind"], item["message"]) for item in errors} == {
        ("unknown_key", "unknown config key: journal.max_entries_per_digets"),
        ("invalid_type", "invalid type for journal.max_entries_per_digest: expected integer, got string"),
        ("unknown_key", "unknown config key: retrival"),
    }


def test_runtime_config_accepts_promoted_digest_lifecycle_and_rejects_unknown_safely(
    tmp_path: Path,
):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    config_path = storage_dir / "config.json"
    config_path.write_text(
        json.dumps({"automatic_digest_default_lifecycle": "auto-activate"}),
        encoding="utf-8",
    )

    invalid = load_runtime_config(PLUGIN_ROOT, storage_dir)
    errors = load_runtime_config_errors(invalid)

    assert invalid["automatic_digest_default_lifecycle"] == "candidate"
    assert [item["kind"] for item in errors] == ["invalid_value"]
    assert "auto-activate" not in errors[0]["message"]

    config_path.write_text(
        json.dumps({"automatic_digest_default_lifecycle": "promoted"}),
        encoding="utf-8",
    )
    promoted = load_runtime_config(PLUGIN_ROOT, storage_dir)

    assert promoted["automatic_digest_default_lifecycle"] == "promoted"
    assert load_runtime_config_errors(promoted) == []


def test_doctor_runtime_config_rejects_unknown_and_invalid_typed_overrides(tmp_path: Path):
    source_root = tmp_path / "source"
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    source_root.mkdir()
    storage_dir.mkdir(parents=True)
    (source_root / "config.json").write_text(
        json.dumps({"journal": {"max_entries_per_digest": 500}}),
        encoding="utf-8",
    )
    (storage_dir / "config.json").write_text(
        json.dumps({"journal": {"max_entries_per_digets": 999, "max_entries_per_digest": "999"}}),
        encoding="utf-8",
    )

    config = doctor_load_runtime_config(source_root, hermes_home)
    errors = config.get("_config_load_errors")

    assert config["journal"]["max_entries_per_digest"] == 500
    assert "max_entries_per_digets" not in config["journal"]
    assert isinstance(errors, list)
    assert {item["kind"] for item in errors} == {"unknown_key", "invalid_type"}


def test_runtime_and_doctor_accept_explicit_journal_compatibility_keys(tmp_path: Path):
    source_root = tmp_path / "source"
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    source_root.mkdir()
    storage_dir.mkdir(parents=True)
    (source_root / "config.json").write_text(
        json.dumps({"journal": {"enabled": True}}),
        encoding="utf-8",
    )
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "journal": {
                    "llm_max_attempts": 2,
                    "llm_retry_attempts": 4,
                    "timeout": 12.0,
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = load_runtime_config(source_root, storage_dir)
    doctor = doctor_load_runtime_config(source_root, hermes_home)

    assert load_runtime_config_errors(runtime) == []
    assert doctor.get("_config_load_errors") is None
    assert runtime["journal"]["llm_max_attempts"] == 2
    assert runtime["journal"]["llm_retry_attempts"] == 4
    assert runtime["journal"]["timeout"] == 12.0


def test_runtime_and_doctor_accept_explicit_retrieval_default_keys(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "retrieval": {
                    "general_min_importance": 0.31,
                    "entity_scope_filter_enabled": False,
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)
    doctor = doctor_load_runtime_config(PLUGIN_ROOT, hermes_home)

    assert load_runtime_config_errors(runtime) == []
    assert doctor.get("_config_load_errors") is None
    assert runtime["retrieval"]["general_min_importance"] == 0.31
    assert doctor["retrieval"]["general_min_importance"] == 0.31
    assert runtime["retrieval"]["entity_scope_filter_enabled"] is False
    assert doctor["retrieval"]["entity_scope_filter_enabled"] is False


def test_runtime_and_doctor_accept_explicit_chat_alias_map(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "identity": {
                    "cross_platform_shared_scope": True,
                    "chat_aliases": {"telegram:synthetic-chat": "joy"},
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)
    doctor = doctor_load_runtime_config(PLUGIN_ROOT, hermes_home)

    assert load_runtime_config_errors(runtime) == []
    assert doctor.get("_config_load_errors") is None
    assert runtime["identity"]["chat_aliases"] == {
        "telegram:synthetic-chat": "joy"
    }
    assert doctor["identity"]["chat_aliases"] == {
        "telegram:synthetic-chat": "joy"
    }


def test_runtime_config_validates_embedding_connection_retry_delays(tmp_path: Path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    config_path = storage_dir / "config.json"
    config_path.write_text(
        json.dumps({"vector": {"embedder": {"connection_retry_delays": [0.5, 2]}}}),
        encoding="utf-8",
    )

    accepted = load_runtime_config(PLUGIN_ROOT, storage_dir)
    assert load_runtime_config_errors(accepted) == []
    assert accepted["vector"]["embedder"]["connection_retry_delays"] == [0.5, 2]

    config_path.write_text(
        json.dumps({"vector": {"embedder": {"connection_retry_delays": [-1]}}}),
        encoding="utf-8",
    )
    rejected = load_runtime_config(PLUGIN_ROOT, storage_dir)
    errors = load_runtime_config_errors(rejected)

    assert rejected["vector"]["embedder"]["connection_retry_delays"] == [
        2.0,
        4.0,
        8.0,
    ]
    assert [(item["kind"], item["message"]) for item in errors] == [
        (
            "invalid_value",
            "invalid value for vector.embedder.connection_retry_delays[0]: "
            "expected a finite number between 0 and 300",
        )
    ]


def _direct_runtime_config_reads() -> dict[str, set[str]]:
    """Collect literal top-level reads from direct ``_config`` access."""

    def is_runtime_config(receiver: ast.expr) -> bool:
        return (
            isinstance(receiver, ast.Attribute) and receiver.attr == "_config"
        ) or (isinstance(receiver, ast.Name) and receiver.id == "_config")

    reads: dict[str, set[str]] = {}
    for source_path in PLUGIN_ROOT.rglob("*.py"):
        relative = source_path.relative_to(PLUGIN_ROOT)
        if (
            "tests" in relative.parts
            or any(part.startswith(".") for part in relative.parts)
            or any(part in {"build", "dist", "__pycache__"} for part in relative.parts)
        ):
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            key: str | None = None
            if isinstance(node, ast.Call):
                function = node.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and is_runtime_config(function.value)
                ):
                    key = node.args[0].value
            elif (
                isinstance(node, ast.Subscript)
                and is_runtime_config(node.value)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                key = node.slice.value
            if key is not None:
                reads.setdefault(key, set()).add(
                    f"{relative.as_posix()}:{getattr(node, 'lineno', 0)}"
                )
    return reads


def test_direct_runtime_config_reads_are_declared_in_canonical_schema():
    reads = _direct_runtime_config_reads()
    packaged_defaults = json.loads(
        (PLUGIN_ROOT / "config.json").read_text(encoding="utf-8")
    )

    missing_from_runtime = sorted(set(reads) - set(DEFAULT_CONFIG))
    missing_from_package = sorted(set(reads) - set(packaged_defaults))

    assert missing_from_runtime == [], {
        key: sorted(reads[key]) for key in missing_from_runtime
    }
    assert missing_from_package == [], {
        key: sorted(reads[key]) for key in missing_from_package
    }


def test_relation_runtime_config_accepts_canonical_bounded_overrides(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    override = {
        "relation_extraction_enabled": False,
        "relation_extraction_max_pairs": 5000,
        "relation_sync_neighbor_limit": 256,
        "relation_rebuild_chunk_pairs": 1000,
    }
    (storage_dir / "config.json").write_text(
        json.dumps(override), encoding="utf-8"
    )

    runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)
    doctor = doctor_load_runtime_config(PLUGIN_ROOT, hermes_home)

    assert load_runtime_config_errors(runtime) == []
    assert doctor.get("_config_load_errors") is None
    for key, value in override.items():
        assert runtime[key] == value
        assert doctor[key] == value


def test_runtime_config_normalizes_supported_boolean_aliases(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    aliases = {
        "false": False,
        "0": False,
        "no": False,
        "off": False,
        " TRUE ": True,
        "1": True,
        "yes": True,
        "on": True,
    }

    for raw, expected in aliases.items():
        (storage_dir / "config.json").write_text(
            json.dumps({"relation_extraction_enabled": raw}), encoding="utf-8"
        )
        runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)
        doctor = doctor_load_runtime_config(PLUGIN_ROOT, hermes_home)

        assert load_runtime_config_errors(runtime) == []
        assert doctor.get("_config_load_errors") is None
        assert runtime["relation_extraction_enabled"] is expected
        assert doctor["relation_extraction_enabled"] is expected


def test_vector_outbox_retention_config_is_bounded_and_packaged_consistently(
    tmp_path: Path,
):
    plugin_dir = tmp_path / "plugin"
    storage_dir = tmp_path / "scope-recall"
    plugin_dir.mkdir()
    storage_dir.mkdir()
    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "outbox_completed_retention_days": -1,
                    "outbox_completed_keep_per_generation": 1_000_001,
                }
            }
        ),
        encoding="utf-8",
    )

    rejected = load_runtime_config(plugin_dir, storage_dir)
    errors = load_runtime_config_errors(rejected)

    assert rejected["vector"]["outbox_completed_retention_days"] == 30
    assert rejected["vector"]["outbox_completed_keep_per_generation"] == 5000
    assert [item["kind"] for item in errors] == ["invalid_value", "invalid_value"]

    (storage_dir / "config.json").write_text(
        json.dumps(
            {
                "vector": {
                    "outbox_completed_retention_days": 0,
                    "outbox_completed_keep_per_generation": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    disabled = load_runtime_config(plugin_dir, storage_dir)
    assert load_runtime_config_errors(disabled) == []
    assert disabled["vector"]["outbox_completed_retention_days"] == 0
    assert disabled["vector"]["outbox_completed_keep_per_generation"] == 0

    packaged = json.loads((PLUGIN_ROOT / "config.json").read_text(encoding="utf-8"))
    assert packaged["vector"]["outbox_completed_retention_days"] == DEFAULT_CONFIG[
        "vector"
    ]["outbox_completed_retention_days"]
    assert packaged["vector"]["outbox_completed_keep_per_generation"] == DEFAULT_CONFIG[
        "vector"
    ]["outbox_completed_keep_per_generation"]


def test_runtime_config_rejects_malformed_identity_alias_entries(tmp_path: Path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    malformed = [
        {"chat_aliases": {"telegram:": "owner"}},
        {"chat_aliases": {":synthetic-chat": "owner"}},
        {"chat_aliases": {"telegram:synthetic-chat": ""}},
        {"chat_aliases": {"telegram:synthetic-chat": {"owner": True}}},
        {"user_aliases": {"telegram:": "owner"}},
    ]

    for identity in malformed:
        (storage_dir / "config.json").write_text(
            json.dumps({"identity": identity}), encoding="utf-8"
        )
        runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)
        errors = load_runtime_config_errors(runtime)

        assert runtime["identity"].get("chat_aliases", {}) == {}
        assert runtime["identity"].get("user_aliases", {}) == {}
        assert len(errors) == 1
        assert errors[0]["kind"] == "invalid_value"
        assert "identity alias" in errors[0]["message"]


def test_runtime_config_accepts_nonempty_identity_alias_entries(tmp_path: Path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    aliases = {
        "chat_aliases": {"telegram:synthetic-chat": "chat-owner"},
        "user_aliases": {"telegram:synthetic-user": "account-owner"},
    }
    (storage_dir / "config.json").write_text(
        json.dumps({"identity": aliases}), encoding="utf-8"
    )

    runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)

    assert load_runtime_config_errors(runtime) == []
    assert runtime["identity"]["chat_aliases"] == aliases["chat_aliases"]
    assert runtime["identity"]["user_aliases"] == aliases["user_aliases"]


def test_relation_runtime_config_rejects_out_of_range_overrides(tmp_path: Path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(parents=True)
    cases = [
        ("relation_extraction_max_pairs", 0, "between 1 and 5000"),
        ("relation_extraction_max_pairs", 5001, "between 1 and 5000"),
        ("relation_sync_neighbor_limit", 0, "between 1 and 256"),
        ("relation_sync_neighbor_limit", 257, "between 1 and 256"),
        ("relation_rebuild_chunk_pairs", 0, "between 1 and 1000"),
        ("relation_rebuild_chunk_pairs", 1001, "between 1 and 1000"),
    ]

    for key, value, expected_message in cases:
        (storage_dir / "config.json").write_text(
            json.dumps({key: value}), encoding="utf-8"
        )
        config = load_runtime_config(PLUGIN_ROOT, storage_dir)
        errors = load_runtime_config_errors(config)

        assert config[key] == DEFAULT_CONFIG[key]
        assert [(item["kind"], item["message"]) for item in errors] == [
            ("invalid_value", f"invalid value for {key}: expected an integer {expected_message}")
        ]


def test_relation_budget_helpers_fail_closed_for_internal_config_bypass():
    provider = SimpleNamespace(
        _config={
            "relation_extraction_max_pairs": 500_000,
            "relation_sync_neighbor_limit": 500_000,
        }
    )

    assert _relation_pair_budget(provider) == 5000
    assert _relation_local_neighbor_limit(provider) == 256

    provider._config = {
        "relation_extraction_max_pairs": -10,
        "relation_sync_neighbor_limit": -10,
    }
    assert _relation_pair_budget(provider) == 1
    assert _relation_local_neighbor_limit(provider) == 1


def test_runtime_and_doctor_share_contract_for_every_default_leaf(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    config_path = storage_dir / "config.json"

    for path, expected in _leaf_items(DEFAULT_CONFIG):
        config_path.write_text(
            json.dumps(_nested_override(path, expected)),
            encoding="utf-8",
        )
        runtime = load_runtime_config(PLUGIN_ROOT, storage_dir)
        doctor = doctor_load_runtime_config(PLUGIN_ROOT, hermes_home)

        assert load_runtime_config_errors(runtime) == [], ".".join(path)
        assert doctor.get("_config_load_errors") is None, ".".join(path)
        assert _dotted_value(runtime, path) == expected, ".".join(path)
        assert _dotted_value(doctor, path) == expected, ".".join(path)


def test_load_runtime_config_reports_malformed_storage_json(tmp_path: Path):
    plugin_dir = tmp_path / "plugin"
    storage_dir = tmp_path / "scope-recall"
    plugin_dir.mkdir()
    storage_dir.mkdir()
    (plugin_dir / "config.json").write_text(json.dumps({"auto_recall": False}), encoding="utf-8")
    (storage_dir / "config.json").write_text('{"vector": ', encoding="utf-8")

    config = load_runtime_config(plugin_dir, storage_dir)
    errors = load_runtime_config_errors(config)

    assert config["auto_recall"] is False
    assert errors
    assert errors[0]["kind"] == "json_decode"
    assert _path_tail(errors[0]["path"]) == ("scope-recall", "config.json")
    assert "message" in errors[0]


def test_load_runtime_config_reports_non_dict_payload(tmp_path: Path):
    plugin_dir = tmp_path / "plugin"
    storage_dir = tmp_path / "scope-recall"
    plugin_dir.mkdir()
    storage_dir.mkdir()
    (plugin_dir / "config.json").write_text("[]", encoding="utf-8")

    config = load_runtime_config(plugin_dir, storage_dir)
    errors = load_runtime_config_errors(config)

    assert errors == [
        {
            "path": str(plugin_dir / "config.json"),
            "kind": "non_dict_payload",
            "message": "config payload must be a JSON object",
        }
    ]


def test_doctor_runtime_config_surfaces_config_load_errors(tmp_path: Path):
    source_root = tmp_path / "source"
    hermes_home = tmp_path / "home"
    source_root.mkdir()
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (source_root / "config.json").write_text(json.dumps({"vector": {"enabled": False}}), encoding="utf-8")
    (storage_dir / "config.json").write_text("{bad-json", encoding="utf-8")

    config = doctor_load_runtime_config(source_root, hermes_home)

    errors = config.get("_config_load_errors")
    assert isinstance(errors, list)
    assert errors[0]["kind"] == "json_decode"
    assert _path_tail(errors[0]["path"]) == ("scope-recall", "config.json")


def test_save_runtime_config_does_not_persist_internal_diagnostics(tmp_path: Path):
    hermes_home = tmp_path / "home"

    save_runtime_config(
        {
            "_config_load_errors": [{"path": "leak", "kind": "synthetic", "message": "should-not-persist"}],
            "_runtime_state.enabled": True,
            "vector.enabled": False,
            "vector._internal_note": "drop me",
        },
        str(hermes_home),
    )

    persisted = json.loads((hermes_home / "scope-recall" / "config.json").read_text(encoding="utf-8"))

    assert "_config_load_errors" not in persisted
    assert "_runtime_state" not in persisted
    assert persisted["vector"]["enabled"] is False
    assert "_internal_note" not in persisted["vector"]


def test_save_runtime_config_persists_only_the_operator_overlay(tmp_path: Path):
    hermes_home = tmp_path / "home"
    packaged_journal = load_runtime_config(
        PLUGIN_ROOT, tmp_path / "packaged-only"
    )["journal"]

    save_runtime_config({"retrieval.top_k": 17}, str(hermes_home))

    storage_dir = hermes_home / "scope-recall"
    persisted = json.loads((storage_dir / "config.json").read_text(encoding="utf-8"))
    assert persisted == {"retrieval": {"top_k": 17}}

    resolved = load_runtime_config(PLUGIN_ROOT, storage_dir)
    assert resolved["retrieval"]["top_k"] == 17
    assert resolved["journal"] == packaged_journal


def test_save_runtime_config_preserves_real_overrides_and_prunes_old_pinned_defaults(
    tmp_path: Path,
):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    config_path = storage_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "auto_recall": False,
                "retrieval": {"top_k": DEFAULT_CONFIG["retrieval"]["top_k"]},
                "journal": {
                    "digest_interval_hours": DEFAULT_CONFIG["journal"][
                        "digest_interval_hours"
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    save_runtime_config({"retrieval.top_k": 17}, str(hermes_home))

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted == {
        "auto_recall": False,
        "retrieval": {"top_k": 17},
    }


def test_save_runtime_config_refuses_to_overwrite_malformed_existing_overlay(
    tmp_path: Path,
):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    config_path = storage_dir / "config.json"
    malformed = '{"retrieval": '
    config_path.write_text(malformed, encoding="utf-8")

    with pytest.raises(ValueError, match="existing runtime config is unreadable"):
        save_runtime_config({"retrieval.top_k": 17}, str(hermes_home))

    assert config_path.read_text(encoding="utf-8") == malformed


def test_doctor_cli_reports_config_load_errors(tmp_path: Path):
    hermes_home = tmp_path / "home"
    storage_dir = hermes_home / "scope-recall"
    storage_dir.mkdir(parents=True)
    (storage_dir / "config.json").write_text("{bad-json", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "doctor.py"),
            "--source-root",
            str(PLUGIN_ROOT),
            "--hermes-home",
            str(hermes_home),
            "--json",
        ],
        cwd=PLUGIN_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["checks"]["config_load"]["ok"] is False
    assert payload["runtime"]["config_load"]["errors"][0]["kind"] == "json_decode"
    assert any("config" in item.lower() for item in payload["recommendations"])
