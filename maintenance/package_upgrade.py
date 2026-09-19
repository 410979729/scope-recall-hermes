"""Offline wheel replacement after an operator has stopped all target writers.

This is the package step of AGENT_WORKFLOW, not a service controller or release
system. uv owns RECORD uninstall/install (including pip-less venvs). Before uv
can uninstall anything, all installed files are backed up and Windows delete
sharing is checked. On an install/verification failure keep the backup and the
host stopped; do not retry, delete ~* remnants or guess that rollback is safe.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from ..core.file_lock import advisory_file_lock
from .backup import _atomic_json as _write_receipt, _safe_path, _sha256
from .install_common import _safe_interpreter


class PackageUpgradeError(RuntimeError):
    """A package step failed; the reason code contains no subprocess output."""


_PROBE = '''import importlib.metadata as m,json,sys
from pathlib import Path
p=m.distribution('hermes-scope-recall')
print(json.dumps(dict(prefix=str(Path(sys.prefix).resolve()),
 version=p.version, files=[str(Path(p.locate_file(f)).resolve()) for f in p.files])))
'''


def _installed(python: Path) -> dict:
    done = subprocess.run([str(python), '-I', '-B', '-c', _PROBE],
                          capture_output=True, text=True, encoding='utf-8', timeout=30)
    if done.returncode:
        raise PackageUpgradeError('installed_distribution_unavailable')
    return json.loads(done.stdout)


@contextmanager
def _delete_access(paths):
    """Prove every installed file/directory permits rename/delete before uninstall.

    Probe handles are closed before Python backup reads and uv runs; ordinary
    Python readers do not share DELETE. The caller must keep hosts and
    autonomous restarters paused throughout; this is not a process-kill probe.
    """
    if sys.platform != 'win32':
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handles = []
    try:
        for path in paths:
            raw = str(path.resolve())
            if not raw.startswith('\\\\?\\'):
                raw = '\\\\?\\UNC\\' + raw[2:] if raw.startswith('\\\\') else '\\\\?\\' + raw
            # DELETE, share read/write/delete, OPEN_EXISTING, BACKUP_SEMANTICS.
            handle = create(raw, 0x10000, 7, None, 3, 0x02000000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise PackageUpgradeError('installed_files_locked_or_not_replaceable')
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def replace_package(python, wheel, backup, *, source_quiesced=False, uv=None) -> dict:
    """Replace one installed distribution, leaving host activation to the agent.

    ``source_quiesced`` attests all gateway/MCP/worker writers and automatic
    restarters are stopped, not merely idle. No dependency upgrade, model call,
    data migration, service stop/start or wrapper change occurs here.
    """
    if source_quiesced is not True:
        raise PackageUpgradeError('stop_all_target_writers_and_restarters_first')
    # Validate the interpreter's resolved target, then keep executing through
    # the launcher the caller passed: a venv's ``bin/python`` symlink is how
    # CPython finds the venv's ``pyvenv.cfg``, and a probe run with the
    # resolved base interpreter reports the base prefix instead of the venv
    # (observed on Ubuntu's hostedtoolcache layout), which then misjudges the
    # helper's containment. The resolved path is still the safety anchor.
    launcher = Path(python).expanduser()
    if not launcher.is_absolute():
        launcher = Path.cwd() / launcher
    _safe_interpreter(launcher, error_type=PackageUpgradeError)
    python = launcher
    wheel = _safe_path(wheel, must_exist=True, error_type=PackageUpgradeError)
    backup = _safe_path(backup, error_type=PackageUpgradeError)
    # Never silently choose another agent's PATH wrapper. The operator selects
    # an external native uv executable after checking its ownership.
    helper = uv
    if not helper:
        raise PackageUpgradeError('uv_required_no_pip_bootstrap')
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise PackageUpgradeError('wheel_distribution_ambiguous')
        from email.parser import BytesParser
        metadata = BytesParser().parsebytes(archive.read(names[0]))
        if metadata['Name'] != 'hermes-scope-recall' or not metadata['Version']:
            raise PackageUpgradeError('wrong_wheel_distribution')
        target_version = metadata['Version']
    helper = _safe_path(helper, must_exist=True, error_type=PackageUpgradeError)
    if not helper.is_file():
        raise PackageUpgradeError('uv_must_be_external_executable')
    current = _installed(python)
    prefix = _safe_path(current['prefix'], must_exist=True, error_type=PackageUpgradeError)
    if helper.is_relative_to(prefix):
        raise PackageUpgradeError('uv_must_be_outside_target_venv')
    if backup.is_relative_to(prefix) or prefix.is_relative_to(backup) or wheel.is_relative_to(prefix):
        raise PackageUpgradeError('backup_and_wheel_must_be_outside_target_venv')
    files = sorted({_safe_path(f, must_exist=True, error_type=PackageUpgradeError) for f in current['files']})
    if not files or any(not f.is_file() or not f.is_relative_to(prefix) for f in files):
        raise PackageUpgradeError('record_outside_target_venv_or_missing')
    # Include package directory handles, not only files listed in RECORD: the
    # D14 incident involved a directory handle held by the running gateway.
    paths = set(files)
    for path in files:
        paths.update(p for p in path.parents if p != prefix and p.is_relative_to(prefix))
    with advisory_file_lock(prefix / '.scope-recall-package-upgrade.lock', timeout_seconds=0):
        with _delete_access(sorted(paths)):
            pass
        # Close DELETE probes before Python file reads (which do not share
        # DELETE). Hosts/restarters must remain stopped across this boundary.
        backup.mkdir(parents=True, exist_ok=False)
        records = []
        for path in files:
            relative = path.relative_to(prefix)
            destination = backup / 'files' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            digest = _sha256(path)
            if _sha256(destination) != digest:
                raise PackageUpgradeError('backup_verification_failed')
            records.append(dict(path=relative.as_posix(), sha256=digest))
        value = dict(state='backed_up', previous_version=current['version'],
                     target_version=target_version, target_python=str(python),
                     target_prefix=str(prefix), files=records,
                     wheel_sha256=_sha256(wheel),
                     host_restart_allowed=False, automatic_rollback=False)
        receipt = backup / 'package-upgrade.json'
        _write_receipt(receipt, value)
        with _delete_access(sorted(paths)):
            pass  # Recheck immediately before uv may start uninstalling.
        # uv is external to the pip-less target and manages entrypoints/RECORD.
        # Mark intent before invocation: an interrupted run is NOT a safe success.
        value['state'] = 'installing'
        _write_receipt(receipt, value)
        try:
            done = subprocess.run([str(helper), 'pip', 'install', '--python', str(python),
                                   '--no-index', '--no-deps', '--reinstall-package',
                                   'hermes-scope-recall', str(wheel)],
                                  capture_output=True, timeout=180)
            value['installer_exit'] = done.returncode
            if done.returncode:
                raise PackageUpgradeError('uv_install_failed')
            installed = _installed(python)
            if installed['version'] != target_version:
                raise PackageUpgradeError('installed_version_mismatch')
            # Reading metadata alone cannot prove that uninstall left an importable package.
            probe = subprocess.run([str(python), '-I', '-B', '-c',
                                    'import scope_recall; import scope_recall.maintenance.cli'],
                                   capture_output=True, timeout=30)
            if probe.returncode:
                raise PackageUpgradeError('installed_import_failed')
        except Exception as exc:
            value.update(state='recovery_required', error_type=type(exc).__name__,
                         next_action='preserve_backup_keep_hosts_stopped_diagnose')
            _write_receipt(receipt, value)
            raise PackageUpgradeError('package_step_failed_backup_retained') from exc
        value.update(state='package_verified', next_action='apply_install_then_doctor_before_restart')
        _write_receipt(receipt, value)
        return value


def main(argv=None) -> int:
    """Agent-only package stage; explicit target, offline wheel and backup required."""
    parser = argparse.ArgumentParser(prog='scope-recall package-upgrade')
    parser.add_argument('--python', required=True)
    parser.add_argument('--wheel', required=True)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--source-quiesced', action='store_true')
    parser.add_argument('--uv', required=True, help='absolute external native uv executable')
    args = parser.parse_args(argv)
    try:
        value = replace_package(args.python, args.wheel, args.backup,
                                source_quiesced=args.source_quiesced, uv=args.uv)
    except (PackageUpgradeError, OSError, ValueError, TimeoutError, zipfile.BadZipFile) as exc:
        print(json.dumps(dict(state='blocked', error_type=type(exc).__name__,
                              host_restart_allowed=False)))
        return 3
    print(json.dumps({k: v for k, v in value.items() if k != 'files'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
