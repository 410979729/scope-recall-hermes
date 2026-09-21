"""A status file is read and replaced beside another process without failing either one.

On Windows a file cannot be opened for the instant ``os.replace`` swaps it in, and cannot be
replaced while a reader holds it open; either side gets ``PermissionError``.  The supervisor reads
its control file outside the control lock, beside the wake that rewrites it, so now and then the
refusal ended a supervisor.  It was seen as a nightly CI failure: ``control.read()`` raised
``PermissionError: [Errno 13]`` in ``test_real_detached_supervisor_processes_future_local_work_after_host_exit``
while the real supervisor process was writing the same file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scope_recall.runtime import worker_entry


def _refusing(times, real):
    """``real``, refused like a sharing violation for the first ``times`` calls."""
    calls = {"count": 0}

    def call(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] <= times:
            raise PermissionError(13, "Permission denied")
        return real(*args, **kwargs)

    return call, calls


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(worker_entry, "_SHARING_PAUSE_SECONDS", 0.0)


def test_a_read_refused_for_an_instant_is_read_again(tmp_path, monkeypatch):
    path = tmp_path / "runtime-supervisor-aaa.json"
    path.write_text(json.dumps({"state": "waiting", "wake_revision": 7}), encoding="utf-8")
    read, calls = _refusing(3, Path.read_text)
    monkeypatch.setattr(Path, "read_text", read)

    assert worker_entry._read_metadata(path) == {"state": "waiting", "wake_revision": 7}
    assert calls["count"] == 4


def test_a_replace_refused_for_an_instant_is_made_again(tmp_path, monkeypatch):
    path = tmp_path / "runtime-supervisor-aaa.json"
    path.write_text(json.dumps({"wake_revision": 1}), encoding="utf-8")
    replace, calls = _refusing(2, os.replace)
    monkeypatch.setattr(worker_entry.os, "replace", replace)

    worker_entry._atomic_metadata(path, {"wake_revision": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"wake_revision": 2} and calls["count"] == 3
    assert [item.name for item in tmp_path.iterdir()] == [path.name], "no temporary file is left behind"


def test_a_file_that_is_really_forbidden_still_fails(tmp_path, monkeypatch):
    path = tmp_path / "runtime-supervisor-aaa.json"
    path.write_text("{}", encoding="utf-8")
    read, calls = _refusing(10_000, Path.read_text)
    monkeypatch.setattr(Path, "read_text", read)

    with pytest.raises(PermissionError):
        worker_entry._read_metadata(path)
    assert calls["count"] == worker_entry._SHARING_RETRIES
