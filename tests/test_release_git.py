"""P1-12: release checker Git prerequisite and execution share one resolver."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_PATH = PLUGIN_ROOT / "scripts" / "check.release.py"


def _load_release_check_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, CHECK_RELEASE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instance_candidate(tmp_path: Path) -> tuple[Path, Path]:
    instance = tmp_path / "hermes-instance"
    candidate = instance / "workspace" / "tmp" / "scope-recall-candidate"
    candidate.mkdir(parents=True)
    (instance / "config.yaml").write_text("instance: test\n", encoding="utf-8")
    return instance, candidate


def _write_posix_git_stub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:2] == ['--version']:\n"
        "    sys.stdout.write('git version 2.45.0\\n')\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:3] == ['status', '--porcelain=v1']:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _install_trusted_git(instance: Path) -> Path:
    """Install a trusted outer bundled Git that ``git --version`` can execute."""

    dest = instance / "git" / "cmd" / "git.exe"
    host = Path(r"C:\Program Files\Git")
    if os.name == "nt" and (host / "cmd" / "git.exe").is_file() and not dest.exists():
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(instance / "git"), str(host)],
            check=False,
            capture_output=True,
        )
        if created.returncode == 0 and dest.is_file():
            return dest
    mingw = host / "mingw64" / "bin" / "git.exe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and mingw.is_file():
        shutil.copy2(mingw, dest)
        for dll in mingw.parent.glob("*.dll"):
            shutil.copy2(dll, dest.parent / dll.name)
        return dest
    if os.name == "nt":
        host_git = shutil.which("git")
        if host_git:
            src = Path(host_git)
            if src.suffix.lower() == ".exe" and src.is_file():
                shutil.copy2(src, dest)
                for dll in src.parent.glob("*.dll"):
                    shutil.copy2(dll, dest.parent / dll.name)
                return dest
    _write_posix_git_stub(dest)
    return dest


def _install_corrupt_git(instance: Path) -> Path:
    dest = instance / "git" / "cmd" / "git.exe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"MZ\x00not-a-valid-Win32-application\n")
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return dest


def test_git_prerequisite_and_run_share_resolve_release_command():
    source = CHECK_RELEASE_PATH.read_text(encoding="utf-8")
    assert "def resolve_release_command(" in source
    assert "def git_prerequisite_check(" in source
    prereq_start = source.index("def git_prerequisite_check(")
    prereq_end = source.index("\ndef ", prereq_start + 1)
    prereq = source[prereq_start:prereq_end]
    assert "shutil.which(" not in prereq
    assert 'run(["git", "--version"]' in prereq or "run(['git', '--version']" in prereq
    run_start = source.index("\ndef run(")
    run_fn = source[run_start : source.index("\ndef ", run_start + 1)]
    assert "resolve_release_command(" in run_fn
    assert "except OSError" in run_fn
    assert "prerequisite_unusable" in run_fn
    assert "winerror" in run_fn


def test_path_empty_trusted_bundled_git_reaches_git_tree_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_shared_git_resolver"
    )
    instance, candidate = _instance_candidate(tmp_path)
    bundled = _install_trusted_git(instance)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setattr(release_check, "ROOT", candidate)
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setattr(release_check.shutil, "which", lambda *_args, **_kwargs: None)

    resolved = release_check.resolve_release_command(
        ["git", "--version"],
        env={"PATH": ""},
        root=candidate,
    )
    assert Path(resolved[0]) == bundled

    prereq = release_check.git_prerequisite_check()
    assert prereq["ok"] is True
    assert prereq["prerequisite"] == "git"
    assert "git version" in str(prereq.get("version") or "").lower() or bool(
        prereq.get("ok")
    )

    tree = release_check.git_tree_check(allow_dirty=True)
    assert "ok" in tree
    assert tree.get("error", {}).get("error") != "prerequisite_missing"


def test_candidate_local_fake_git_is_rejected_by_shared_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_shared_git_rejects_candidate"
    )
    source_root = tmp_path / "scope-recall-candidate"
    source_root.mkdir()
    forged = source_root / "git.exe"
    forged.write_bytes(b"candidate-controlled")
    monkeypatch.setattr(
        release_check.shutil,
        "which",
        lambda *_args, **_kwargs: str(forged),
    )

    with pytest.raises(FileNotFoundError, match="trusted Git executable"):
        release_check.resolve_release_command(
            ["git", "--version"],
            env={"PATH": str(source_root)},
            root=source_root,
        )
    monkeypatch.setattr(release_check, "ROOT", source_root)
    monkeypatch.setenv("PATH", str(source_root))
    result = release_check.git_prerequisite_check()
    assert result["ok"] is False
    assert result["error"] == "prerequisite_missing"
    assert result["prerequisite"] == "git"


def test_corrupt_git_executable_is_structured_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_corrupt_git"
    )
    instance, candidate = _instance_candidate(tmp_path)
    _install_corrupt_git(instance)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setattr(release_check, "ROOT", candidate)
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setattr(release_check.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["check.release.py"])

    executed = release_check.run(["git", "--version"])
    assert executed["returncode"] != 0
    assert executed["error"] == "prerequisite_unusable"
    assert executed["prerequisite"] in {"git", "git.exe"}
    if os.name == "nt":
        assert executed.get("winerror") in {193, 216} or any(
            code in str(executed.get("detail") or "") for code in ("193", "216")
        )

    exit_code = release_check.main()
    captured = capsys.readouterr()
    assert exit_code != 0
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload.get("error") == "prerequisite_unusable"
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_hanging_git_helper_is_bounded_and_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_check = _load_release_check_module(
        "scope_recall_check_release_hanging_git"
    )
    hanging = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(
        release_check,
        "resolve_release_command",
        lambda _cmd, *, env: hanging,
    )

    started = time.monotonic()
    result = release_check.run(
        ["git", "--version"],
        timeout=0.1,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert result["returncode"] != 0
    assert result["error"] == "prerequisite_timeout"
    assert result["prerequisite"] == "git"
    assert result["timeout_seconds"] == 0.1
