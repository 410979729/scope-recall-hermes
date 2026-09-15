"""Offline safety contract for the model-evidence archive helper."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from scripts.extract_model_evidence import EvidenceArchiveError, extract_model_evidence


SHA = "a" * 40


def _archive(tmp_path: Path, *members: tuple[str, str | bytes, int | None]) -> Path:
    path = tmp_path / f"scope-recall-model-evidence-{SHA}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content, mode in members:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, content)
    return path


def test_extracts_commit_bound_bundle_and_receipt(tmp_path: Path) -> None:
    archive = _archive(tmp_path, ("formal-receipt.json", "{}", None), ("metadata/source.txt", "public", None))

    receipt = extract_model_evidence(archive, tmp_path / "bundle", source_sha=SHA)

    assert receipt.read_text(encoding="utf-8") == "{}"
    assert (tmp_path / "bundle" / "metadata" / "source.txt").read_text(encoding="utf-8") == "public"


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([ ("../formal-receipt.json", "{}", None) ], "unsafe"),
        ([ ("formal-receipt.json", "{}", stat.S_IFLNK | 0o777) ], "symlink"),
        ([ ("formal-receipt.json", "{}", None), ("FORMAL-RECEIPT.JSON", "{}", None) ], "duplicate"),
    ],
)
def test_rejects_unsafe_archive_members(tmp_path: Path, members: list[tuple[str, str, int | None]], message: str) -> None:
    archive = _archive(tmp_path, *members)

    with pytest.raises(EvidenceArchiveError, match=message):
        extract_model_evidence(archive, tmp_path / "bundle", source_sha=SHA)


def test_rejects_wrong_commit_name_and_missing_receipt(tmp_path: Path) -> None:
    wrong_name = tmp_path / ("scope-recall-model-evidence-" + "b" * 40 + ".zip")
    with zipfile.ZipFile(wrong_name, "w") as archive:
        archive.writestr("formal-receipt.json", "{}")
    with pytest.raises(EvidenceArchiveError, match="bound"):
        extract_model_evidence(wrong_name, tmp_path / "wrong-bundle", source_sha=SHA)

    missing = _archive(tmp_path, ("metadata.txt", "{}", None))
    with pytest.raises(EvidenceArchiveError, match="missing formal-receipt"):
        extract_model_evidence(missing, tmp_path / "missing-bundle", source_sha=SHA)
