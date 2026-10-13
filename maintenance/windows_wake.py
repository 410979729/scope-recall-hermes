"""Build a scheduled wake whose first executable cannot allocate a console.

A venv's pythonw may be a console-subsystem redirector. A real GUI interpreter
starts that venv under CREATE_NO_WINDOW; the venv still owns package imports.
No task registration or environment mutation occurs while preparing the action.
"""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

# Static code, not shell text: all operator paths remain separate argv entries.
# The GUI parent waits so Task Scheduler sees the wake's exit code. The bounded
# child ends before the task's one-minute execution limit, including on timeout.
_HIDDEN_WAKE = (
    "import subprocess,sys; "
    "sys.exit(subprocess.run(sys.argv[1:],stdin=subprocess.DEVNULL,"
    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
    "creationflags=subprocess.CREATE_NO_WINDOW,timeout=50).returncode)"
)
_BASE_PROBE = "import json,sys; print(json.dumps(sys._base_executable))"


def is_gui_executable(path: Path) -> bool:
    """Read the PE subsystem rather than trusting an executable's filename."""
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            stream.seek(struct.unpack_from("<I", header, 60)[0])
            pe = stream.read(96)
        return (
            len(pe) == 96
            and pe[:4] == b"PE\0\0"
            and struct.unpack_from("<H", pe, 24)[0] in (0x10B, 0x20B)
            and struct.unpack_from("<H", pe, 92)[0] == 2
        )
    except (OSError, ValueError, OverflowError):
        return False


def wake_command(python: Path, config_path: Path) -> list[str]:
    """Keep the selected venv and refuse a Windows wake with no GUI launcher."""
    arguments = ["-I", "-B", "-m", "scope_recall.runtime.resume_entry", "--config", str(config_path)]
    gui = python.with_name("pythonw.exe")
    if is_gui_executable(gui):
        return [str(gui), *arguments]
    try:
        probe = subprocess.run(
            [str(python), "-I", "-B", "-c", _BASE_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
        base = json.loads(probe.stdout)
        if not isinstance(base, str) or not Path(base).is_absolute():
            raise ValueError("invalid_base_interpreter")
        gui = Path(base).with_name("pythonw.exe")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError("autostart_gui_interpreter_unavailable") from exc
    if not is_gui_executable(gui):
        # Never fall back to a console executable: that recreates the flash.
        raise ValueError("autostart_gui_interpreter_unavailable")
    return [str(gui), "-I", "-B", "-c", _HIDDEN_WAKE, str(python), *arguments]
