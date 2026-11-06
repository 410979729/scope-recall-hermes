"""Exercise Windows ACL copying without changing the machine's security descriptors."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest
from scope_recall.core import file_access
from scope_recall.maintenance import backup


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.mark.parametrize("owners", [[101, 101], [101, 202, 101]])
def test_dacl_copy_does_not_require_write_owner(tmp_path, monkeypatch, owners):
    calls = []
    remaining = iter(owners)

    def read(_path, _kind, _flags, owner, _group, acl, _sacl, descriptor):
        ctypes.cast(owner, ctypes.POINTER(ctypes.c_void_p))[0] = next(remaining)
        if acl:
            ctypes.cast(acl, ctypes.POINTER(ctypes.c_void_p))[0] = 303
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 404
        return 0

    def control(_descriptor, value, _revision):
        ctypes.cast(value, ctypes.POINTER(ctypes.c_ushort))[0] = 0x1000
        return True

    def write(_path, _kind, flags, *_args):
        calls.append(flags)
        return 5 if flags & 1 else 0

    api = SimpleNamespace(
        GetNamedSecurityInfoW=Function(read),
        GetSecurityDescriptorControl=Function(control),
        SetNamedSecurityInfoW=Function(write),
        EqualSid=Function(lambda a, b: a.value == b.value),
    )
    kernel = SimpleNamespace(LocalFree=Function(lambda _: None))
    monkeypatch.setattr(file_access, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: api if name == "advapi32" else kernel, raising=False)
    file_access.preserve_file_access(tmp_path / "source", tmp_path / "target")
    assert calls[-1] == 4 | 0x80000000
    assert len(calls) == (1 if len(owners) == 2 else 2)


def test_backup_failure_removes_only_its_empty_output(tmp_path, monkeypatch):
    source = tmp_path / "config.yaml"
    source.write_bytes(b"TEST configuration\r\n")
    monkeypatch.setattr(backup, "preserve_file_access", lambda *a: (_ for _ in ()).throw(PermissionError(5)))
    with pytest.raises(PermissionError):
        backup.adjacent_backup(source)
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == b"TEST configuration\r\n"
