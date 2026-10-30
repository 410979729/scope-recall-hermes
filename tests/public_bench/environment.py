"""Interpreter, SQLite and machine facts recorded beside benchmark results."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def sqlite_probe() -> dict:
    """Probe an in-memory connection only, enabling and then disabling extensions."""
    with sqlite3.connect(":memory:") as connection:
        options = [row[0] for row in connection.execute("PRAGMA compile_options")]
        try:
            connection.enable_load_extension(True)
            connection.enable_load_extension(False)
            extension, error = True, None
        except (AttributeError, sqlite3.Error) as exc:
            extension, error = False, type(exc).__name__
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "enable_load_extension": extension,
        "extension_error": error,
        "compile_options": options,
    }


def machine(data_dir: Path) -> dict:
    """Read hardware through nonprivileged APIs, without querying other processes.

    Disk figures are for the volume holding ``data_dir``, which must exist.
    """
    result = {
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "disk": dict(zip(("total", "used", "free"), shutil.disk_usage(data_dir), strict=True)),
        "observed_at": datetime.now().astimezone().isoformat(),
    }
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            """Windows MEMORYSTATUSEX layout, including physical memory totals."""

            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total_phys",
                    "avail_phys",
                    "total_page",
                    "avail_page",
                    "total_virtual",
                    "avail_virtual",
                    "avail_extended",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            result["physical_memory_bytes"] = status.total_phys
    return result
