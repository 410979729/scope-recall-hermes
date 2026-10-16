"""The probe rehearsal hands the child the import roots its host imports from.

Hermes with a package-manager environment boots a bundled interpreter and injects the environment it built into
``sys.path`` at run time (``docs/install.md`` §1); nothing puts it on ``PYTHONPATH``.  The in-process rehearsal
spawned ``sys.executable -c "import lancedb, pyarrow"``, which sees only the bundled venv, so on that shape the
rehearsal failed, ``native_modules()`` returned ``None``, and every recall degraded to words-only while capture and
embedding kept working.  The helper path already receives the host's import roots as arguments; the rehearsal child
now receives them on ``PYTHONPATH``.  LanceDB is not needed: the stubs stand in for the real wheels, and they mark
their own import, so a child that imported a real wheel instead of the stubs cannot pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_CHECKOUT = Path(__file__).resolve().parents[2]

_STUB_SOURCE = "from pathlib import Path\nPath(__file__).with_suffix('.imported').write_text('stub')\n"

_DRIVER = """\
import json, sys, types

checkout, stubs = sys.argv[1], sys.argv[2]
package = types.ModuleType("scope_recall")
package.__path__ = [checkout]
sys.modules["scope_recall"] = package
# The PM shape: the environment lands on sys.path at run time, never on PYTHONPATH.
sys.path.insert(0, stubs)
from scope_recall.vector import lance_native

lance_native._native_import_safe = None
print(json.dumps({"rehearsal": lance_native.native_import_is_safe()}))
"""


def _make_stubs(tmp_path):
    stubs = tmp_path / "stubs"
    (stubs / "lancedb").mkdir(parents=True)
    (stubs / "pyarrow").mkdir(parents=True)
    (stubs / "lancedb" / "__init__.py").write_text(_STUB_SOURCE)
    (stubs / "pyarrow" / "__init__.py").write_text(_STUB_SOURCE)
    return stubs


def test_probe_rehearses_with_the_roots_its_host_imports(tmp_path):
    stubs = _make_stubs(tmp_path)
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")}
    completed = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(_CHECKOUT), str(stubs)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    answer = json.loads(completed.stdout.strip().splitlines()[-1])
    assert answer["rehearsal"] is True, "the rehearsal child never saw the host's import roots"
    # The rehearsal must have imported THESE stubs, not a real wheel the interpreter carries.
    assert (stubs / "lancedb" / "__init__.imported").is_file()
    assert (stubs / "pyarrow" / "__init__.imported").is_file()


def test_probe_child_environment_carries_only_the_roots(monkeypatch):
    from scope_recall.vector import lance_native

    root = "/pm/env/site-packages"
    monkeypatch.setattr(lance_native, "helper_import_roots", lambda: [root])
    environment = lance_native._probe_command_environment()
    assert environment is not None
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == root

    # No host roots: the child inherits the environment untouched instead of wiping PYTHONPATH.
    monkeypatch.setattr(lance_native, "helper_import_roots", lambda: [])
    assert lance_native._probe_command_environment() is None

    # An inherited PYTHONPATH keeps its place behind the handed roots.
    monkeypatch.setattr(lance_native, "helper_import_roots", lambda: [root])
    monkeypatch.setenv("PYTHONPATH", "/pre/existing")
    environment = lance_native._probe_command_environment()
    assert environment["PYTHONPATH"].split(os.pathsep) == [root, "/pre/existing"]
