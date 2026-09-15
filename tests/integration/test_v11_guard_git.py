from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("push", id="push-with-allowed-absolute-C"),
        pytest.param("clone", id="clone-with-allowed-absolute-target"),
        pytest.param("missing-C-argument", id="missing-C-argument"),
    ],
)
def test_bare_git_network_or_malformed_commands_are_rejected(tmp_path, command):
    if command == "push":
        argv = ["git", "-C", str(tmp_path), "push"]
    elif command == "clone":
        argv = ["git", "clone", "https://example.invalid/repo.git", str(tmp_path / "clone")]
    else:
        argv = ["git", "-C", "-c", "user.name=TEST", "status"]
    with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
        subprocess.run(argv, cwd=tmp_path, check=False, capture_output=True, text=True)


def test_bare_git_relative_C_cannot_escape_actual_cwd(tmp_path):
    protected = Path(os.environ["SCOPE_RECALL_TEST_PROTECTED_HOME"])
    relative = os.path.relpath(protected, tmp_path)
    with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
        subprocess.run(["git", "-C", relative, "status"], cwd=tmp_path, check=False)


def test_bare_git_local_init_hash_and_status_are_allowed(tmp_path):
    repo = tmp_path / "repo"
    init = subprocess.run(["git", "init", str(repo)], cwd=tmp_path, check=False, capture_output=True, text=True)
    assert init.returncode == 0, init.stdout + init.stderr
    blob = repo / "TEST-blob.txt"
    blob.write_text("TEST-git-blob", encoding="utf-8")
    hashed = subprocess.run(["git", "hash-object", str(blob)], cwd=repo, check=False, capture_output=True, text=True)
    assert hashed.returncode == 0, hashed.stdout + hashed.stderr
    status = subprocess.run(["git", "-C", str(repo), "status", "--short"], cwd=tmp_path, check=False, capture_output=True, text=True)
    assert status.returncode == 0, status.stdout + status.stderr
