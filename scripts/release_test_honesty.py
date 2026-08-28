"""Pytest plugin that emits exact, source-bound final-suite accounting."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping


SCHEMA_VERSION = "scope-recall.test-honesty.v1"
OUTPUT_ENV = "SCOPE_RECALL_TEST_HONESTY_OUTPUT"
SOURCE_COMMIT_ENV = "SCOPE_RECALL_SOURCE_COMMIT"
SOURCE_TREE_ENV = "SCOPE_RECALL_SOURCE_TREE"
TIMEOUTS_ENV = "SCOPE_RECALL_TEST_TIMEOUTS_JSON"
FAILURE_FIXES_ENV = "SCOPE_RECALL_FIRST_FAILURE_FIXES_JSON"
FIRST_FAILURE_STATUS_NOT_PROVIDED = "not_provided"
FIRST_FAILURE_STATUS_DECLARED = "declared"

_PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?i)(?:\\\\\?\\)?[a-z]:\\Users\\[^\\\s:]+(?:\\[^\s:]*)?"),
    re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s:]+(?:/[^\s:]*)?"),
)


def _sanitize_evidence_text(value: str) -> str:
    sanitized = str(value)
    for pattern in _PRIVATE_PATH_PATTERNS:
        sanitized = pattern.sub("<private-path>", sanitized)
    return sanitized


def _json_array_from_env(
    name: str,
    environment: Mapping[str, str] | None = None,
) -> list[object]:
    env = os.environ if environment is None else environment
    raw = str(env.get(name) or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain a JSON array") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"{name} must contain a JSON array")
    return payload


def _skip_reason(report: object) -> str:
    longrepr = getattr(report, "longrepr", "")
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        rendered = str(longrepr[2]).strip()
    else:
        rendered = str(longrepr).strip()
    return rendered or "pytest reported a skip without a rendered reason"


class ReleaseTestHonestyPlugin:
    """Collect one terminal accounting record per pytest node ID."""

    def __init__(
        self,
        *,
        output: Path,
        source_commit: str,
        source_tree: str,
        timeout_overrides: list[object],
        first_failure_fixes: list[object],
        first_failure_fixes_status: str = FIRST_FAILURE_STATUS_NOT_PROVIDED,
    ) -> None:
        self.output = output
        self.source_commit = source_commit
        self.source_tree = source_tree
        self.timeout_overrides = timeout_overrides
        self.first_failure_fixes = first_failure_fixes
        self.first_failure_fixes_status = first_failure_fixes_status
        self.started = time.monotonic()
        self.passed: set[str] = set()
        self.skipped: dict[str, str] = {}
        self.xfail: set[str] = set()
        self.xpass: set[str] = set()
        self.failed: set[str] = set()
        self.errors: set[str] = set()
        self.rerun_count = 0

    def pytest_sessionstart(self, session: object) -> None:
        del session
        self.started = time.monotonic()

    def pytest_runtest_logreport(self, report: object) -> None:
        node_id = str(getattr(report, "nodeid", "") or "").strip()
        if not node_id:
            return
        outcome = str(getattr(report, "outcome", "") or "")
        when = str(getattr(report, "when", "") or "")
        was_xfail = bool(getattr(report, "wasxfail", False))
        if outcome == "rerun":
            self.rerun_count += 1
            return
        if was_xfail:
            if outcome == "skipped":
                self.xfail.add(node_id)
            elif outcome == "passed":
                self.xpass.add(node_id)
            elif outcome == "failed":
                self.failed.add(node_id)
            return
        if outcome == "skipped":
            self.skipped.setdefault(
                node_id,
                _sanitize_evidence_text(_skip_reason(report)),
            )
        elif outcome == "passed" and when == "call":
            self.passed.add(node_id)
        elif outcome == "failed" and when == "call":
            self.failed.add(node_id)
        elif outcome == "failed":
            self.errors.add(node_id)

    def payload(self, *, collected: int) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "collected": int(collected),
            "passed": len(self.passed),
            "failed": len(self.failed),
            "errors": len(self.errors),
            "skipped": [
                {"node_id": node_id, "reason": self.skipped[node_id]}
                for node_id in sorted(self.skipped)
            ],
            "xfail": len(self.xfail),
            "xpass": len(self.xpass),
            "rerun_count": self.rerun_count,
            "timeout_overrides": self.timeout_overrides,
            "duration_seconds": round(time.monotonic() - self.started, 3),
            "first_failure_fixes": self.first_failure_fixes,
            "first_failure_fixes_status": self.first_failure_fixes_status,
        }

    def pytest_sessionfinish(self, session: object, exitstatus: int) -> None:
        del exitstatus
        payload = self.payload(collected=int(getattr(session, "testscollected", 0)))
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def build_plugin_from_environment(
    environment: Mapping[str, str] | None = None,
) -> ReleaseTestHonestyPlugin | None:
    env = os.environ if environment is None else environment
    output = str(env.get(OUTPUT_ENV) or "").strip()
    if not output:
        return None
    raw_failure_fixes = str(env.get(FAILURE_FIXES_ENV) or "").strip()
    failure_status = (
        FIRST_FAILURE_STATUS_DECLARED
        if raw_failure_fixes
        else FIRST_FAILURE_STATUS_NOT_PROVIDED
    )
    return ReleaseTestHonestyPlugin(
        output=Path(output),
        source_commit=str(env.get(SOURCE_COMMIT_ENV) or ""),
        source_tree=str(env.get(SOURCE_TREE_ENV) or ""),
        timeout_overrides=_json_array_from_env(TIMEOUTS_ENV, env),
        first_failure_fixes=_json_array_from_env(FAILURE_FIXES_ENV, env),
        first_failure_fixes_status=failure_status,
    )


def pytest_configure(config: Any) -> None:
    plugin = build_plugin_from_environment()
    if plugin is not None:
        config.pluginmanager.register(plugin, "scope-recall-release-test-honesty")
