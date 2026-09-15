import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def worker(directory, create):
    start = time.perf_counter()
    import lancedb
    import pyarrow as pa
    loaded = time.perf_counter()
    db = lancedb.connect(directory / "vectors")
    if create:
        schema = pa.schema([pa.field("id", pa.string()), pa.field("vector", pa.list_(pa.float32(), 2))])
        table = db.create_table("TEST_vectors", data=[{"id":"TEST-A","vector":[1.0,0.0]},{"id":"TEST-B","vector":[-1.0,0.0]}], schema=schema)
    else:
        table = db.open_table("TEST_vectors")
    result = table.search([1.0,0.0]).limit(1).to_list()
    assert result[0]["id"] == "TEST-A"
    print(json.dumps({"lancedb_version":lancedb.__version__,"pyarrow_version":pa.__version__,"native_import_seconds":loaded-start,"query_and_open_seconds":time.perf_counter()-loaded,"synthetic_neighbor":"TEST-A","embedding_api_calls":0,"semantic_evaluation":False}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if args.worker:
        directory = args.worker.resolve()
        if not directory.is_relative_to((ROOT / ".execution").resolve()) or not directory.name.startswith("TEST-P02-native-"):
            raise SystemExit("Native probe requires isolated TEST-P02 directory")
        worker(directory, args.create)
        return
    if not args.python or not args.python.is_file():
        parser.error("--python must identify the dedicated native probe interpreter")
    directory = ROOT / ".execution" / f"TEST-P02-native-{time.time_ns()}"
    directory.mkdir(parents=True)
    env = {k:v for k,v in os.environ.items() if k.upper() in {"SYSTEMROOT","WINDIR","COMSPEC","PATH","PATHEXT"}}
    for key in ("HOME","USERPROFILE","APPDATA","LOCALAPPDATA","TEMP","TMP","HERMES_HOME","CODEX_HOME"):
        target = directory / key.lower()
        target.mkdir()
        env[key] = str(target)
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUTF8="1")
    runs = []
    for number in range(6):
        command = [str(args.python),"-I","-B",str(Path(__file__).resolve()),"--worker",str(directory)]
        if number == 0:
            command.append("--create")
        start = time.perf_counter()
        try:
            result = subprocess.run(command, capture_output=True, env=env, timeout=10)
            detail = json.loads(result.stdout) if result.returncode == 0 else {"error":result.stderr.decode("utf-8",errors="replace")[:4000]}
            runs.append({"command":command,"wall_seconds":time.perf_counter()-start,"exit_code":result.returncode,"includes_fixture_creation":number == 0,**detail})
        except subprocess.TimeoutExpired:
            runs.append({"command":command,"wall_seconds":time.perf_counter()-start,"exit_code":124,"error":"native worker exceeded 10-second probe timeout"})
        if runs[-1]["exit_code"]:
            break
    receipt = {"kind":"isolated_native_import_and_exact_vector_smoke","host_callbacks_executed":False,"model_calls":0,"script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"runs":runs,"valid_runs":sum(x["exit_code"]==0 for x in runs),"median_wall_seconds":statistics.median(x["wall_seconds"] for x in runs),"maximum_wall_seconds":max(x["wall_seconds"] for x in runs),"limits":"Six fresh processes on this machine, OS cache uncontrolled; two manually supplied vectors. No semantic recall, real host startup, throughput or cancellation contract proven."}
    evidence = ROOT / "verification/P02"
    evidence.mkdir(parents=True,exist_ok=True)
    (evidence / f"native-startup-{time.time_ns()}.json").write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(receipt))
    raise SystemExit(0 if len(runs)==6 and all(x["exit_code"]==0 for x in runs) else 1)


if __name__ == "__main__":
    main()
