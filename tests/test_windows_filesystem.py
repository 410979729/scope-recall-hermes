"""Windows extended-length filesystem contract tests.

The release path helpers must preflight complete copy destinations before
mutation, keep Windows I/O prefixes out of public receipts, and remove partial
copies after failures.  Real deep-path tests run only on Windows; portable
normal-path behavior remains covered everywhere.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scope_recall.windows_filesystem import (
    PathBudgetError,
    copy_tree,
    io_path,
    preflight_copy_tree,
    public_path,
    remove_path,
)


def _deep_home(base: Path, *, target_length: int = 178) -> Path:
    component_length = target_length - len(str(base)) - 1
    if component_length < 8 or component_length > 240:
        pytest.skip(
            f"temporary root length {len(str(base))} cannot form the {target_length}-character fixture"
        )
    return base / ("p" * component_length)


def test_public_path_round_trips_ordinary_paths(tmp_path: Path):
    path = tmp_path / "normal" / "file.txt"

    assert public_path(path) == path.resolve(strict=False)
    assert "\\\\?\\" not in str(public_path(io_path(path)))


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_preflight_rejects_oversized_component_before_destination_mutation(
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("payload", encoding="utf-8")
    destination = tmp_path / ("d" * 256)

    with pytest.raises(PathBudgetError, match="component"):
        preflight_copy_tree(source, destination)

    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_copy_tree_crosses_legacy_windows_limit_without_prefix_leak(tmp_path: Path):
    source = tmp_path / "source"
    nested = source / ("n" * 80)
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("deep payload", encoding="utf-8")
    destination = _deep_home(tmp_path) / "backups" / "sr" / "r" / "12345678" / "scope-recall"

    plan = copy_tree(source, destination)
    copied = destination / ("n" * 80) / "payload.txt"

    assert plan.max_destination_chars > 260
    assert plan.destination == destination.resolve(strict=False)
    assert "\\\\?\\" not in str(plan.destination)
    assert os.path.isfile(io_path(copied))
    assert Path(io_path(copied)).read_text(encoding="utf-8") == "deep payload"

    remove_path(_deep_home(tmp_path), missing_ok=True)
    assert not os.path.exists(io_path(_deep_home(tmp_path)))


def test_copy_tree_removes_partial_destination_after_copy_failure(
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")
    destination = tmp_path / "destination"

    def flaky_copy(source_path, destination_path, *args, **kwargs):
        if Path(source_path).name == "b.txt":
            raise OSError("injected copy failure")
        return shutil.copy2(source_path, destination_path, *args, **kwargs)

    with pytest.raises(OSError, match="injected copy failure"):
        copy_tree(source, destination, copy_function=flaky_copy)

    assert not destination.exists()
