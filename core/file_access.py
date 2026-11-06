"""Preserve configuration access before any bytes are copied into a replacement file."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


def preserve_file_access(source: Path, target: Path) -> None:
    """Keep the source owner and DACL on Windows; restrict POSIX copies to their owner.

    Both paths are existing files in the same protected directory. Call this
    while the new target is still empty. Failure must abort the write rather
    than publish a copy under broader inherited permissions.
    """
    if os.name != "nt":
        os.chmod(target, 0o600)
        return
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    api.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        pointer,
        ctypes.POINTER(pointer),
        pointer,
        ctypes.POINTER(pointer),
    ]
    api.GetNamedSecurityInfoW.restype = wintypes.DWORD
    api.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    api.GetSecurityDescriptorControl.restype = wintypes.BOOL
    api.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    api.SetNamedSecurityInfoW.restype = wintypes.DWORD
    api.EqualSid.argtypes = [pointer, pointer]
    api.EqualSid.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    owner, acl, descriptor = pointer(), pointer(), pointer()
    status = api.GetNamedSecurityInfoW(
        ctypes.create_unicode_buffer(str(source)),
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(acl),
        None,
        ctypes.byref(descriptor),
    )
    if status:
        raise ctypes.WinError(status)
    try:

        def same_owner() -> bool:
            """Compare actual SIDs; an already matching owner needs no WRITE_OWNER permission."""
            target_owner, target_descriptor = pointer(), pointer()
            result = api.GetNamedSecurityInfoW(
                ctypes.create_unicode_buffer(str(target)),
                1,
                1,
                ctypes.byref(target_owner),
                None,
                None,
                None,
                ctypes.byref(target_descriptor),
            )
            if result:
                raise ctypes.WinError(result)
            try:
                return bool(api.EqualSid(owner, target_owner))
            finally:
                kernel.LocalFree(target_descriptor)

        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Preserve protected versus inherited DACL semantics, not just its ACEs.
        inheritance = 0x80000000 if control.value & 0x1000 else 0x20000000
        flags = 4 if same_owner() else 5
        status = api.SetNamedSecurityInfoW(
            ctypes.create_unicode_buffer(str(target)),
            1,
            flags | inheritance,
            owner if flags & 1 else None,
            None,
            acl,
            None,
        )
        if status == 5 and flags & 1 and same_owner():
            status = api.SetNamedSecurityInfoW(
                ctypes.create_unicode_buffer(str(target)), 1, 4 | inheritance, None, None, acl, None
            )
        if status:
            raise ctypes.WinError(status)
    finally:
        kernel.LocalFree(descriptor)
