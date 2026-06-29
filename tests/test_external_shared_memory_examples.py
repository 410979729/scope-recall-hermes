import importlib.util
import json
from pathlib import Path

import tomllib


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"
EXAMPLE_FILES = [
    "examples/external_bridge/import.jsonl",
    "examples/external_bridge/export.jsonl",
    "examples/external_bridge/conflict_resolution.jsonl",
]
REQUIRED_ROW_FIELDS = {
    "schema_version",
    "bridge_action",
    "record_id",
    "target",
    "memory_type",
    "content",
    "summary",
    "tenant_id",
    "external_user_ref",
    "agent_identity",
    "workspace_id",
    "entities",
    "tags",
    "source",
    "updated_at",
    "metadata",
}


def _load_release_check_module():
    spec = importlib.util.spec_from_file_location("scope_recall_check_release_external_bridge", CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    release_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_check)
    return release_check


def _jsonl_rows(path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert line.strip(), f"blank JSONL line in {path}:{line_no}"
        rows.append(json.loads(line))
    return rows


def test_external_shared_memory_docs_cover_bridge_examples():
    docs = (PLUGIN_ROOT / "docs" / "external-shared-memory.md").read_text(encoding="utf-8")

    for snippet in [
        "examples/external_bridge/import.jsonl",
        "examples/external_bridge/export.jsonl",
        "examples/external_bridge/conflict_resolution.jsonl",
        "schema_version",
        "bridge_action",
        "tenant_id",
        "external_user_ref",
        "redaction_policy",
        "central-backend-wins",
        "pseudonymous external user reference",
    ]:
        assert snippet in docs


def test_external_bridge_jsonl_examples_are_safe_and_packaged():
    pyproject = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["scope_recall"]
    manifest = (PLUGIN_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    release_check = _load_release_check_module()

    assert "examples/external_bridge/*.jsonl" in package_data
    assert "recursive-include examples *.jsonl" in manifest

    observed_actions: set[str] = set()
    for rel in EXAMPLE_FILES:
        path = PLUGIN_ROOT / rel
        assert path.is_file(), rel
        assert rel in release_check.REQUIRED_SOURCE_FILES
        assert f"scope_recall/{rel}" in release_check.REQUIRED_WHEEL

        rows = _jsonl_rows(path)
        assert rows, rel
        for row in rows:
            assert REQUIRED_ROW_FIELDS <= row.keys()
            assert row["schema_version"] == "scope-recall.external-memory.v1"
            assert row["bridge_action"] in {"import", "export", "conflict_resolution"}
            assert row["target"] in {"user", "memory", "project", "ops"}
            assert row["memory_type"] == row["target"]
            assert row["tenant_id"].startswith("tenant-demo-")
            assert row["external_user_ref"].startswith("user-demo-")
            assert row["agent_identity"].startswith("agent-demo-")
            assert row["workspace_id"].startswith("workspace-demo-")
            assert isinstance(row["entities"], list)
            assert isinstance(row["tags"], list)

            metadata = row["metadata"]
            identity = metadata["identity_safety"]
            redaction = metadata["redaction_policy"]
            assert identity["tenant_boundary"] == "tenant_id"
            assert identity["user_ref_policy"] == "pseudonymous external user reference"
            assert redaction["state"] in {"sanitized", "redacted"}
            assert redaction["contains_secret_like_values"] is False
            assert "/home/" not in json.dumps(row, ensure_ascii=False)
            assert "~/.hermes" not in json.dumps(row, ensure_ascii=False)
            observed_actions.add(row["bridge_action"])

    assert observed_actions == {"import", "export", "conflict_resolution"}
