"""Measure a health page against the frozen synthetic source and fact distribution."""

import json
import sys
import time
from pathlib import Path
from threading import Thread

from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.maintenance.ui_api import UIApi
from scope_recall.maintenance.ui_http import UIServer

from tests.host.test_memory_ui_http import send


def test_health_first_page_at_frozen_scale(tmp_path, record_property):
    sys.path.insert(0, str(Path(__file__).parents[1] / "public_bench"))
    from cache import RequestCache
    from lab import Lab
    from performance import NOW, SCOPES, seed_truth
    from ports import Ports

    ports = Ports(RequestCache(tmp_path / "cache.sqlite3"))
    lab = Lab(tmp_path / "health-scale", ports, now=NOW, backend="sqlite", scopes=SCOPES)
    try:
        seed_truth(lab, 350000)
        api = UIApi(MemoryManagement(lab.core, InstallationMaintenance(lab.context)))
        with UIServer(api) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                started = time.perf_counter()
                status, _, raw = send(server, "/api/health")
                elapsed = (time.perf_counter() - started) * 1000
                assert status == 200
                result = json.loads(raw)["data"]
                assert "complete" in result
                record_property("first_page_ms", elapsed)
                record_property("complete", str(result["complete"]))
                record_property("source_count", 350000)
                record_property("fact_count", 35000)
            finally:
                server.shutdown()
                thread.join(6)
                assert not thread.is_alive()
    finally:
        lab.close()
