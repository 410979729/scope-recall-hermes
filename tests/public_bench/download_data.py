"""Explicitly acquire pinned public benchmark data; evaluation never downloads.

The destination must be outside the checkout. Downloaded Python files are
reference text and are never imported or executed by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

from dataset_files import file_records, verify_file

LME_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
LME_CODE_REVISION = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
LOCOMO_REVISION = "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
MAB_REVISION = "7ea066982b140a19337e17e60d45d4076e042faf"
MAB_CODE_REVISION = "538026089d1a8a8eff05121d0db89b388f360eba"


def read_json(url: str) -> dict | list:
    """Read unauthenticated public metadata with a finite network timeout."""
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


def acquire(url: str, destination: Path) -> dict:
    """Download a new file, or require an existing file to match frozen provenance."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = destination.parent / "datasets-manifest.json"
    expected = (
        file_records(json.loads(manifest.read_text(encoding="utf-8"))).get(destination.name)
        if manifest.exists()
        else None
    )
    if destination.exists():
        if expected is None or expected.get("url") != url:
            raise ValueError(f"unverified_existing_file:{destination.name}")
        verify_file(destination, expected)
    if not destination.exists():
        with urllib.request.urlopen(url, timeout=120) as response, destination.open("xb") as target:
            shutil.copyfileobj(response, target, 1024 * 1024)
        if expected is not None:
            verify_file(destination, expected)
    with destination.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"url": url, "file": destination.name, "bytes": destination.stat().st_size, "sha256": digest}


def main() -> None:
    """Acquire the requested datasets and provenance into an explicit data root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    root = args.data.resolve()
    checkout = Path(__file__).resolve().parents[2]
    if root.is_relative_to(checkout):
        parser.error("datasets must remain outside the checkout")
    records = {}
    lme = f"https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{LME_REVISION}"
    records["longmemeval"] = {
        "revision": LME_REVISION,
        "license": "MIT",
        **acquire(f"{lme}/longmemeval_s_cleaned.json", root / "longmemeval_s_cleaned.json"),
    }
    lme_code = f"https://raw.githubusercontent.com/xiaowu0162/LongMemEval/{LME_CODE_REVISION}"
    records["longmemeval_judge"] = {
        "revision": LME_CODE_REVISION,
        **acquire(f"{lme_code}/src/evaluation/evaluate_qa.py", root / "lme-evaluate-qa.py.txt"),
    }
    records["longmemeval_license"] = acquire(f"{lme_code}/LICENSE", root / "lme-LICENSE.txt")
    locomo = f"https://raw.githubusercontent.com/snap-research/locomo/{LOCOMO_REVISION}"
    records["locomo"] = {
        "revision": LOCOMO_REVISION,
        "license": "CC-BY-NC-4.0; local noncommercial smoke only",
        **acquire(f"{locomo}/data/locomo10.json", root / "locomo10.json"),
    }
    records["locomo_readme"] = acquire(f"{locomo}/README.MD", root / "locomo-README.MD")
    records["locomo_eval"] = acquire(f"{locomo}/task_eval/evaluation.py", root / "locomo-evaluation.py.txt")
    tree = read_json(f"https://huggingface.co/api/datasets/ai-hyz/MemoryAgentBench/tree/{MAB_REVISION}?recursive=true")
    paths = [item["path"] for item in tree if "Conflict_Resolution" in item["path"] and item["type"] == "file"]
    records["memoryagentbench"] = {"revision": MAB_REVISION, "license": "MIT", "files": []}
    for path in paths:
        url = f"https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/resolve/{MAB_REVISION}/{path}"
        records["memoryagentbench"]["files"].append(acquire(url, root / Path(path).name))
    code = f"https://raw.githubusercontent.com/HUST-AI-HYZ/MemoryAgentBench/{MAB_CODE_REVISION}"
    records["mab_readme"] = acquire(f"{code}/README.md", root / "mab-README.md")
    # This snapshot is metadata, not a dependency resolver or an installation.
    if not (root / "pypi-releases.json").exists():
        pypi = read_json("https://pypi.org/pypi/hermes-scope-recall/json")
        (root / "pypi-releases.json").write_text(json.dumps(pypi, indent=2), encoding="utf-8")
    (root / "datasets-manifest.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
