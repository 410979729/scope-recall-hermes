"""Cross-platform byte-fingerprint tests for the release source manifest."""

from __future__ import annotations

import subprocess
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import check  # noqa: E402


@pytest.fixture(autouse=True)
def _make_git_fixture_writable(tmp_path: Path):
    yield
    for path in sorted(tmp_path.rglob("*"), reverse=True):
        try:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitattributes").write_text("*.txt text eol=lf\n*.bin -text\n", encoding="utf-8")
    return tmp_path


def test_text_line_endings_are_canonical_but_binary_and_edits_are_sensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    text_path = root / "notes with spaces.txt"
    binary_path = root / "payload.bin"
    text_path.write_bytes(b"alpha\r\nbeta\r\n")
    binary_path.write_bytes(b"alpha\r\nbeta\r\n")
    monkeypatch.setattr(check, "ROOT", root)

    crlf_manifest = check._source_manifest()
    assert crlf_manifest["notes with spaces.txt"].startswith("git-blob:")
    assert crlf_manifest["payload.bin"].startswith("git-blob:")

    text_path.write_bytes(b"alpha\nbeta\n")
    assert check._source_manifest() == crlf_manifest

    binary_path.write_bytes(b"alpha\nbeta\n")
    binary_manifest = check._source_manifest()
    assert binary_manifest["payload.bin"] != crlf_manifest["payload.bin"]

    text_path.write_bytes(b"alpha\ngamma\n")
    assert check._source_manifest()["notes with spaces.txt"] != crlf_manifest["notes with spaces.txt"]


def test_untracked_addition_and_deleted_path_change_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    tracked = root / "tracked.txt"
    tracked.write_text("kept\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    monkeypatch.setattr(check, "ROOT", root)

    before = check._source_manifest()
    (root / "new file.txt").write_text("new\n", encoding="utf-8")
    with_untracked = check._source_manifest()
    assert "new file.txt" in with_untracked
    assert with_untracked != before

    tracked.unlink()
    after_delete = check._source_manifest()
    assert after_delete["tracked.txt"] == "missing"
    assert after_delete != with_untracked


def test_newline_path_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(check, "ROOT", root)
    def fake_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, b"bad\nname.txt\0", b"")
    monkeypatch.setattr(check.subprocess, "run", fake_git)

    with pytest.raises(RuntimeError, match="newline"):
        check._source_manifest()
