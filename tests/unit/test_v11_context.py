from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from scope_recall.contracts import ArtifactVersion, ContractError, DisplaySnapshot, validate_capture, validate_model_request
from v11_support import FixedInputs, context, recall_request, source_event


@pytest.mark.parametrize("field", ["database_path", "agent_id", "installation_id", "allowed_scope_ids", "actor_origin", "sql", "scope"])
def test_model_cannot_supply_trusted_identity(tmp_path, field):
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        validate_model_request("recall_request", recall_request(**{field: "TEST-forged"}), context(tmp_path))


def test_scope_can_only_narrow_installation_binding(tmp_path):
    ctx = context(tmp_path)
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        replace(ctx, allowed_scope_ids=frozenset({"TEST-other-agent"}))
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        validate_model_request("recall_request", recall_request(), replace(ctx, allowed_scope_ids=frozenset()))
    with pytest.raises(FrozenInstanceError):
        ctx.session_id = "changed"


@pytest.mark.parametrize("origin", ["assistant_visible", "tool_observation", "host_generated", "memory_reinjection", "origin_unknown"])
def test_user_role_does_not_make_human_origin(tmp_path, origin):
    ctx = context(tmp_path, origin)
    event = validate_capture(source_event(role="user", origin=origin), ctx)
    assert event["origin"] == origin
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        validate_capture(source_event(origin="human_direct"), ctx)


def test_model_cannot_call_capture_schema(tmp_path):
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        validate_model_request("source_event", source_event(), context(tmp_path))


def test_test_dataset_requires_isolated_installation(tmp_path):
    event = source_event(dataset_id="SYNTHETIC_TEST_ONLY")
    with pytest.raises(ContractError, match="dataset_id"):
        validate_capture(event, context(tmp_path, test_mode=False))
    assert validate_capture(event, context(tmp_path))["dataset_id"] == "SYNTHETIC_TEST_ONLY"


@pytest.mark.parametrize("changes", [dict(agent_id=""), dict(data_directory=Path("relative")), dict(scope_ids={"mutable"}), dict(test_mode=1)])
def test_invalid_installation_is_not_bound(tmp_path, changes):
    with pytest.raises(ContractError, match="IDENTITY_UNBOUND"):
        replace(context(tmp_path).binding, **changes)


def test_context_is_bounded(tmp_path):
    with pytest.raises(ContractError, match="context_budget"):
        replace(context(tmp_path), recent_messages=("x",) * 9)
    with pytest.raises(ContractError, match="display_snapshot"):
        DisplaySnapshot("observed", (ArtifactVersion("TEST-artifact", 1),) * 33)


def test_model_cannot_upgrade_unknown_display_order(tmp_path):
    snapshot = DisplaySnapshot("unknown", (ArtifactVersion("TEST-A", 1), ArtifactVersion("TEST-B", 2)))
    ctx = replace(context(tmp_path), display_snapshot=snapshot)
    event = source_event(artifact_refs=["TEST-A", "TEST-B"], display_snapshot=snapshot.to_payload())
    assert validate_capture(event, ctx)
    event["display_snapshot"]["order"] = "observed"
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        validate_capture(event, ctx)
    observed = replace(snapshot, order="observed")
    assert validate_capture(event, replace(ctx, display_snapshot=observed))
    event["display_snapshot"]["items"].reverse()
    with pytest.raises(ContractError, match="ACCESS_DENIED"):
        validate_capture(event, replace(ctx, display_snapshot=observed))


def test_fixed_clock_and_ids_are_explicit():
    first, second = FixedInputs(), FixedInputs()
    assert first.time() == second.time() == "2026-09-05T12:00:00+00:00"
    assert first.next_id() == second.next_id() == "TEST-generated-1"
    assert first.next_id() == "TEST-generated-2"


def test_runner_isolates_home_and_blocks_external_effects(tmp_path):
    isolated = Path(os.environ["SCOPE_RECALL_TEST_BOUNDARY_PARENT"])
    for name in ("HOME", "USERPROFILE", "HERMES_HOME", "APPDATA"):
        assert Path(os.environ[name]).is_relative_to(isolated)
    assert not any("API_KEY" in key or "TOKEN" in key for key in os.environ)
    allow_loopback = os.environ.get("SCOPE_RECALL_TEST_ALLOW_LOOPBACK") == "1"
    allow_children = os.environ.get("SCOPE_RECALL_TEST_ALLOW_OWNED_SUBPROCESSES") == "1"
    if allow_loopback:
        socket.socket()
        with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
            socket.socket().connect(("8.8.8.8", 53))
    else:
        with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
            socket.socket()
    if allow_children:
        completed = subprocess.run([sys.executable, "-c", "pass"], check=False, capture_output=True, text=True)
        assert completed.returncode == 0
        with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
            subprocess.run([str(Path(os.environ["SCOPE_RECALL_TEST_PROTECTED_HOME"]) / "python.exe"), "-c", "pass"])
    else:
        with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
            subprocess.run([sys.executable, "-c", "pass"])
    protected = Path(os.environ["SCOPE_RECALL_TEST_PROTECTED_HOME"])
    with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
        (protected / "TEST-do-not-read").read_text()
    with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
        (protected / "TEST-do-not-write").write_text("synthetic", encoding="utf-8")
    marker = tmp_path / "TEST-marker.txt"
    marker.write_text("synthetic", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "synthetic"


def test_packaging_helper_roots_follow_declared_tier(tmp_path):
    from v11_guard import _PACKAGING_HELPER_ROOTS, _PROCESS_TIER, _check_owned_child_process, _is_allowed_child_path

    # A path outside every allowed root on THIS platform: Windows drive
    # letters are absolute only on Windows, and POSIX treats "D:/..." as a
    # relative name that then resolves under the checkout, so the fixture
    # must anchor the foreign path per platform.
    if os.name == "nt":
        foreign = str(Path("D:/not-an-authorized-helper/uv.exe"))
    else:
        foreign = str(Path("/opt/not-an-authorized-helper/uv"))
    assert _is_allowed_child_path(foreign) is False
    with pytest.raises(PermissionError, match="TEST_BOUNDARY"):
        _check_owned_child_process((None, f"{foreign} build --wheel", str(tmp_path), None))
    if _PROCESS_TIER in {"packaging", "release"}:
        assert _PACKAGING_HELPER_ROOTS
        helper = next(iter(_PACKAGING_HELPER_ROOTS))
        authorized = helper / ("uv.exe" if os.name == "nt" else "uv")
        assert _is_allowed_child_path(str(authorized)) is True
    else:
        if _PACKAGING_HELPER_ROOTS:
            helper = next(iter(_PACKAGING_HELPER_ROOTS))
            assert _is_allowed_child_path(str(helper / "uv.exe")) is False


def test_windows_extended_paths_are_normalized_without_expanding_test_authority(tmp_path):
    if os.name!='nt':pytest.skip('Windows namespace contract')
    from v11_guard import _check_path
    marker=tmp_path/'TEST-extended.txt'
    native=Path('\\\\?\\'+str(marker))
    native.write_text('TEST same authorized location',encoding='utf-8')
    assert marker.read_text(encoding='utf-8')=='TEST same authorized location'
    protected=Path(os.environ['SCOPE_RECALL_TEST_PROTECTED_HOME'])/'TEST-no-read'
    with pytest.raises(PermissionError,match='TEST_BOUNDARY'):
        Path('\\\\?\\'+str(protected)).read_bytes()
    for device in ('\\\\.\\PhysicalDrive0','\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1'):
        with pytest.raises(PermissionError,match='TEST_BOUNDARY'):_check_path(device)
