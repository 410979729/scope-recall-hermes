"""Read-only installed artifact, requirement and version diagnostics.

Probe this file with the target interpreter: its metadata, marker environment
and installed versions must answer, not the doctor's own environment. No imports
of optional native dependencies, package installation or network calls occur.
"""
from __future__ import annotations

import base64
import csv
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path

DISTRIBUTION = "hermes-scope-recall"


def record_integrity(dist) -> dict:
    """Compare RECORD digests and sizes to bytes, never newline-normalized text."""
    record = dist.read_text("RECORD")
    if not record:
        return {"status": "unavailable", "reason": "record_missing"}
    mismatches, checked = [], 0
    try:
        for relative, digest, size in csv.reader(io.StringIO(record)):
            if not digest:  # RECORD itself and generated pyc have no digest.
                continue
            algorithm, expected = digest.split("=", 1)
            path = Path(dist.locate_file(relative))
            try:
                raw = path.read_bytes()
                actual = base64.urlsafe_b64encode(hashlib.new(algorithm, raw).digest()).decode().rstrip("=")
                valid = actual == expected and (not size or len(raw) == int(size))
            except OSError:
                valid = False
            checked += 1
            if not valid:
                mismatches.append(relative)
    except (ValueError, TypeError, csv.Error):
        return {"status": "unavailable", "reason": "record_invalid", "checked": checked}
    return {"status": "mismatch" if mismatches else ("ok" if checked else "unavailable"),
            "checked": checked, "mismatches": mismatches[:32], "mismatch_count": len(mismatches)}


def dependency_health(requirements, *, version_lookup=metadata.version) -> dict:
    """Evaluate declared runtime/installed optional specs and environment markers.

    Missing optional extras are not drift. If an optional package is installed,
    its declared extra requirement still applies. Dev extras are not runtime.
    """
    try:
        from packaging.requirements import Requirement
        from packaging.markers import default_environment
        from packaging.utils import canonicalize_name
        from packaging.version import InvalidVersion
    except ImportError:
        return {"status": "unavailable", "reason": "requirement_parser_missing"}
    rows = []
    try:
        for raw in requirements or ():
            req = Requirement(raw)
            installed = None
            try:
                installed = version_lookup(req.name)
            except metadata.PackageNotFoundError:
                pass
            env = default_environment()
            required = req.marker is None or req.marker.evaluate({**env, "extra": ""})
            optional = any(req.marker and req.marker.evaluate({**env, "extra": extra})
                           for extra in ("lancedb", "codex"))
            if not required and not (optional and installed is not None):
                continue
            try:
                ok = installed is not None and req.specifier.contains(installed, prereleases=True)
            except InvalidVersion:
                ok = False
            rows.append({"name": canonicalize_name(req.name), "spec": str(req.specifier),
                         "installed": installed, "ok": ok})
    except (ValueError, TypeError):
        return {"status": "unavailable", "reason": "requirement_invalid"}
    return {"status": "mismatch" if any(not row["ok"] for row in rows) else "ok", "requirements": rows}


def package_probe() -> dict:
    """Measure the package actually imported by the target interpreter."""
    import scope_recall._version as version
    path = Path(version.__file__).resolve()
    result = {"source": "development", "version": version.__version__, "path": str(path),
              "distribution_version": None, "hot_patched": {"status": "unavailable"},
              "dependency_drift": {"status": "unavailable"}}
    try:
        dist = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return result
    result["distribution_version"] = dist.version
    if path.is_relative_to(Path(dist.locate_file("scope_recall")).resolve()):
        result["source"] = "installed"
        result["hot_patched"] = record_integrity(dist)
        result["dependency_drift"] = dependency_health(dist.requires)
    return result


def version_health(instance: Path, probe: dict, running: dict) -> dict:
    """Compare receipt, distribution, imported version and live breadcrumbs.

    Missing records are explicitly incomplete, not a clean three-way check.
    """
    receipt_version = None
    try:
        receipt = json.loads((instance / ".scope-recall-install-receipt.json").read_text(encoding="utf-8"))
        receipt_version = receipt.get("package_version")
    except (OSError, ValueError, TypeError):
        pass
    versions = {"receipt": receipt_version, "distribution": probe.get("distribution_version"),
                "imported": probe.get("version"),
                "running": [record.get("version") for record in running.get("live_processes", [])]}
    values = [versions["receipt"], versions["distribution"], versions["imported"], *versions["running"]]
    known = [v for v in values if isinstance(v, str) and v]
    mismatch = len(set(known)) > 1
    complete = all(isinstance(v, str) and v for v in values) and bool(versions["running"])
    return {"status": "mismatch" if mismatch else ("ok" if complete else "incomplete"), "versions": versions}


def apply_package_health(report, instance: Path, probe: dict) -> None:
    """Attach independent checks and named gaps to the public doctor report."""
    checks = {name: probe.get(name, {"status": "unavailable"}) for name in ("hot_patched", "dependency_drift")}
    checks["version_mismatch"] = version_health(instance, probe, report.running_code)
    report.package_health = checks
    for name, result in checks.items():
        report.checks.append({"name": name, "result": result["status"]})
        if result["status"] == "mismatch" and name not in report.capability_gaps:
            report.capability_gaps.append(name)


if __name__ == "__main__":
    print(json.dumps(package_probe()))
