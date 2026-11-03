"""One candidate and physical-core budget for mapped vector search."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from functools import cache
from pathlib import Path

MIN_CANDIDATES = 1024

# One publication amortizes file open/fsync work across small scope partitions,
# while keeping the largest supported raw matrix below a bounded worker batch.
INDEX_PAGE_ROWS = 8192
INDEX_BATCH_ROWS = 262144


def _physical_cores() -> int:
    """Read physical topology with platform APIs; an unknown topology stays serial."""
    try:
        if os.name == "nt":
            size = ctypes.c_ulong()
            query = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
            query(0, None, ctypes.byref(size))
            data = ctypes.create_string_buffer(size.value)
            if not query(0, data, ctypes.byref(size)):
                return 1
            offset = count = 0
            while offset < size.value:
                relation, width = struct.unpack_from("<II", data, offset)
                if width < 8:
                    return 1
                count += relation == 0
                offset += width
            return max(1, count)
        if sys.platform == "darwin":
            count, size = ctypes.c_uint(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_uint))
            result = ctypes.CDLL(None).sysctlbyname(b"hw.physicalcpu", ctypes.byref(count), ctypes.byref(size), None, 0)
            return max(1, count.value) if result == 0 else 1
        topology = Path("/sys/devices/system/cpu")
        cores = {
            ((cpu / "topology/physical_package_id").read_text(), (cpu / "topology/core_id").read_text())
            for cpu in topology.glob("cpu[0-9]*")
        }
        return max(1, len(cores))
    except (OSError, ValueError, AttributeError, struct.error):
        return 1


@cache
def compute_threads() -> int:
    """Bound numerical scan workers by four and by the host's physical cores."""
    return min(4, _physical_cores())
