import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.retained_artifacts import (
    ArtifactGrant, RetainedBlob,
    erase_retained, read_retained, retain,
)
from v11_support import context


def _grant(path: Path, media_type: str = "image/png", limit: int | None = None):
    data = path.read_bytes()
    return ArtifactGrant(path, hashlib.sha256(data).hexdigest(), media_type, len(data) if limit is None else limit)


PNG_V1 = b"\x89PNG\r\n\x1a\nTEST-v1"
PNG_V2 = b"\x89PNG\r\n\x1a\nTEST-v2"
WEBP = b"RIFF\x04\x00\x00\x00WEBP"


def _io_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    return Path("\\\\?\\" + value) if not value.startswith("\\\\") else Path("\\\\?\\UNC\\" + value[2:])


def test_same_name_versions_are_content_addressed_and_immutable(tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    source = tmp_path / "design.png"
    source.write_bytes(PNG_V1)
    first = retain(ctx.binding, _grant(source))
    source.write_bytes(PNG_V2)
    second = retain(ctx.binding, _grant(source))
    assert first != second
    assert read_retained(ctx.binding, first) == PNG_V1
    assert read_retained(ctx.binding, second) == PNG_V2
    assert first.relative_path.startswith("retained/") and not str(ctx.binding.data_directory) in first.relative_path
    assert str(source) not in repr(first)


def test_mime_size_hash_and_svg_policy(tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    source = tmp_path / "a.txt"
    source.write_bytes(b"x")
    with pytest.raises(ContractError):
        retain(ctx.binding, ArtifactGrant(source, hashlib.sha256(b"x").hexdigest(), "application/pdf", 1))
    with pytest.raises(ContractError):
        retain(ctx.binding, ArtifactGrant(source, "0" * 64, "text/plain", 1))
    with pytest.raises(ContractError):
        retain(ctx.binding, ArtifactGrant(source, hashlib.sha256(b"x").hexdigest(), "text/plain", 0))
    source.write_bytes(b"xx")
    with pytest.raises(ContractError):
        retain(ctx.binding, _grant(source, "text/plain", limit=1))
    svg = tmp_path / "bad.svg"
    svg.write_text('<svg><script>alert(1)</script></svg>', encoding="utf-8")
    with pytest.raises(ContractError):
        retain(ctx.binding, _grant(svg, "image/svg+xml"))
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1" fill="#fff"/></svg>', encoding="utf-8")
    valid = retain(ctx.binding, _grant(svg, "image/svg+xml"))
    assert read_retained(ctx.binding, valid).startswith(b"<svg")
    svg.write_text('<svg><style>@import "https://example.invalid/a.css";</style></svg>', encoding="utf-8")
    with pytest.raises(ContractError):
        retain(ctx.binding, _grant(svg, "image/svg+xml"))
    svg.write_text('<svg><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///secret">]></svg>', encoding="utf-8")
    with pytest.raises(ContractError):
        retain(ctx.binding, _grant(svg, "image/svg+xml"))


def test_read_detects_tamper_missing_and_erase_is_exact(tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    source = tmp_path / "a.webp"
    source.write_bytes(WEBP)
    blob = retain(ctx.binding, _grant(source, "image/webp"))
    retained = ctx.binding.data_directory / blob.relative_path
    _io_path(retained).write_bytes(b"tampered")
    with pytest.raises(ContractError):
        read_retained(ctx.binding, blob)
    _io_path(retained).write_bytes(WEBP)
    assert read_retained(ctx.binding, blob) == WEBP
    erase_retained(ctx.binding, blob)
    assert not retained.exists()
    with pytest.raises(ContractError):
        read_retained(ctx.binding, blob)
    erase_retained(ctx.binding, blob)


def test_links_and_out_of_root_blob_are_rejected(tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    outside_dir = tmp_path / "TEST-outside"
    outside_dir.mkdir()
    outside = outside_dir / "a.png"
    outside.write_bytes(PNG_V1)
    link = tmp_path / "link.png"
    junction = None
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("symlinks unavailable")
        import _winapi
        junction = tmp_path / "TEST-link-parent"
        _winapi.CreateJunction(str(outside_dir), str(junction))
        link = junction / outside.name
    try:
        with pytest.raises(ContractError, match="IDENTITY_UNBOUND"):
            retain(ctx.binding, _grant(link))
        with pytest.raises(ContractError):
            read_retained(ctx.binding, RetainedBlob(hashlib.sha256(b"x").hexdigest(), 1, "image/png", "../outside"))
        assert outside.read_bytes() == PNG_V1
    finally:
        if junction is not None:
            junction.rmdir()


def test_hardlink_mutation_is_detected_and_fsync_failure_leaves_no_partial(monkeypatch, tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    source = tmp_path / "a.png"
    source.write_bytes(PNG_V1)
    blob = retain(ctx.binding, _grant(source))
    retained = ctx.binding.data_directory / blob.relative_path
    alias = tmp_path / "hardlink.png"
    try:
        os.link(_io_path(retained), _io_path(alias))
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    alias.write_bytes(b"BAD")
    with pytest.raises(ContractError):
        read_retained(ctx.binding, blob)

    source.write_bytes(PNG_V2)
    import scope_recall.core.retained_artifacts as artifacts
    monkeypatch.setattr(artifacts.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
    new_blob = _grant(source)
    with pytest.raises(OSError, match="fsync"):
        retain(ctx.binding, new_blob)
    assert not (ctx.binding.data_directory / "retained" / new_blob.sha256).exists()


def test_windows_junction_ancestor_is_rejected(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    import _winapi
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "junction"
    try:
        _winapi.CreateJunction(str(target), str(alias))
    except OSError as exc:
        pytest.skip(f"junction unavailable: {exc}")
    try:
        ctx = context(alias)
        source = tmp_path / "a.png"
        source.write_bytes(PNG_V1)
        with pytest.raises(ContractError):
            retain(ctx.binding, _grant(source))
    finally:
        alias.rmdir()


def test_blob_metadata_does_not_echo_text_contents(tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    source = tmp_path / "notes.txt"
    source.write_text("TEST-secret-content", encoding="utf-8")
    blob = retain(ctx.binding, _grant(source, "text/plain"))
    assert "TEST-secret-content" not in repr(blob)
    assert read_retained(ctx.binding, blob) == b"TEST-secret-content"
    source.write_text("api_key=sk-abcdefghijklmnopqrstuvwxyz123456", encoding="utf-8")
    with pytest.raises(ContractError):
        retain(ctx.binding, _grant(source, "text/plain"))


def test_blob_identity_is_bound_to_installation_and_agent(tmp_path):
    ctx = context(tmp_path / "TEST-instance")
    source = tmp_path / "a.png"
    source.write_bytes(PNG_V1)
    blob = retain(ctx.binding, _grant(source))
    other = replace(ctx.binding, installation_id="OTHER-installation", agent_id="OTHER-agent")
    with pytest.raises(ContractError):
        read_retained(other, blob)
