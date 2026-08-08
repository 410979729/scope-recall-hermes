"""Regression tests for the doctor CLI output encoding contract."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path


def test_doctor_json_stdout_is_utf8_safe_under_cp936_console(monkeypatch, tmp_path: Path) -> None:
    """Machine JSON must remain UTF-8 decodable on Chinese Windows consoles."""

    from scripts import doctor

    monkeypatch.setattr(
        doctor,
        "parse_args",
        lambda: argparse.Namespace(
            json=True,
            source_root=str(tmp_path),
            hermes_home="",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "source_report",
        lambda _root: (
            {"description": "中文健康状态"},
            {"ok": True},
            [],
        ),
    )

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp936", newline="\n")
    monkeypatch.setattr(sys, "stdout", console)

    assert doctor.main() == 0
    console.flush()

    payload = json.loads(raw.getvalue().decode("utf-8"))
    assert payload["source"]["description"] == "中文健康状态"
