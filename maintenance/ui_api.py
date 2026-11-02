"""Translate local web requests to the existing owner interfaces.

Previews and receipts live only for this server process. Store policy, revision
checks and export publication remain in their owning management interfaces.
"""

from __future__ import annotations

import getpass
import secrets
import time
from collections import OrderedDict
from threading import RLock
from typing import Any

from ..contracts import ContractError
from ..core.deletion_mirror import configured_backup_dir
from ..core.installation_copy import installation_copy_id
from ..core.management_query import MemoryFilter
from ..runtime import backup_upkeep
from ..runtime.backup_rotation import find_owned_backup_bundles
from .doctor_report import DoctorReport
from .doctor_store import check_deletion_mirror, check_footprint, check_storage
from .memory_export import write_export

FILTERS = ("kind", "scope", "agent", "session", "since", "until", "query")
REVIEW_SECONDS = 600
RETAINED_RESULTS = 128


def text(data, key, default=None):
    """Read a bounded string without coercing JSON objects into owner input."""
    value = data.get(key, default)
    if value is not None and (type(value) is not str or len(value) > 8192):
        raise ContractError("INPUT_INVALID", key)
    return value


def number(data, key, default=None):
    """Read an integer shared by JSON bodies and URL query parameters."""
    value = data.get(key, default)
    if value is None:
        return None
    if type(value) is str and value.isascii() and value.isdecimal() and len(value) < 12:
        value = int(value)
    if type(value) is not int or not 1 <= value < 2**63:
        raise ContractError("INPUT_INVALID", key)
    return value


def policy_version(data):
    """Policy history starts at zero before its first recorded change."""
    value = data.get("expected_version")
    if type(value) is not int or not 0 <= value < 2**63:
        raise ContractError("INPUT_INVALID", "expected_version")
    return value


def filters(data):
    """Keep inventory, search, session selection and export on one filter contract."""
    return MemoryFilter(**{key: text(data, key) for key in FILTERS if key != "query"}, query=text(data, "query") or "")


def flag(data, key):
    """Accept explicit JSON booleans for optional export and correction controls."""
    value = data.get(key, False)
    if type(value) is not bool:
        raise ContractError("INPUT_INVALID", key)
    return value


class UIApi:
    """Bind a selected owner API and serialize ephemeral review/confirmation state."""

    def __init__(self, management):
        self.management = management
        self.binding = management.core.storage.binding
        # Mint copy metadata before serving: GET status must never create it.
        self.copy_id = installation_copy_id(self.binding.data_directory, self.binding.installation_id)
        self.previews = OrderedDict()
        self.receipts = OrderedDict()
        self.lock = RLock()

    def overview(self):
        """Use the doctor's store and footprint measurements without duplicating SQL."""
        report = DoctorReport(host="hermes", status="ok")
        if not check_storage(report, self.binding, self.binding.data_directory):
            if report.database_present:
                # The database exists but could not be read: ui_http answers every OSError as the retryable
                # store-unavailable error, never as a missing store.
                raise OSError("store status unavailable")
            raise ContractError("SOURCE_MISSING", "store_status")
        check_footprint(report, self.binding.data_directory, None)
        return report.to_dict()

    def backup(self):
        """Read verified bundles, mirror diagnostics and the backup worker's own state."""
        directory = self.binding.data_directory
        report = DoctorReport(host="hermes", status="ok")
        check_deletion_mirror(report, directory)
        destination = configured_backup_dir(directory)
        bundles = (
            find_owned_backup_bundles(destination, self.binding.installation_id, self.copy_id) if destination else []
        )
        state = backup_upkeep.read_backup_state(directory)
        return dict(
            latest_bundle=str(bundles[0].path) if bundles else None,
            checks=report.checks,
            problem=backup_upkeep.backup_job_problem(directory) or state.get("last_error"),
        )

    def _entry_or_status(self, route, data):
        """Read an owner entry or installation status."""
        if route == "entries":
            return {"entries": self.management.entries()}
        if route == "entry":
            return self.management.entry(text(data, "entry_id"))
        return getattr(self, route)()

    def read(self, route, data):
        """Dispatch a read without invoking any state-changing owner method."""
        api = self.management
        if route in {
            "overview",
            "backup",
            "entries",
            "entry",
            "health",
            "health-actions",
            "reviews",
            "review-actions",
            "review-policy",
        }:
            return self._read_control(route, data)
        if route in {"list", "search"}:
            return api.list(filters(data), limit=number(data, "limit", 30), cursor=text(data, "cursor"))
        if route == "detail":
            return api.show(text(data, "ref"), revision=number(data, "revision"))
        if route == "history":
            return api.history(text(data, "ref"), before=number(data, "before"), limit=number(data, "limit", 30))
        if route == "quarantine":
            return api.quarantine(limit=number(data, "limit", 30), before=number(data, "before"))
        if route == "recalls":
            return api.recalls(
                session=text(data, "session"),
                request_id=text(data, "request_id"),
                before=number(data, "before"),
                limit=number(data, "limit", 30),
            )
        if route in {"private", "instruction"}:
            return (
                api.private_status(session=text(data, "session"), project=text(data, "project"))
                if route == "private"
                else {"instructions": api.memory_instructions(text(data, "scope"))}
            )
        if route == "receipt":
            return self._lookup(self.receipts, text(data, "id"))
        raise ContractError("INPUT_INVALID", "route")

    def _read_reviews(self, route, data):
        """Read native queues, receipts and the scope publication switch."""
        if route == "review-policy":
            return self.management.review_policy(text(data, "scope"))
        if route == "review-actions":
            return self.management.review_actions(limit=number(data, "limit", 30), before=number(data, "before"))
        return self.management.reviews(
            source=text(data, "source"), limit=number(data, "limit", 30), cursor=text(data, "cursor")
        )

    def _read_control(self, route, data):
        """Dispatch entry, status and health reads."""
        if route in {"reviews", "review-actions", "review-policy"}:
            return self._read_reviews(route, data)
        return (
            self._read_health(route, data)
            if route in {"health", "health-actions"}
            else self._entry_or_status(route, data)
        )

    def _read_health(self, route, data):
        """Read health findings or undoable action receipts."""
        api = self.management
        if route == "health":
            return api.health(limit=number(data, "limit", 30), cursor=text(data, "cursor"))
        if route == "health-actions":
            return api.health_actions(limit=number(data, "limit", 30), before=number(data, "before"))
        raise ContractError("INPUT_INVALID", "route")

    def _remember(self, collection, identity, value):
        collection[identity] = (time.monotonic(), value)
        while len(collection) > RETAINED_RESULTS:
            collection.popitem(last=False)

    def _lookup(self, collection, identity):
        found = collection.get(identity)
        if found is None or time.monotonic() - found[0] > REVIEW_SECONDS:
            raise ContractError("SOURCE_MISSING", "review_or_receipt")
        return found[1]

    def _control_plan(self, data, action):
        """Prepare entry, instruction and health changes through the owner boundary."""
        if action in {"review-decide", "review-undo", "review-policy-set"}:
            return self._review_plan(data, action)
        if action == "entry-state":
            return dict(action=action, entry_id=text(data, "entry_id"), state=text(data, "state"))
        if action == "instruction-set":
            return dict(action=action, scope=text(data, "scope"), instruction=text(data, "instruction"))
        return self._health_plan(data, action)

    def _plan(self, data):
        api = self.management
        action = text(data, "action")
        if action in {"entry-state", "instruction-set", "review-decide", "review-undo", "review-policy-set"} or (
            action is not None and action.startswith("health-")
        ):
            return self._control_plan(data, action)
        if action in {"mute", "unmute", "forget"}:
            ref = text(data, "ref")
            revision = number(data, "expected_revision")
            mode = {"mute": "suppress", "unmute": "unmute", "forget": "delete"}[action]
            plan = api.preview([ref], mode=mode, expected={ref: revision} if revision else None)
            return dict(action=action, plan=plan)
        if action == "release-quarantine":
            ref = text(data, "ref")
            revision = number(data, "expected_revision")
            if revision is None:
                raise ContractError("INPUT_INVALID", "expected_revision")
            item = api.preview_quarantine_release(ref, expected_revision=revision)
            return dict(
                action=action, ref=ref, expected_revision=revision, item=item, content_sha256=item["content_sha256"]
            )
        if action == "forget-session":
            # Session deletion uses the CLI's source filters, independently of browse text.
            return dict(action=action, plan=api.forget_session(filters(dict(data, query=""))))
        if action == "importance":
            item = api.show(text(data, "ref"))
            return dict(
                action=action,
                item=item,
                expected_revision=number(data, "expected_revision"),
                importance=number(data, "importance"),
                pinned=flag(data, "pinned"),
            )
        if action == "revise":
            item = api.show(text(data, "ref"))
            value = None if flag(data, "retract") else text(data, "value")
            if value is None and not flag(data, "retract"):
                raise ContractError("INPUT_INVALID", "value_required")
            return dict(action=action, item=item, value=value, expected_revision=number(data, "expected_revision"))
        if action in {"private-on", "private-off"}:
            target: dict[str, Any] = dict(
                target=text(data, "target"), session=text(data, "session"), project=text(data, "project")
            )
            if action == "private-on":
                target.update(mode=text(data, "mode", "full"), duration_seconds=number(data, "duration_seconds"))
            else:
                target = api.private_off_preview(**target)
            return dict(action=action, arguments=target)
        if action == "export":
            return dict(
                action=action,
                out=text(data, "out"),
                filters={key: getattr(filters(data), key) for key in FILTERS},
                with_attachments=flag(data, "with_attachments"),
            )
        raise ContractError("INPUT_INVALID", "action")

    def _review_plan(self, data, action):
        """Retain only review contract fields in the single-use owner preview."""
        if action == "review-policy-set":
            return dict(
                action=action,
                scope=text(data, "scope"),
                enabled=flag(data, "enabled"),
                expected_version=policy_version(data),
            )
        if action == "review-undo":
            return dict(
                action=action, action_id=text(data, "action_id"), expected_version=text(data, "expected_version")
            )
        if not isinstance(data.get("key"), list) or data.get("decision") not in {"approve", "reject"}:
            raise ContractError("INPUT_INVALID", "review_decision")
        return dict(
            action=action,
            key=data["key"],
            decision=data["decision"],
            selection=data.get("selection"),
            expected_version=text(data, "expected_version"),
        )

    def _health_plan(self, data, action):
        """Validate the reviewed health decision without disclosing hidden facts."""
        if action in {"health-entity", "health-merge", "health-choose"}:
            first, second = text(data, "first_ref"), text(data, "second_ref")
            return dict(
                action=action,
                first_ref=first,
                first_revision=number(data, "first_revision"),
                second_ref=second,
                second_revision=number(data, "second_revision"),
                name=text(data, "name"),
            )
        if action == "health-undo":
            versions = data.get("versions")
            if not isinstance(versions, dict) or not all(
                type(k) is str and type(v) is int and v > 0 for k, v in versions.items()
            ):
                raise ContractError("INPUT_INVALID", "versions")
            return dict(action=action, action_id=text(data, "action_id"), versions=versions)
        raise ContractError("INPUT_INVALID", "action")

    def preview(self, data):
        """Retain the exact displayed request; confirmation cannot substitute its payload."""
        with self.lock:
            plan = self._plan(data)
            identity = secrets.token_urlsafe(24)
            self._remember(self.previews, identity, plan)
            return dict(id=identity, preview=plan, expires_in_seconds=REVIEW_SECONDS)

    def _control_apply(self, plan):
        """Apply an entry, instruction or health review."""
        action = plan["action"]
        if action in {"review-decide", "review-undo", "review-policy-set"}:
            return self._review_apply(plan)
        if action == "entry-state":
            return self.management.set_entry_state(plan["entry_id"], plan["state"], actor=getpass.getuser())
        if action == "instruction-set":
            return self.management.set_memory_instruction(plan["scope"], plan["instruction"])
        return self._health_apply(plan)

    def _apply(self, plan):
        api = self.management
        action = plan["action"]
        if action in {"entry-state", "instruction-set", "review-decide", "review-undo", "review-policy-set"} or (
            action is not None and action.startswith("health-")
        ):
            return self._control_apply(plan)
        if action in {"mute", "unmute", "forget", "forget-session"}:
            return api.apply(plan["plan"])
        if action == "release-quarantine":
            return api.release_quarantine(
                plan["ref"], expected_revision=plan["expected_revision"], content_sha256=plan["content_sha256"]
            )
        if action == "importance":
            return api.importance(
                plan["item"]["ref"],
                expected_revision=plan["expected_revision"],
                importance=plan["importance"],
                pinned=plan["pinned"],
            )
        if action == "revise":
            return api.revise(plan["item"]["ref"], expected_revision=plan["expected_revision"], value=plan["value"])
        if action == "private-on":
            return api.private_on(**plan["arguments"])
        if action == "private-off":
            return api.private_off(**plan["arguments"])
        with api.export_reader(MemoryFilter(**plan["filters"])) as reader:
            return write_export(reader, plan["out"], with_attachments=plan["with_attachments"])

    def _review_apply(self, plan):
        """Route review writes through the same owner boundary as the CLI."""
        if plan["action"] == "review-policy-set":
            return self.management.set_review_policy(
                plan["scope"], plan["enabled"], expected_version=plan["expected_version"]
            )
        if plan["action"] == "review-undo":
            return self.management.review_undo(plan["action_id"], expected_version=plan["expected_version"])
        return self.management.review_decide(
            plan["key"], plan["decision"], expected_version=plan["expected_version"], selection=plan["selection"]
        )

    def _health_apply(self, plan):
        """Apply a reviewed health decision through the management boundary."""
        api = self.management
        action = plan["action"]
        if action in {"health-entity", "health-merge", "health-choose"}:
            fn = {
                "health-entity": api.health_merge_entity,
                "health-merge": api.health_merge_facts,
                "health-choose": api.health_choose_fact,
            }[action]
            args = (plan["first_ref"], plan["first_revision"], plan["second_ref"], plan["second_revision"])
            return fn(*args, name=plan["name"]) if action == "health-entity" else fn(*args)
        if action == "health-undo":
            return api.health_undo(plan["action_id"], plan["versions"])
        raise ContractError("INPUT_INVALID", "action")

    def confirm(self, data):
        """Apply a single-use review after an explicit confirmation and retain its receipt."""
        if data.get("confirmed") is not True:
            raise ContractError("INPUT_INVALID", "confirmation_required")
        with self.lock:
            identity = text(data, "id")
            plan = self._lookup(self.previews, identity)
            del self.previews[identity]
            result = dict(id=identity, action=plan["action"], result=self._apply(plan))
            self._remember(self.receipts, identity, result)
            return result
