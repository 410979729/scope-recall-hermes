"""Run with -I in a clean environment after installing the locally built wheel."""
from __future__ import annotations

import hashlib
import importlib.abc
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def main() -> None:
    started = time.perf_counter()
    target = Path(sys.argv[1]).resolve()
    if not target.is_absolute() or not target.name.startswith("TEST-") or target.exists():
        raise ValueError("a new absolute TEST directory is required")
    target.mkdir(parents=True)
    allowed_reads = (target, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                     Path(__file__).parent.resolve(), Path(os.environ.get("SystemRoot", "/usr")).resolve())

    class NoHosts(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in {"hermes_cli", "hermes", "gateway", "run_agent", "lancedb", "torch"} or fullname == "scope_recall.provider":
                raise ImportError("standalone core cannot import host/native Provider runtime")

    def audit(event, args):
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2"}:
            raise PermissionError("TEST network and process calls disabled")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            mode, flags = args[1:3]
            write = isinstance(mode, str) and any(c in mode for c in "wax+")
            write |= isinstance(flags, int) and bool(flags & (os.O_RDWR | os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if write and not path.is_relative_to(target):
                raise PermissionError("TEST write outside target")
            if not any(path.is_relative_to(root) for root in allowed_reads):
                raise PermissionError("TEST read outside package/runtime/target")
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            if not Path(args[0]).resolve().is_relative_to(target):
                raise PermissionError("TEST mutation outside target")

    assert importlib.util.find_spec("hermes_cli") is None
    sys.meta_path.insert(0, NoHosts())
    sys.addaudithook(audit)
    from scope_recall.core import CoreConfig, MemoryCore
    from scope_recall.contracts import ContractError, InstanceBinding, TrustedContext
    from scope_recall.core.schema import SCHEMA_VERSION

    binding = InstanceBinding("TEST-package-agent", "TEST-package-install", target / "TEST-data", frozenset({"TEST-private", "TEST-group"}), True)
    core = MemoryCore(CoreConfig(binding))
    checks = {"import_and_construction_no_database": not binding.data_directory.exists(),
              "imported_from_installed_package": Path(sys.modules["scope_recall.core"].__file__).resolve().is_relative_to(Path(sys.prefix).resolve())}
    assert all(checks.values())
    checks["explicit_initialization"] = core.initialize().schema_version == SCHEMA_VERSION
    private = TrustedContext(binding, "TEST-session", frozenset({"TEST-private"}), "human_direct")
    group = TrustedContext(binding, "TEST-session", frozenset({"TEST-group"}), "human_direct")
    event = dict(protocol_version="1.1", source_event_key="TEST-package/message1", source_revision=1,
                 origin="human_direct", role="user", content="TEST 新目标库保留原文。", occurred_at=None,
                 recorded_at="2026-09-06T06:00:00Z", time_precision="unknown", capture_state="complete",
                 evidence_refs=[], dataset_id="SYNTHETIC_TEST_ONLY")
    with core.storage.write(private) as tx:
        row = tx.put_source(event, scope_id="TEST-private", persisted_at=event["recorded_at"])
        tx.enqueue_source(row.ref, 1, work_type="consolidate", available_at=event["recorded_at"])
    checks["exact_persisted_source"] = core.source(private, row.ref, 1).event == event
    checks["group_cannot_read_private"] = core.source(group, row.ref, 1) is None
    checks["source_and_work_committed"] = core.status(private).sources == core.status(private).pending_work == 1
    before = core.storage.path.read_bytes()
    core.status(private)
    core.source(private, row.ref, 1)
    checks["queries_leave_database_bytes_unchanged"] = core.storage.path.read_bytes() == before
    try:
        with core.storage.write(private) as tx:
            tx.put_source({**event, "source_event_key": "TEST-package/rollback"}, scope_id="TEST-private", persisted_at=event["recorded_at"])
            raise RuntimeError("TEST rollback")
    except RuntimeError:
        pass
    checks["rollback_retains_only_committed_source"] = core.status(private).sources == 1
    checks["no_host_provider_native_imports"] = not any(n.startswith(("hermes_cli", "gateway", "run_agent", "lancedb", "torch", "scope_recall.provider")) for n in sys.modules)
    assert all(checks.values()), checks
    print(json.dumps(dict(checks=checks, passed=sum(checks.values()), failed=0, skipped=0,
                         duration_seconds=time.perf_counter()-started,
                         schema_version=SCHEMA_VERSION, python=sys.version,
                         distributions={d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
                         database_sha256=hashlib.sha256(before).hexdigest(),
                         evidence_scope="Installed intermediate wheel, no Hermes, no network/model calls; storage only"), ensure_ascii=False))


if __name__ == "__main__":
    main()
