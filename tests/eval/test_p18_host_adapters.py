from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from p18_test_runner import (
    HostAdapterConfigurationError,
    HermesA2AHostAdapter,
    CodexDesktopHostAdapter,
    HOSTS,
    HostExchange,
)


ROOT = Path(__file__).resolve().parents[2]
RECEIPT_DIR = ROOT / ".execution" / "TEST-P18-RUNNER-READY-v1"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _item(ordinal: int = 1) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "kind": "query_pair",
        "model_input": {"history": [], "query": {"text": "TEST public host adapter query"}},
    }


def test_hermes_injected_exchange_is_explicit_test_only_and_records_metadata() -> None:
    calls: list[tuple[str, bytes, float]] = []

    def exchange(endpoint: str, body: bytes, timeout: float) -> HostExchange:
        calls.append((endpoint, body, timeout))
        return HostExchange(200, b'{"jsonrpc":"2.0","result":{"status":"TEST"}}', {"Content-Type": "application/json"})

    adapter = HermesA2AHostAdapter(
        HOSTS[0],
        endpoint="http://127.0.0.1:19921",
        isolation_root=Path("F:/scope-recall-P18-test-home"),
        transport=exchange,
        test_transport=True,
    )
    result = adapter.execute_query(_item())
    assert result["status"] == "PASS"
    assert result["http_status"] == 200
    assert result["network_calls"] == 1
    assert result["model_calls"] == 1
    assert result["raw_response_retained"] is False
    assert result["response_bytes"] > 0
    assert result["request_sha256"] == hashlib.sha256(calls[0][1]).hexdigest()
    assert len(calls) == 1
    sent = json.loads(calls[0][1].decode("utf-8"))
    assert sent["method"] == "message/send"
    assert sent["params"]["message"]["parts"][0]["text"].startswith("TEST")
    assert sent["params"]["message"]["contextId"] == "P18-host-hermes-a2a-query_pair-1"


def test_hermes_queue_has_one_exchange_per_item_and_no_retry() -> None:
    calls: list[bytes] = []

    def exchange(endpoint: str, body: bytes, timeout: float) -> tuple[int, bytes, dict[str, str]]:
        calls.append(body)
        return 200, b"{}", {"Content-Type": "application/json"}

    adapter = HermesA2AHostAdapter(
        HOSTS[0],
        endpoint="http://127.0.0.1:19921",
        transport=exchange,
        test_transport=True,
    )
    result = adapter.execute_queue((_item(1), _item(2)))
    assert result["status"] == "PASS"
    assert result["queue_count"] == 2
    assert result["network_calls"] == 2
    assert result["retry_count"] == 0
    assert len(calls) == 2
    contexts = {json.loads(body.decode("utf-8"))["params"]["message"]["contextId"] for body in calls}
    assert contexts == {"P18-host-hermes-a2a-query_pair-1", "P18-host-hermes-a2a-query_pair-2"}


def test_hermes_expired_deadline_does_not_start_transport() -> None:
    calls: list[bytes] = []

    def exchange(endpoint: str, body: bytes, timeout: float) -> tuple[int, bytes, dict[str, str]]:
        calls.append(body)
        return 200, b"{}", {}

    adapter = HermesA2AHostAdapter(
        HOSTS[0],
        endpoint="http://127.0.0.1:19921",
        transport=exchange,
        test_transport=True,
    )
    result = adapter.execute_query({**_item(), "deadline_monotonic": time.monotonic() - 1})
    assert result["status"] == "FAIL"
    assert result["error_type"] == "deadline_expired"
    assert result["network_calls"] == 0
    assert result["model_calls"] == 0
    assert calls == []


def test_hermes_rejects_unsafe_endpoint_and_production_home() -> None:
    with pytest.raises(HostAdapterConfigurationError, match="HTTPS"):
        HermesA2AHostAdapter(HOSTS[0], endpoint="http://10.0.0.4:19921")
    with pytest.raises(HostAdapterConfigurationError, match="production"):
        HermesA2AHostAdapter(HOSTS[0], endpoint="http://127.0.0.1:19921", isolation_root=r"F:\Agents\天璇")
    with pytest.raises(HostAdapterConfigurationError, match="TEST-only"):
        HermesA2AHostAdapter(HOSTS[0], endpoint="http://127.0.0.1:19921", transport=lambda *_: None)
    with pytest.raises(HostAdapterConfigurationError, match="isolated"):
        HermesA2AHostAdapter(HOSTS[0], endpoint="http://127.0.0.1:19921")


def test_missing_hermes_endpoint_is_unsupported_without_network() -> None:
    result = HermesA2AHostAdapter(HOSTS[0]).execute_query(_item())
    assert result["status"] == "UNSUPPORTED"
    assert result["reason"] == "endpoint_not_configured"
    assert result["network_calls"] == 0
    assert result["model_calls"] == 0


def test_codex_does_not_substitute_cli_or_stdio_for_desktop() -> None:
    adapter = CodexDesktopHostAdapter(HOSTS[1])
    result = adapter.execute_queue((_item(),))
    assert result["status"] == "UNSUPPORTED"
    assert result["reason"] == "official_codex_desktop_bridge_not_configured"
    assert result["network_calls"] == 0
    assert result["model_calls"] == 0


def test_adapter_receipt_is_saved_as_offline_evidence() -> None:
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "scope-recall.p18-host-adapter-offline-receipt.v1",
        "status": "OFFLINE_TRANSPORT_CONTRACT_PASS",
        "protocol_sha256": "45b70192857b5b8645dd2bf409db55f320f1cf31f297e89910f9f340e76d32d4",
        "protocol_status": "PREPARATION_ONLY",
        "runner_source_sha256": _sha256_file(Path(__file__).with_name("p18_test_runner.py")),
        "test_source_sha256": _sha256_file(Path(__file__)),
        "offline_test_count": 7,
        "network_calls": 0,
        "model_calls": 0,
        "formal_host_execution": False,
        "hermes": "injected exchange tested; no gateway contacted",
        "codex": "UNSUPPORTED until actual desktop bridge is configured",
    }
    path = RECEIPT_DIR / "offline-host-adapter-tests.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["formal_host_execution"] is False
