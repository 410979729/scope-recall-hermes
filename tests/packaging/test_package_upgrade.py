"""D14: actual offline uv replacement in a disposable pip-less venv only."""
from __future__ import annotations

from contextlib import contextmanager
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from scope_recall.maintenance import package_upgrade as upgrade


def _wheel(directory, version):
    """A synthetic, dependency-free RECORD fixture, never a release artifact."""
    info = f'hermes_scope_recall-{version}.dist-info'
    files = {
        'scope_recall/__init__.py': f'fixture_version = {version!r}\n'.encode(),
        'scope_recall/maintenance/__init__.py': b'',
        'scope_recall/maintenance/cli.py': b'# TEST fixture import\n',
        f'{info}/METADATA': f'Metadata-Version: 2.1\nName: hermes-scope-recall\nVersion: {version}\n'.encode(),
        f'{info}/WHEEL': b'Wheel-Version: 1.0\nGenerator: TEST\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
    }
    record = io.StringIO(newline='')
    writer = csv.writer(record)
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
        writer.writerow((name, 'sha256=' + digest, len(data)))
    writer.writerow((f'{info}/RECORD', '', ''))
    files[f'{info}/RECORD'] = record.getvalue().encode()
    path = directory / f'hermes_scope_recall-{version}-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _run(args):
    done = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                          encoding='utf-8', timeout=60)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done.stdout


@pytest.fixture
def target(tmp_path, monkeypatch):
    uv = os.environ.get('SCOPE_RECALL_TEST_UV') or shutil.which('uv')
    if not uv:
        pytest.skip('isolated package fixture requires external uv')
    uv = Path(uv).resolve()
    monkeypatch.setenv('UV_PYTHON_DOWNLOADS', 'never')
    monkeypatch.setenv('UV_CACHE_DIR', str(tmp_path / 'uv-cache'))
    for key in ('PYTHONHOME', 'VIRTUAL_ENV'):
        monkeypatch.delenv(key, raising=False)
    venv = tmp_path / 'TEST-pipless'
    _run([uv, 'venv', '--python', sys.executable, str(venv)])
    python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    _run([python, '-I', '-c', 'import importlib.util; assert importlib.util.find_spec("pip") is None'])
    old, new = (_wheel(tmp_path, v) for v in ('0.0.1', '0.0.2'))
    _run([uv, 'pip', 'install', '--python', python, '--no-index', '--no-deps', old])
    return python, new, uv


def test_offline_replacement_without_pip_retains_verified_backup(target, tmp_path):
    python, wheel, uv = target
    backup = tmp_path / 'backup'
    result = upgrade.replace_package(python, wheel, backup, source_quiesced=True, uv=uv)
    assert result['state'] == 'package_verified'
    assert result['previous_version'] == '0.0.1' and result['target_version'] == '0.0.2'
    assert not result['host_restart_allowed'] and not result['automatic_rollback']
    assert _run([python, '-I', '-B', '-c', 'import scope_recall; print(scope_recall.fixture_version)']).strip() == '0.0.2'
    saved = json.loads((backup / 'package-upgrade.json').read_text(encoding='utf-8'))
    for item in saved['files']:
        assert hashlib.sha256((backup / 'files' / item['path']).read_bytes()).hexdigest() == item['sha256']
    _run([python, '-I', '-c', 'import importlib.util; assert importlib.util.find_spec("pip") is None'])


def test_partial_installer_failure_keeps_backup_and_requires_recovery(target, tmp_path, monkeypatch):
    python, wheel, uv = target
    backup = tmp_path / 'failed-backup'
    original = upgrade.subprocess.run
    def fail_install(args, **kwargs):
        if args[0] == str(uv) and args[1:3] == ['pip', 'install']:
            # Model an interrupted uninstall; failure does not manufacture a
            # successful rollback or restart permission.
            installed = upgrade._installed(python)
            removed = next(Path(f) for f in installed['files'] if f.endswith('__init__.py'))
            removed.unlink()
            return subprocess.CompletedProcess(args, 73, b'', b'TEST interrupted install')
        return original(args, **kwargs)
    monkeypatch.setattr(upgrade.subprocess, 'run', fail_install)
    with pytest.raises(upgrade.PackageUpgradeError, match='backup_retained'):
        upgrade.replace_package(python, wheel, backup, source_quiesced=True, uv=uv)
    saved = json.loads((backup / 'package-upgrade.json').read_text(encoding='utf-8'))
    assert saved['state'] == 'recovery_required' and saved['installer_exit'] == 73
    assert not saved['host_restart_allowed'] and not saved['automatic_rollback']
    assert all((backup / 'files' / item['path']).is_file() for item in saved['files'])


@pytest.mark.skipif(sys.platform != 'win32', reason='real Windows delete-sharing handle')
def test_locked_package_directory_fails_before_uninstall(target, tmp_path):
    import ctypes
    from ctypes import wintypes
    python, wheel, uv = target
    installed = upgrade._installed(python)
    package = next(Path(f).parent for f in installed['files'] if f.endswith('scope_recall\\__init__.py'))
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handle = create(str(package), 0x80000000, 1 | 2, None, 3, 0x02000000, None)
    assert handle != wintypes.HANDLE(-1).value
    backup = tmp_path / 'locked-backup'
    before = {f: hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in installed['files']}
    try:
        with pytest.raises(upgrade.PackageUpgradeError, match='locked_or_not_replaceable'):
            upgrade.replace_package(python, wheel, backup, source_quiesced=True, uv=uv)
    finally:
        close(handle)
    assert not backup.exists()
    assert {f: hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in installed['files']} == before
    assert upgrade._installed(python)['version'] == '0.0.1'


def test_missing_stop_confirmation_and_uv_refuse_before_probe(tmp_path):
    with pytest.raises(upgrade.PackageUpgradeError, match='stop_all_target_writers'):
        upgrade.replace_package(tmp_path / 'missing', tmp_path / 'missing.whl', tmp_path / 'backup')
