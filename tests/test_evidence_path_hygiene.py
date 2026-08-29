"""Regression tests for decoded shareable-evidence private-path hygiene."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from scope_recall.scripts import evidence_path_hygiene as hygiene


ROOT = Path(__file__).resolve().parents[1]
HONESTY_SCRIPT = ROOT / "scripts" / "release_test_honesty.py"


def _honesty_module():
    spec = importlib.util.spec_from_file_location(
        "scope_recall_evidence_path_honesty_test",
        HONESTY_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _windows_path(drive: str, *parts: str, slash: str = "\\") -> str:
    return drive + ":" + slash + slash.join(parts)


def test_private_path_scan_detects_arbitrary_windows_drive_path():
    value = _windows_path("C", "x3", "pytest", "probe.db")
    assert hygiene.find_absolute_local_paths(value)


def test_private_path_scan_detects_forward_slash_drive_path():
    value = _windows_path("D", "build", "pytest", "probe.db", slash="/")
    assert hygiene.find_absolute_local_paths(value)


def test_private_path_redaction_consumes_spaced_windows_segments():
    reason = r"symlink at C:\Program Files\Scope Recall\target failed"

    redacted = hygiene.redact_absolute_local_paths(reason)

    assert redacted == "symlink at <isolated-path> failed"
    assert hygiene.find_absolute_local_paths(redacted) == []


def test_private_path_scan_detects_unc_path():
    value = "\\" * 2 + "server" + "\\" + "share" + "\\" + "probe.db"
    assert hygiene.find_absolute_local_paths(value)


def test_private_path_scan_detects_extended_length_windows_path():
    value = "\\" * 2 + "?" + "\\" + _windows_path("C", "long", "probe.db")
    assert hygiene.find_absolute_local_paths(value)


def test_private_path_scan_detects_json_escaped_absolute_path():
    value = _windows_path("C", "x3", "pytest", "probe.db")
    rendered = json.dumps({"reason": value})
    assert hygiene.private_path_match_count(rendered, decode_json=True) > 0


def test_private_path_scan_detects_posix_temp_and_home_paths():
    values = [
        "/" + "/".join(("tmp", "pytest", "probe.db")),
        "/" + "/".join(("home", "operator", "probe.db")),
        "/" + "/".join(("Users", "operator", "probe.db")),
        "/" + "/".join(("private", "var", "folders", "probe.db")),
    ]
    assert all(hygiene.find_absolute_local_paths(value) for value in values)


def test_private_path_scan_allows_relative_package_paths():
    assert hygiene.find_absolute_local_paths("relative/package/path") == []
    assert hygiene.find_absolute_local_paths("scope_recall/module.py") == []


def test_private_path_scan_allows_https_urls():
    assert hygiene.find_absolute_local_paths("https://example.test/path") == []


def test_shareable_skip_report_redacts_path_but_preserves_reason(tmp_path: Path):
    module = _honesty_module()
    local_path = _windows_path("C", "x3", "pytest", "probe.db")
    raw = {
        "schema_version": module.SCHEMA_VERSION,
        "skipped": [
            {
                "node_id": "tests/test_probe.py::test_symlink_privilege",
                "reason": f"symlink privilege unavailable at '{local_path}' (winerror=1314)",
            }
        ],
    }
    raw_path = tmp_path / "PYTEST_SKIP_REPORT.raw.json"
    shareable_path = tmp_path / "PYTEST_SKIP_REPORT.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")

    shareable = module.write_shareable_report(raw_path, shareable_path)

    persisted_raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert local_path in persisted_raw["skipped"][0]["reason"]
    reason = shareable["skipped"][0]["reason"]
    assert hygiene.REDACTION_MARKER in reason
    assert "symlink privilege unavailable" in reason
    assert "winerror=1314" in reason
    assert hygiene.find_absolute_local_paths(reason) == []
