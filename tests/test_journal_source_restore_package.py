"""Real sdist/wheel proof for journal source-restore runtime and tests."""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

import pytest

from journal_source_restore_support import apply_kwargs, build_source_restore_pair, cli_argv
from scope_recall.maintenance_lease import activation_lease_path


ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT_SECONDS = 300
# The installed-sdist rehearsal runs six complete source-restore test modules
# in a nested interpreter.  Windows Python 3.11 can legitimately take just
# over five minutes for that workload on a hosted runner, while the surrounding
# install/build/CLI probes should retain their tighter five-minute ceiling.
NESTED_PYTEST_TIMEOUT_SECONDS = 600
NESTED_BUILD_REQUIREMENTS = (
    "setuptools>=77,<82",
    "wheel>=0.45,<1",
)
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
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "VIRTUAL_ENV",
        }
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def _nested_pytest_parent() -> Path:
    declared = str(os.environ.get("SCOPE_RECALL_TEST_BOUNDARY_PARENT") or "").strip()
    if not declared:
        raise AssertionError("SCOPE_RECALL_TEST_BOUNDARY_PARENT is required")
    parent = Path(declared).resolve(strict=False)
    parent.mkdir(parents=True, exist_ok=True)
    return parent


@pytest.fixture
def short_package_work() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix="sr-package-work-",
        dir=_nested_pytest_parent(),
    ) as work_raw:
        yield Path(work_raw)


def test_clean_env_drops_parent_python_and_pytest_state(monkeypatch) -> None:
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "VIRTUAL_ENV"):
        monkeypatch.setenv(key, "must-not-leak")

    env = _clean_env(extra={"SCOPE_RECALL_TEST_MARKER": "preserved"})

    assert not {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "VIRTUAL_ENV",
    }.intersection(env)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["SCOPE_RECALL_TEST_MARKER"] == "preserved"


def test_nested_builder_requirements_remain_upper_bounded() -> None:
    assert NESTED_BUILD_REQUIREMENTS == (
        "setuptools>=77,<82",
        "wheel>=0.45,<1",
    )
    assert all("<" in requirement for requirement in NESTED_BUILD_REQUIREMENTS)


def test_nested_pytest_parent_ignores_process_tempdir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    declared = tmp_path / "declared-boundary-parent"
    monkeypatch.setenv("SCOPE_RECALL_TEST_BOUNDARY_PARENT", str(declared))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "nested-process-temp"))

    assert _nested_pytest_parent() == declared.resolve()


def test_short_package_work_is_bounded(short_package_work: Path) -> None:
    assert short_package_work.parent == _nested_pytest_parent()


def _copy_sources(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".execution",
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
        [str(python), "-m", "pip", "install", *NESTED_BUILD_REQUIREMENTS],
        capture_output=True,
        text=True,
        env=_clean_env(),
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert built.returncode == 0, built.stdout + "\n" + built.stderr
    wheels = list(dist_dir.glob("*.whl")) or list(src.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz")) or list(src.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, (
        f"stdout={built.stdout}\nstderr={built.stderr}\ndist={list(dist_dir.iterdir())}"
    )
    return sdists[0], wheels[0]


def test_real_sdist_and_installed_wheel_source_restore(
    short_package_work: Path,
) -> None:
    tmp_path = short_package_work
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert tester_ready.returncode == 0, tester_ready.stdout + "\n" + tester_ready.stderr
    tester_env = _clean_env(extra={"PYTHONPATH": str(extracted)})
    # Keep the nested pytest root short on Windows.  A path under this already
    # nested build fixture can exceed legacy Win32 limits used by lock/receipt
    # subprocesses even though the product paths themselves are valid.
    with tempfile.TemporaryDirectory(
        prefix="sr-sdist-pytest-",
        dir=_nested_pytest_parent(),
    ) as nested_raw:
        nested_basetemp = Path(nested_raw)
        collect = subprocess.run(
            [
                str(tester_python),
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "--basetemp",
                str(nested_basetemp / "collect"),
                *SOURCE_RESTORE_PYTEST,
            ],
            cwd=str(extracted),
            capture_output=True,
            text=True,
            env=tester_env,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert collect.returncode == 0, collect.stdout + "\n" + collect.stderr
        ran = subprocess.run(
            [
                str(tester_python),
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(nested_basetemp / "run"),
                *SOURCE_RESTORE_PYTEST,
            ],
            cwd=str(extracted),
            capture_output=True,
            text=True,
            env=tester_env,
            check=False,
            timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert applied.returncode == 0, applied.stdout + "\n" + applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["ok"] is True
    assert applied_payload["journal_inserted_count"] == 19
    assert applied_payload["digest_run_inserted_count"] == 2
    assert not activation_lease_path(pair.target_path).exists()
    assert "pairs" not in applied_payload
    assert "path" not in applied_payload
