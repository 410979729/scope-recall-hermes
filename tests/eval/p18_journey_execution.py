"""Execute private J fixtures through real host turns and local TEST controls.

No fixture text or expected answer lives in this module. Host turns must use
the existing formal runner; controls retain their own artifacts and do not
claim a model turn or semantic PASS. Unsupported baseline controls stay
unsupported instead of borrowing the candidate's Core.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Mapping


class JourneyExecutionError(ValueError):
    pass


_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}\Z")


def _jsonable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _save(path: Path, value) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(_jsonable(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise JourneyExecutionError("path_outside_TEST_root")
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise JourneyExecutionError("linked_TEST_path")
    return path


def load_artifact(root: Path, ref: Mapping[str, str]) -> Path:
    path = _inside(root, ref["path"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
        raise JourneyExecutionError("private_input_artifact_hash_mismatch")
    return path


def load_journey(bundle_path: Path, journey_id: str) -> tuple[Path, dict]:
    root = bundle_path.resolve().parent
    value = json.loads(bundle_path.read_text(encoding="utf-8"))
    if value.get("schema") != "scope-recall.p18-private-journey-execution.v1":
        raise JourneyExecutionError("journey_schema")
    matches = [j for j in value["journeys"] if j["journey_id"] == journey_id]
    if len(matches) != 1:
        raise JourneyExecutionError("journey_id")
    journey = matches[0]
    actions = journey["actions"]
    ids = [a["operation_id"] for a in actions]
    if any(type(v) is not str or not _OPERATION.fullmatch(v) for v in ids):
        raise JourneyExecutionError("operation_id_invalid")
    if len(ids) != len(set(ids)):
        raise JourneyExecutionError("duplicate_operation")
    turns = [a for a in actions if a["kind"] == "host_turn"]
    if not 1 <= len(turns) <= 8 or [a["primary_round_ordinal"] for a in turns] != list(range(1, len(turns) + 1)):
        raise JourneyExecutionError("journey_round_budget")
    if {n for a in actions for n in a["source_step_orders"]} != set(range(1, 7)):
        raise JourneyExecutionError("journey_step_coverage")
    for action in turns:
        model_input = json.loads(load_artifact(root, action["parameters"]["input"]).read_text(encoding="utf-8"))
        if set(model_input) != {"query", "attachments"} or not isinstance(model_input["query"], str) or not model_input["query"].strip():
            raise JourneyExecutionError("natural_input_only")
        for attachment in model_input["attachments"]:
            load_artifact(root, attachment["asset"])
    for action in actions:
        if action["kind"] == "assert_evidence":
            load_artifact(root, action["parameters"]["criteria"])
    return root, journey


class CoreJourneyControls:
    """Existing Core/maintenance calls in the one explicitly bound TEST arm.

    ``operator_context`` is a local installation-operator capability provided
    by the executor; it must never be derived from an A2A role or model text.
    The host owner supplies quiesce/resume so restore cannot race its workers.
    """

    def __init__(self, *, runtime, operator_context, scope_id: str, arm_root: Path,
                 workspace: Path, evidence_root: Path, host):
        self.runtime = runtime
        self.core = runtime.core if runtime is not None else None
        self.context = operator_context
        self.scope_id = scope_id
        self.arm_root = arm_root.resolve()
        self.workspace = workspace.resolve()
        self.evidence_root = evidence_root.resolve()
        self.host = host
        self.observations: dict[str, dict] = {}
        self._lock_connection = None
        self._journal_mode = None
        self._held = None
        self._worker_lock = None
        self._ledger = None
        self._snapshots: dict[str, Path] = {}
        self._readonly_sids: dict[Path, str] = {}
        for path in (self.workspace, self.evidence_root):
            if not path.is_relative_to(self.arm_root) or "test" not in str(self.arm_root).lower():
                raise JourneyExecutionError("isolated_TEST_arm_required")
            path.mkdir(parents=True, exist_ok=True)
        if self.core is not None:
            if (not operator_context.binding.test_mode
                    or operator_context.binding != self.core.config.binding
                    or scope_id not in operator_context.allowed_scope_ids
                    or not operator_context.binding.data_directory.resolve().is_relative_to(self.arm_root)):
                raise JourneyExecutionError("operator_binding_mismatch")

    def _require_core(self):
        if self.core is None:
            raise JourneyExecutionError("UNSUPPORTED:arm_has_no_Core_control_surface")
        return self.core

    def _source_refs(self, operation_id: str) -> tuple[str, ...]:
        refs = self.observations.get(operation_id, {}).get("source_refs")
        if not isinstance(refs, (list, tuple)) or not refs:
            raise JourneyExecutionError("actual_source_refs_missing")
        return tuple(refs)

    def _source(self, ref: str):
        from scope_recall.core.claim_storage import parse_source_ref
        return self.core.source(self.context, *parse_source_ref(ref))

    def _capture(self, text: str, key: str, *, origin: str, refs=(), session_id=None):
        context = replace(self.context, actor_origin=origin, recent_messages=())
        if session_id is not None:
            context = replace(context, session_id=session_id)
        now = self.core.clock.utc_now()
        receipt = self.core.record_event(context, {
            "protocol_version": "1.1", "source_event_key": key, "source_revision": 1,
            "origin": origin, "role": "document" if origin == "external_document" else "user" if origin == "human_direct" else "assistant",
            "content": text, "occurred_at": now, "recorded_at": now,
            "time_precision": "instant", "capture_state": "complete", "evidence_refs": list(refs),
        }, scope_id=self.scope_id, remaining_seconds=5)
        if receipt.durability != "persisted" or not receipt.event_refs:
            raise JourneyExecutionError("control_source_not_persisted")
        return tuple(f"{v.ref}@{v.revision}" for v in receipt.event_refs)

    def _git(self, *args):
        git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
        if not Path(git).is_file():
            raise JourneyExecutionError("TEST_git_executable_missing")
        result = subprocess.run([git, *args], cwd=self.workspace, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise JourneyExecutionError("TEST_git_control_failed")
        return result.stdout.strip()

    def _copy(self, input_root: Path, rows):
        copied = []
        for row in rows:
            source = load_artifact(input_root, row["asset"])
            destination = _inside(self.workspace, row["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise JourneyExecutionError("asset_destination_exists")
            shutil.copyfile(source, destination)
            copied.append({"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
        return {"copied": copied}

    def run(self, action: Mapping[str, Any], input_root: Path):
        kind, params = action["kind"], action["parameters"]
        if kind == "copy_assets":
            return self._copy(input_root, params["assets"])
        if kind in {"prepare_git_workspace", "advance_git_workspace"}:
            return self._git_workspace(kind, params, input_root)
        if kind == "assert_evidence":
            # Retain an independent-scoring task, never mark the assertion PASS.
            criteria = load_artifact(input_root, params["criteria"])
            payload = json.loads(criteria.read_text(encoding="utf-8"))
            missing = [op for op in payload["observed_operations"] if op not in self.observations]
            if missing:
                raise JourneyExecutionError("assertion_observations_missing")
            return {"status": "AWAITING_INDEPENDENT_SCORER", "criteria_artifact": dict(params["criteria"]),
                    "observed_operations": payload["observed_operations"], "workspace_files": self._inventory()}
        core = self._require_core()
        if kind == "snapshot_immediate_state":
            return {"status": _jsonable(core.status(self.context)), "captured_at": core.clock.utc_now()}
        if kind == "reinject_observed_memory":
            refs = self._source_refs(params["source_operation"])
            sources = [self._source(ref) for ref in refs]
            if any(source is None for source in sources):
                raise JourneyExecutionError("reinjection_source_missing")
            observed = []
            for index in range(params["repetitions"]):
                for source in sources:
                    observed.extend(self._capture(source.event["content"], f"{action['operation_id']}/{index}/{source.ref}",
                                                  origin="memory_reinjection", refs=refs))
            return {"source_refs": observed, "same_root_refs": refs}
        if kind == "bounded_enumeration":
            return self._enumerate(params)
        if kind == "seed_scale_archive":
            return self._seed_scale(action, params)
        if kind == "drain_and_observe":
            return self._drain(action, params)
        if kind == "hold_real_consolidation":
            return self._hold(action, params)
        if kind == "release_held_consolidation":
            return self._release_held(require_rejection=params["require_fence_rejection"])
        if kind == "authorized_forget":
            return self._forget(action, params)
        if kind == "backup_sqlite":
            from maintenance.backup import backup_sqlite
            self.host.quiesce()
            destination = _inside(self.evidence_root, params["slot"] + ".sqlite3")
            receipt = backup_sqlite(core.storage.path, destination)
            self._snapshots[params["slot"]] = destination
            self.host.resume()
            return receipt
        if kind == "restore_snapshot":
            return self._restore(params)
        if kind == "replay_deletion_ledger":
            from scope_recall.core.restore import InstallationMaintenance, replay_deletion_ledger
            if self._ledger is None:
                raise JourneyExecutionError("latest_deletion_ledger_missing")
            receipt = replay_deletion_ledger(core.storage, InstallationMaintenance(self.context), self._ledger)
            self.host.resume()
            return receipt
        if kind == "sqlite_unavailable":
            self.host.quiesce_workers()
            vector_open = False
            if params.get("require_vector_available"):
                port = self.runtime._ensure_vector_port(allow_create=False, deadline=time.monotonic() + 3)
                if port is None:
                    raise JourneyExecutionError("actual_existing_vector_companion_required")
                vector_open = True
            connection = sqlite3.connect(core.storage.path, timeout=1)
            try:
                # Exclusive locks in WAL mode do not block readers. Refuse to
                # pretend that they do; use a rollback-journal TEST database.
                prior_mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
                mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if mode.lower() != "delete":
                    raise JourneyExecutionError("sqlite_fault_requires_quiescent_DELETE_mode")
                connection.execute("BEGIN EXCLUSIVE")
            except Exception:
                connection.close()
                raise
            self._lock_connection = connection
            self._journal_mode = prior_mode
            return {"fault": "actual_sqlite_exclusive_lock", "journal_mode": mode,
                    "existing_vector_companion_opened": vector_open}
        if kind == "sqlite_restore":
            if self._lock_connection is None:
                raise JourneyExecutionError("sqlite_fault_not_active")
            self._release_sqlite_fault()
            return {"lock_released": True}
        raise JourneyExecutionError("unknown_control_kind:" + str(kind))

    def _inventory(self):
        return [{"path": str(p.relative_to(self.workspace)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(self.workspace.rglob("*")) if p.is_file() and ".git" not in p.parts]

    def _git_workspace(self, kind, params, input_root):
        if kind == "prepare_git_workspace":
            copied = self._copy(input_root, params["files"])
            self._git("init", "--initial-branch=" + params["initial_branch"])
            self._git("-c", "user.name=TEST", "-c", "user.email=test@example.invalid", "add", ".")
            self._git("-c", "user.name=TEST", "-c", "user.email=test@example.invalid", "commit", "-m", "TEST initial state")
            target = _inside(self.workspace, params["readonly_directory"])
            target.mkdir()
            acl = self._set_writable(target, False)
            return {"files": copied, "head": self._git("rev-parse", "HEAD"), "readonly_probe": acl}
        self._git("switch", "-c", params["branch"])
        target = _inside(self.workspace, params["destination"])
        shutil.copyfile(load_artifact(input_root, params["replace_asset"]), target)
        self._git("add", params["destination"])
        self._git("-c", "user.name=TEST", "-c", "user.email=test@example.invalid", "commit", "-m", "TEST current environment")
        acl = self._set_writable(_inside(self.workspace, params["make_writable"]), True)
        return {"head": self._git("rev-parse", "HEAD"), "main_head": self._git("rev-parse", "main"), "writable_probe": acl}

    def _set_writable(self, target: Path, writable: bool):
        if not target.resolve().is_relative_to(self.workspace) or not target.is_dir():
            raise JourneyExecutionError("readonly_target_outside_workspace")
        if os.name != "nt":
            target.chmod(0o755 if writable else 0o555)
        else:
            options = dict(capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if writable:
                sid = self._readonly_sids.get(target)
                if sid is None:
                    raise JourneyExecutionError("readonly_control_not_owned")
                args = ["icacls", str(target), "/remove:d", "*" + sid]
            else:
                identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], **options)
                identity.check_returncode()
                rows = list(csv.reader(identity.stdout.splitlines()))
                sid = next((row[1] for row in rows if len(row) == 2 and row[1].startswith("S-1-")), None)
                if sid is None:
                    raise JourneyExecutionError("TEST_operator_sid_unavailable")
                args = ["icacls", str(target), "/deny", "*" + sid + ":(OI)(CI)(WD,AD,WEA,WA)"]
                self._readonly_sids[target] = sid
            result = subprocess.run(args, **options)
            result.check_returncode()
            if writable:
                self._readonly_sids.pop(target, None)
        probe = target / ".TEST-write-probe"
        succeeded = False
        try:
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("TEST")
            succeeded = True
        except PermissionError:
            pass
        finally:
            if succeeded:
                probe.unlink()
        if succeeded != writable:
            raise JourneyExecutionError("actual_write_probe_did_not_match_requested_permission")
        return {"actual_write_succeeded": succeeded, "target": str(target)}

    def _enumerate(self, params):
        from scope_recall.core.retrieval import CollectionQuery
        from scope_recall.core.retrieval_storage import scope_digest
        query = CollectionQuery(object_kind=params.get("object_kind", "claim"),
                                where=tuple(tuple(v) for v in params.get("where", ())),
                                page_size=params["page_size"], memory_epoch=self.core.status(self.context).memory_epoch,
                                scope_digest=scope_digest(self.context))
        pages, cursor = [], None
        for _ in range(16):
            page = self.core.collection(self.context, query, cursor=cursor)
            pages.append(_jsonable(page))
            cursor = getattr(page, "next_cursor", None)
            if cursor is None:
                break
        return {"pages": pages, "exhausted": cursor is None}

    def _seed_scale(self, action, params):
        started = time.monotonic()
        checkpoints = []
        maximum = max(params["counts"])
        start = params.get("start_index", 1)
        series = params.get("series_id", action["operation_id"])
        if not _OPERATION.fullmatch(series) or type(start) is not int or not 1 <= start <= maximum <= 100000:
            raise JourneyExecutionError("scale_limit")
        self.host.quiesce_workers()
        write_path = params.get("write_path", "test_only_bulk_fixture")
        if write_path == "production_capture_throughput":
            return self._seed_scale_production_capture_throughput(action, params, started, series, start, maximum)
        if write_path != "test_only_bulk_fixture":
            raise JourneyExecutionError("scale_write_path")
        from p18_scale_fixture import install_test_only_scale_fixture, series_count
        core = self._require_core()
        result = install_test_only_scale_fixture(
            db_path=core.storage.path,
            scope_id=self.scope_id,
            session_id=self.context.session_id,
            project_id=self.context.project_id,
            branch_id=self.context.branch_id,
            series=series,
            start=start,
            maximum=maximum,
            template=params["generator"]["template"],
            now=core.clock.utc_now(),
        )
        for requested in params["counts"]:
            if requested < start:
                continue
            checkpoints.append({
                "requested": requested,
                "actual_source_objects": series_count(core.storage.path, series),
            })
        return {
            "checkpoints": checkpoints,
            "claims_preseeded": False,
            "elapsed_seconds": time.monotonic() - started,
            "external_model_calls": 0,
            "not_production_write_path": True,
            "path": "test_only_bulk_fixture",
            "real_embeddings": 0,
            "scale_fixture": result,
        }

    def _seed_scale_production_capture_throughput(self, action, params, started, series, start, maximum):
        """Measure production Capture import rate only. Not the M45/J08 scale fixture."""
        checkpoints = []
        deadline = params.get("deadline_seconds")
        if deadline is None:
            raw = os.environ.get("SCOPE_RECALL_SCALE_DEADLINE_SECONDS")
            deadline = float(raw) if raw not in (None, "") else 600
        raw_batch = os.environ.get("SCOPE_RECALL_SCALE_BATCH_SIZE")
        batch_size = int(raw_batch) if raw_batch not in (None, "") else 80
        if batch_size < 1:
            batch_size = 1
        storage = self.core.storage
        previous_timeout = getattr(storage, "timeout_seconds", None)
        if previous_timeout is not None:
            storage.timeout_seconds = 30.0
        pending = []
        try:
            for index in range(start, maximum + 1):
                if time.monotonic() - started > deadline:
                    raise JourneyExecutionError("scale_capture_deadline")
                text = params["generator"]["template"].format(index=index, bucket=(index * 17) % 997,
                                                               size=1 + (index * 31) % 83)
                pending.append((text, f"{series}/{index}"))
                if len(pending) >= batch_size or index == maximum or index in params["counts"]:
                    self._capture_scale_batch(pending, origin="external_document")
                    pending.clear()
                if index in params["counts"]:
                    with self.core.storage.read(self.context) as tx:
                        prefix = series + "/"
                        count = tx._check().execute(
                            "SELECT count(*) FROM source_events WHERE substr(source_event_key,1,?)=?",
                            (len(prefix), prefix)).fetchone()[0]
                    checkpoints.append({"requested": index, "actual_source_objects": count})
        finally:
            if previous_timeout is not None:
                storage.timeout_seconds = previous_timeout
        return {
            "checkpoints": checkpoints,
            "claims_preseeded": False,
            "elapsed_seconds": time.monotonic() - started,
            "path": "production_capture_throughput",
            "throughput_only": True,
        }

    def _capture_scale_batch(self, rows, *, origin: str):
        """TEST-only: same capture fields as _capture, one SQLite write per batch."""
        if not rows:
            return
        from scope_recall.core.events import prepare_capture
        from scope_recall.core.mutate import capture_correction
        context = replace(self.context, actor_origin=origin, recent_messages=())
        now = self.core.clock.utc_now()
        remaining = 30.0
        with self.core.storage.write(context, remaining_seconds=remaining) as tx:
            for text, key in rows:
                value = {
                    "protocol_version": "1.1", "source_event_key": key, "source_revision": 1,
                    "origin": origin, "role": "document",
                    "content": text, "occurred_at": now, "recorded_at": now,
                    "time_precision": "instant", "capture_state": "complete", "evidence_refs": [],
                }
                prepared = prepare_capture(value, context)
                if prepared.rejection:
                    raise JourneyExecutionError("control_source_not_persisted")
                for event in prepared.events:
                    source = tx.put_source(event, scope_id=self.scope_id, persisted_at=now,
                                           capture_gaps=prepared.gaps)
                    if source.disposition != "inserted" or not source.ref:
                        raise JourneyExecutionError("control_source_not_persisted")
                    tx.claims.link_source(source.ref, source.revision)
                    tx.index_source(source.ref, source.revision)
                    tx.enqueue_source(source.ref, source.revision, work_type="consolidate", available_at=now)
                    tx.enqueue_source(source.ref, source.revision, work_type="embed", available_at=now)
                    tx.episodes.attach(tx.source(source.ref, source.revision), now)
                    capture_correction(tx, tx.source(source.ref, source.revision), self.core.clock)

    def _drain(self, action, params):
        from scope_recall.core.worker import build_consolidation_model
        model = build_consolidation_model(self.core.consolidation)
        if params.get("require_actual_auxiliary") and model is None:
            raise JourneyExecutionError("actual_auxiliary_not_configured")
        results, raw_refs = [], []
        owner = self

        class Recorder:
            def propose(self, sources, **kwargs):
                raw = model.propose(sources, **kwargs)
                raw_refs.append(_save(owner.evidence_root / f"{action['operation_id']}-aux-{len(raw_refs)+1}.json",
                                      {"actual_raw_result": raw, "source_refs": [f"{s.ref}@{s.revision}" for s in sources]}))
                return raw

        for _ in range(params["max_drains"]):
            receipt = self.runtime.drain(consolidation=Recorder() if model else None)
            results.append(_jsonable(receipt))
            if receipt.idle:
                break
        return {"drains": results, "actual_auxiliary_artifacts": raw_refs,
                "auxiliary_evidence_gap": bool(params.get("require_actual_auxiliary") and not raw_refs)}

    def _lease_fault_target(self, ref):
        """TEST-only proposal/apply pause, not evidence of normal FIFO service.

        Lease exactly the captured replay row with the existing queue's CAS
        fields. Noise remains pending. The production epoch/root/lease fence
        remains authoritative when the real response is applied later.
        """
        from datetime import timedelta
        from scope_recall.core.claim_storage import parse_source_ref
        from scope_recall.core.work_storage import LeasedWork, _format_time, _parse_time
        subject_ref, revision = parse_source_ref(ref)
        with self.core.storage.write(self.context) as tx:
            if tx.source(subject_ref, revision) is None:
                raise JourneyExecutionError("fault_target_inaccessible")
            conn = tx._check(write=True)
            row = conn.execute("SELECT * FROM work_items WHERE work_type='consolidate' AND subject_ref=? AND subject_revision=?",
                               (subject_ref, revision)).fetchone()
            if row is None or row["state"] != "pending":
                raise JourneyExecutionError("fault_target_not_pending")
            owner, token = "TEST-P18-apply-pause", row["lease_token"] + 1
            until = _format_time(_parse_time(self.core.clock.utc_now()) + timedelta(seconds=120))
            updated = conn.execute("UPDATE work_items SET state='leased',lease_token=?,lease_owner=?,lease_until=?,attempt=attempt+1 WHERE work_id=? AND state='pending' AND lease_token=?",
                                   (token, owner, until, row["work_id"], row["lease_token"]))
            if updated.rowcount != 1:
                raise JourneyExecutionError("fault_target_lease_race")
            return LeasedWork(row["work_id"], row["work_type"], subject_ref, revision, row["scope_id"],
                              row["project_id"], row["branch_id"], token, row["attempt"] + 1, owner)

    def _hold(self, action, params):
        from scope_recall.contracts import validate_payload
        from scope_recall.core.consolidate import ConsolidationWorkFence
        from scope_recall.core.worker import _episode_batch, _root_only_sources, build_consolidation_model
        from scope_recall.file_lock import advisory_file_lock
        model = build_consolidation_model(self.core.consolidation)
        if model is None or self._held is not None:
            raise JourneyExecutionError("actual_hold_model_unavailable_or_busy")
        refs = self._source_refs(params["source_operation"])
        source = self._source(refs[0])
        if source is None:
            raise JourneyExecutionError("hold_source_missing")
        with self.core.storage.read(self.context) as tx:
            if not _root_only_sources(tx, (source,)):
                # An A2A message is not an attested human/document source.
                # A TEST replay must not manufacture an eligible L2 root.
                raise JourneyExecutionError("actual_source_not_eligible_for_consolidation")
        self.host.quiesce_workers()
        host_control = self.host.session_control
        pause = getattr(getattr(host_control, "process_owner", host_control), "pause", None)
        if pause is None or pause.lock is None:
            self._worker_lock = advisory_file_lock(self.context.binding.data_directory / "runtime-worker.lock", timeout_seconds=2)
            self._worker_lock.__enter__()
        try:
            new_refs = self._capture(source.event["content"], action["operation_id"] + "/source-replay",
                                     origin=source.event["origin"], refs=refs,
                                     session_id="TEST-" + action["operation_id"] + "-apply-pause")
            item = self._lease_fault_target(new_refs[0])
            with self.core.storage.read(self.context) as tx:
                epoch = tx.status().memory_epoch
                subject = tx.source(item.subject_ref, item.subject_revision)
                episode_ref, batch, pending_sources = _episode_batch(tx, subject, item, now=self.core.clock.utc_now())
                roots = _root_only_sources(tx, batch)
            if not roots:
                raise JourneyExecutionError("no_real_consolidation_needed")
            # Pause at the real worker's proposal/apply boundary. The model
            # has returned; no live thread or shared runtime is left racing
            # deletion/restore. Applying later uses the same authoritative fence.
            raw_hold = os.environ.get("SCOPE_RECALL_HOLD_CONSOLIDATION_SECONDS")
            hold_seconds = float(raw_hold) if raw_hold not in (None, "") else 180.0
            raw = model.propose(roots, episode_ref=episode_ref, remaining_seconds=hold_seconds)
            value = validate_payload("consolidation_result", json.loads(raw))
            fence = ConsolidationWorkFence(item.work_id, item.lease_token, item.lease_owner,
                                           item.subject_ref, item.subject_revision, epoch,
                                           frozenset(f"{v.ref}@{v.revision}" for v in roots),
                                           skipped_source_refs=frozenset(f"{v.ref}@{v.revision}" for v in batch)
                                           - frozenset(f"{v.ref}@{v.revision}" for v in roots),
                                           pending_sources=pending_sources)
            ref = _save(self.evidence_root / f"{action['operation_id']}-held-response.json",
                        {"actual_raw_result": raw, "fence": fence})
            self._held = (value, fence, subject.session_id, item.scope_id, ref)
            return {"actual_response": ref, "source_refs": new_refs, "lease": _jsonable(item),
                    "fault_injection": "single_pending_target_CAS_lease", "normal_scheduler_proven": False}
        except Exception:
            if self._worker_lock is not None:
                self._worker_lock.__exit__(None, None, None)
                self._worker_lock = None
            raise

    def _release_held(self, *, require_rejection):
        from scope_recall.contracts import ContractError
        from scope_recall.core.consolidate import accept_consolidation
        if self._held is None:
            raise JourneyExecutionError("no_held_result")
        value, fence, session_id, scope_id, artifact = self._held
        try:
            if not require_rejection:
                return {"status": "abandoned_prepared_result", "artifact": artifact,
                        "recovery": "existing_lease_expiry"}
            try:
                result = accept_consolidation(self.core.storage, self.core.clock,
                    replace(self.context, session_id=session_id, recent_messages=()), value,
                    scope_id=scope_id, remaining_seconds=5, work_fence=fence)
            except ContractError as exc:
                if exc.code not in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
                    raise
                return {"rejected": True, "code": exc.code, "artifact": artifact}
            raise JourneyExecutionError("late_result_not_fenced:" + str(type(result).__name__))
        finally:
            self._held = None
            if self._worker_lock is not None:
                self._worker_lock.__exit__(None, None, None)
                self._worker_lock = None

    def _forget(self, action, params):
        refs = self._source_refs(params["target_source_operation"])
        if params.get("pending_source_operation"):
            refs += self._source_refs(params["pending_source_operation"])
        refs = tuple(dict.fromkeys(refs))
        sources = [self._source(ref) for ref in refs]
        if any(source is None for source in sources):
            raise JourneyExecutionError("delete_target_not_live")
        # Explicit local TEST operator action, independently authorized by
        # the frozen graph; never upgrade an A2A user's identity.
        if self.context.actor_origin != "human_direct":
            raise JourneyExecutionError("local_TEST_operator_required")
        self._capture("删除 " + " ".join(s.ref for s in sources), action["operation_id"] + "/operator-authorization",
                      origin="human_direct")
        receipt = self.core.forget(self.context, {"protocol_version": "1.1", "mode": "delete",
                                                "target_refs": [s.ref for s in sources],
                                                "expected_revisions": {s.ref: s.revision for s in sources}}, remaining_seconds=10)
        if params["purge_attachments"]:
            self.core.purge_attachments(self.context, receipt["operation_id"], remaining_seconds=10)
        return receipt

    def _restore(self, params):
        from scope_recall.core.restore import InstallationMaintenance, begin_restore, export_deletion_ledger, ledger_digest
        self.host.quiesce()
        authority = InstallationMaintenance(self.context)
        self._ledger = export_deletion_ledger(self.core.storage, authority)
        begin_restore(self.core.storage, authority, expected_ledger_sha256=ledger_digest(self._ledger))
        source = self._snapshots.get(params["slot"])
        if source is None:
            raise JourneyExecutionError("snapshot_missing")
        # SQLite backup API restores in place; never move/delete a computed DB tree.
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as reader, sqlite3.connect(self.core.storage.path) as writer:
            reader.backup(writer)
        return {"restored_snapshot": str(source), "admission_closed": True, "latest_ledger_sha256": ledger_digest(self._ledger)}

    def close(self):
        for target in tuple(self._readonly_sids):
            self._set_writable(target, True)
        if self._lock_connection is not None:
            self._release_sqlite_fault()
        if self._held is not None:
            self._release_held(require_rejection=False)

    def _release_sqlite_fault(self):
        connection = self._lock_connection
        try:
            connection.rollback()
            if self._journal_mode in {"wal", "delete", "truncate", "persist"}:
                connection.execute("PRAGMA journal_mode=" + self._journal_mode)
        finally:
            connection.close()
            self._lock_connection = None
            self._journal_mode = None


def execute_journey(bundle_path: Path, journey_id: str, *, host, controls: CoreJourneyControls,
                    operation_prefix: str = "") -> dict:
    """Run ordered actions; host.execute_turn must use FormalEvidenceWriter.

    The host bridge receives only natural inputs. Assertion files and source
    step labels never enter its request. No fallback fake host is provided.
    """
    input_root, journey = load_journey(bundle_path, journey_id)
    if any(not _OPERATION.fullmatch(operation_prefix + a["operation_id"]) for a in journey["actions"]):
        raise JourneyExecutionError("operation_prefix_invalid")
    sessions: dict[str, str | None] = {}
    receipts = []
    unsupported = []
    try:
        for action in journey["actions"]:
            operation_id = operation_prefix + action["operation_id"]
            kind, alias = action["kind"], action["session_alias"]
            try:
                if kind == "new_session":
                    if alias in sessions:
                        raise JourneyExecutionError("new_session_alias_reused")
                    result = host.new_session(alias)
                    session_id = result["session_id"]
                    if session_id is None:
                        pending = result.get("pending_context_id")
                        if not isinstance(pending, str) or not pending.strip():
                            raise JourneyExecutionError("pending_host_context_required")
                    elif not isinstance(session_id, str) or not session_id.strip() or session_id in sessions.values():
                        raise JourneyExecutionError("new_session_not_distinct")
                    sessions[alias] = session_id
                elif kind == "host_turn":
                    natural = json.loads(load_artifact(input_root, action["parameters"]["input"]).read_text(encoding="utf-8"))
                    result = host.execute_turn(operation_id=operation_id, session_alias=alias,
                                               session_id=sessions.get(alias), model_input=natural,
                                               workspace=controls.workspace)
                    observed_session = result.get("session_id")
                    if not isinstance(observed_session, str) or not observed_session.strip():
                        raise JourneyExecutionError("actual_completed_session_required")
                    if ((sessions.get(alias) is not None and sessions[alias] != observed_session
                         and not (getattr(host, "host_id", None) == "hermes_cli_local_input_v1"
                                  and sessions[alias] in result.get("session_lineage", [])))
                            or any(other != alias and value == observed_session for other, value in sessions.items())):
                        raise JourneyExecutionError("completed_session_not_distinct_or_changed")
                    evidence_path = result.get("formal_evidence_path")
                    if not isinstance(evidence_path, str) or not Path(evidence_path).is_file():
                        raise JourneyExecutionError("actual_formal_turn_receipt_required")
                    formal = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
                    if (formal.get("operation_id") != operation_id or formal.get("status") != "COMPLETED"
                            or formal.get("ids", {}).get("session_id") != result.get("session_id")):
                        raise JourneyExecutionError("completed_formal_turn_association_required")
                    sessions[alias] = result["session_id"]
                else:
                    result = controls.run(action, input_root)
                controls.observations[action["operation_id"]] = result
                receipt = {"operation_id": operation_id, "kind": kind, "status": "RECORDED", "result": result}
            except Exception as exc:
                if isinstance(exc, JourneyExecutionError) and str(exc).startswith("UNSUPPORTED:"):
                    # A baseline lacks candidate-only controls. Preserve that
                    # capability gap and continue its actual host turns under
                    # its own memory policy, instead of borrowing C or losing
                    # the rest of the comparison to a harness exception.
                    result = {"status": "UNSUPPORTED", "reason": str(exc)}
                    controls.observations[action["operation_id"]] = result
                    unsupported.append(operation_id)
                    receipts.append(_save(controls.evidence_root / (operation_id + ".json"), {
                        "operation_id": operation_id, "kind": kind, "status": "UNSUPPORTED", "result": result}))
                    continue
                receipt = {"operation_id": operation_id, "kind": kind, "status": "FAILED",
                           "error_type": type(exc).__name__, "error": str(exc)[:256]}
                ref = _save(controls.evidence_root / (operation_id + ".json"), receipt)
                receipts.append(ref)
                return {"status": "FAILED", "journey_id": journey_id, "receipts": receipts, "semantic_pass": False}
            receipts.append(_save(controls.evidence_root / (operation_id + ".json"), receipt))
        return {"status": "EXECUTED_WITH_UNSUPPORTED_CONTROLS" if unsupported else "EXECUTED",
                "journey_id": journey_id, "receipts": receipts, "unsupported_operations": unsupported,
                "semantic_pass": False, "scoring": "independent_scorer_required"}
    finally:
        controls.close()
