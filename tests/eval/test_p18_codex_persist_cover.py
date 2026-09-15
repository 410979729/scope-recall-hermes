"""Codex persist-before-runtime cover overlays the -I hook interpreter only."""
from pathlib import Path

from p18_run_journey import (
    JourneyExecutionError,
    _install_worktree_codex_persist_cover,
)


def _binding(tmp_path: Path, original=b"# frozen handler\n_CAPTURE_TIMEOUT_S = 1.0\n"):
    env = tmp_path / "interp"
    target = env / "Lib" / "site-packages" / "scope_recall" / "adapters" / "codex" / "handler.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    python = env / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return {"loader": {"candidate_python": {"path": str(python)}}}, target


def test_codex_persist_cover_overlays_and_restores(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_RECALL_CODEX_ATTEMPT_AUTHORIZATION", "authorized")
    binding, target = _binding(tmp_path)
    original = target.read_bytes()
    restore = _install_worktree_codex_persist_cover(binding)
    assert restore is not None
    overlaid = target.read_bytes()
    assert overlaid != original
    assert b"def _ensure_host_runtime(" in overlaid
    assert b"_CAPTURE_TIMEOUT_S = 1.0" in overlaid
    assert b"_TOTAL_BUDGET_S = 2.0" in overlaid
    restore()
    assert target.read_bytes() == original
    assert not target.with_name("handler.py.frozen-backup").is_file()


def test_codex_persist_cover_noop_without_authorization(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOPE_RECALL_CODEX_ATTEMPT_AUTHORIZATION", raising=False)
    binding, target = _binding(tmp_path)
    original = target.read_bytes()
    assert _install_worktree_codex_persist_cover(binding) is None
    assert target.read_bytes() == original


def test_codex_persist_cover_refuses_missing_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_RECALL_CODEX_ATTEMPT_AUTHORIZATION", "authorized")
    python = tmp_path / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    binding = {"loader": {"candidate_python": {"path": str(python)}}}
    try:
        _install_worktree_codex_persist_cover(binding)
    except JourneyExecutionError as exc:
        assert str(exc) == "codex_hook_interpreter_handler_missing"
    else:
        raise AssertionError("expected JourneyExecutionError")
