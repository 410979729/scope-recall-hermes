from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

try:
    from .doctor_common import plugin_yaml_version, read_text
except ImportError:  # pragma: no cover - direct source-script execution fallback
    from doctor_common import plugin_yaml_version, read_text

def source_report(source_root: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    failures: list[str] = []
    recommendations: list[str] = []

    pyproject_path = source_root / "pyproject.toml"
    plugin_path = source_root / "plugin.yaml"
    readme_path = source_root / "README.md"
    changelog_path = source_root / "CHANGELOG.md"

    pyproject_version = ""
    plugin_version = ""
    readme_versions: list[str] = []
    changelog_has_version = False

    try:
        pyproject_version = tomllib.loads(read_text(pyproject_path))["project"]["version"]
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read pyproject version: {exc}")

    try:
        plugin_version = plugin_yaml_version(read_text(plugin_path))
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read plugin.yaml version: {exc}")

    try:
        readme_versions = re.findall(r"Version `([^`]+)`", read_text(readme_path))
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read README public version: {exc}")

    try:
        changelog_has_version = f"## [{pyproject_version}]" in read_text(changelog_path)
    except Exception as exc:  # pragma: no cover - defensive reporting
        failures.append(f"cannot read CHANGELOG version section: {exc}")

    if pyproject_version and plugin_version and pyproject_version != plugin_version:
        failures.append(f"pyproject/plugin version mismatch: {pyproject_version} != {plugin_version}")
    if pyproject_version and readme_versions != [pyproject_version]:
        failures.append(f"README public versions {readme_versions!r} do not match {pyproject_version}")
    if pyproject_version and not changelog_has_version:
        failures.append(f"CHANGELOG is missing ## [{pyproject_version}] section")

    if failures:
        recommendations.append("Align pyproject.toml, plugin.yaml, README.md, and CHANGELOG.md before release.")

    source = {
        "root": str(source_root),
        "pyproject_version": pyproject_version,
        "plugin_version": plugin_version,
        "readme_public_versions": readme_versions,
        "changelog_has_version": changelog_has_version,
    }
    check = {"ok": not failures, "failures": failures}
    return source, check, recommendations
