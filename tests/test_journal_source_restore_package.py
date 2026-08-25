"""Real sdist/wheel proof for journal source-restore runtime and tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import venv
import zipfile
from pathlib import Path

from journal_source_restore_support import apply_kwargs, build_source_restore_pair, cli_argv
from scope_recall.maintenance_lease import activation_lease_path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_RUNTIME = (
    "journal_source_restore.py",
    "journal_source_restore_snapshot.py",
    "journal_source_restore_rows.py",
    "scripts/journal.source_restore.py",
    "docs/journal-source-restore.md",
)
REQUIRED_SDIST_TESTS = (
    "tests/plugin_source.py",
    "tests/conftest.py",
    "tests/journal_source_restore_support.py",
    "tests/journal_source_restore_oracles.py",
    "tests/test_journal_source_restore_planning.py",
    "tests/test_journal_source_restore_apply.py",
    "tests/test_journal_source_restore_cli.py",
    "tests/test_journal_source_restore_snapshot.py",
    "tests/test_journal_source_restore_rows.py",
    "tests/test_journal_source_restore_ledger.py",
)
SOURCE_RESTORE_PYTEST = [
    "tests/test_journal_source_restore_planning.py",
    "tests/test_journal_source_restore_snapshot.py",
    "tests/test_journal_source_restore_rows.py",
    "tests/test_journal_source_restore_apply.py",
    "tests/test_journal_source_restore_cli.py",
    "tests/test_journal_source_restore_ledger.py",
]


def _clean_env(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def _copy_sources(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".git",
            ".hermes-agent-src",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "build",
            "dist",
            "*.egg-info",
            ".venv",
        ),
        dirs_exist_ok=True,
    )
    return dest


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _build_artifacts(work: Path) -> tuple[Path, Path]:
    src = _copy_sources(work / "src")
    dist_dir = work / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    builder = work / "builder-venv"
    venv.create(builder, with_pip=True, clear=True)
    python = _venv_python(builder)
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "setuptools>=77", "wheel"],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + "\n" + installed.stderr
    built = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from pathlib import Path;"
                "from setuptools.build_meta import build_sdist, build_wheel;"
                "import sys;"
                "out = Path(sys.argv[1]);"
                "print('sdist', build_sdist(str(out)), flush=True);"
                "print('wheel', build_wheel(str(out)), flush=True);"
                "print('members', sorted(p.name for p in out.iterdir()), flush=True)"
            ),
            str(dist_dir),
        ],
        cwd=str(src),
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    assert built.returncode == 0, built.stdout + "\n" + built.stderr
    wheels = list(dist_dir.glob("*.whl")) or list(src.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz")) or list(src.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, (
        f"stdout={built.stdout}\nstderr={built.stderr}\ndist={list(dist_dir.iterdir())}"
    )
    return sdists[0], wheels[0]


def test_real_sdist_and_installed_wheel_source_restore(tmp_path: Path) -> None:
    sdist, wheel = _build_artifacts(tmp_path)
    with tarfile.open(sdist, "r:gz") as archive:
        names = set(archive.getnames())
        archive.extractall(tmp_path / "sdist")
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    for relative in REQUIRED_RUNTIME:
        assert any(name.endswith(relative) for name in names), relative
        assert any(name.endswith(relative) for name in wheel_names), relative
    for relative in REQUIRED_SDIST_TESTS:
        assert any(name.endswith(relative) for name in names), relative

    extracted = next((tmp_path / "sdist").iterdir())
    tester = tmp_path / "sdist-test-venv"
    venv.create(tester, with_pip=True, clear=True)
    tester_python = _venv_python(tester)
    tester_ready = subprocess.run(
        [str(tester_python), "-m", "pip", "install", "pytest", "PyYAML", "jsonschema"],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    assert tester_ready.returncode == 0, tester_ready.stdout + "\n" + tester_ready.stderr
    tester_env = _clean_env(extra={"PYTHONPATH": str(extracted)})
    collect = subprocess.run(
        [str(tester_python), "-m", "pytest", "--collect-only", "-q", *SOURCE_RESTORE_PYTEST],
        cwd=str(extracted),
        capture_output=True,
        text=True,
        env=tester_env,
        check=False,
    )
    assert collect.returncode == 0, collect.stdout + "\n" + collect.stderr
    ran = subprocess.run(
        [str(tester_python), "-m", "pytest", "-q", *SOURCE_RESTORE_PYTEST],
        cwd=str(extracted),
        capture_output=True,
        text=True,
        env=tester_env,
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + "\n" + ran.stderr

    venv_dir = tmp_path / "wheel-venv"
    venv.create(venv_dir, with_pip=True, clear=True)
    if os.name == "nt":
        python = venv_dir / "Scripts" / "python.exe"
        console = venv_dir / "Scripts" / "hermes-scope-recall.exe"
        path_prefix = str(venv_dir / "Scripts")
    else:
        python = venv_dir / "bin" / "python"
        console = venv_dir / "bin" / "hermes-scope-recall"
        path_prefix = str(venv_dir / "bin")
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + "\n" + installed.stderr
    env = _clean_env(
        extra={
            "VIRTUAL_ENV": str(venv_dir),
            "PATH": path_prefix + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    help_done = subprocess.run(
        [str(console), "journal", "source-restore", "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert help_done.returncode == 0, help_done.stdout + "\n" + help_done.stderr
    assert "operation-id" in help_done.stdout
    assert "trusted SQLite snapshot" in help_done.stdout

    pair = build_source_restore_pair(tmp_path / "fixture")
    dry = subprocess.run(
        [str(console), "journal", "source-restore", *cli_argv(apply_kwargs(pair), apply=False)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert dry.returncode == 0, dry.stdout + "\n" + dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["ok"] is True
    assert dry_payload["dry_run"] is True
    assert dry_payload["journal_selected_count"] == 19

    pair.backup_path.parent.mkdir(parents=True, exist_ok=True)
    pair.backup_path.write_bytes(b"preexisting-backup")
    refused = subprocess.run(
        [str(console), "journal", "source-restore", *cli_argv(apply_kwargs(pair), apply=True)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert refused.returncode != 0
    refused_payload = json.loads(refused.stdout)
    assert refused_payload["ok"] is False
    assert refused_payload["error_code"] == "prewrite_backup_failed"
    assert not activation_lease_path(pair.target_path).exists()

    pair.backup_path.unlink()
    applied = subprocess.run(
        [str(console), "journal", "source-restore", *cli_argv(apply_kwargs(pair), apply=True)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert applied.returncode == 0, applied.stdout + "\n" + applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["ok"] is True
    assert applied_payload["journal_inserted_count"] == 19
    assert applied_payload["digest_run_inserted_count"] == 2
    assert not activation_lease_path(pair.target_path).exists()
    assert "pairs" not in applied_payload
    assert "path" not in applied_payload
