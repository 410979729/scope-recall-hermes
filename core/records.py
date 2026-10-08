"""Record calls no host makes (``MemoryCore.records``): accepting claim proposals and a consolidation, scheduling a
source and resuming the deferred ones, the unresolved updates, an episode's sources, artifacts and references,
and releasing objects."""

from __future__ import annotations

import time

from ..contracts import ContractError, TrustedContext
from .admission import resume_deferred, schedule_source
from .file_lock import advisory_file_lock
from .visibility import release_objects


class Records:
    def __init__(self, core) -> None:
        self._core = core

    def schedule_source(self, context, ref, revision, *, remaining_seconds=None):
        return schedule_source(
            self._core.storage,
            self._core.clock,
            context,
            ref,
            revision,
            policy=self._core.config.admission_policy,
            remaining_seconds=self._core.config.write_timeout_seconds
            if remaining_seconds is None
            else remaining_seconds,
        )

    def resume_deferred(self, context, *, limit=16, remaining_seconds=None):
        return resume_deferred(
            self._core.storage,
            self._core.clock,
            context,
            self._core.config.admission_policy,
            limit=limit,
            remaining_seconds=self._core.config.write_timeout_seconds
            if remaining_seconds is None
            else remaining_seconds,
        )

    def accept_claim_proposals(
        self, context: TrustedContext, value, *, scope_id: str, remaining_seconds: float | None = None
    ):
        from .mutate import accept_claim_proposals

        return accept_claim_proposals(
            self._core.storage,
            self._core.clock,
            context,
            value,
            scope_id=scope_id,
            remaining_seconds=self._core.config.write_timeout_seconds
            if remaining_seconds is None
            else remaining_seconds,
        )

    def unresolved_updates(self, context: TrustedContext):
        with self._core.storage.read(context) as tx:
            return tx.claims.unresolved_updates()

    def release_objects(
        self, context: TrustedContext, refs, *, expected_epoch: int, automatic: bool = True, history: bool = False
    ):
        return release_objects(
            self._core.storage,
            self._core.clock,
            context,
            refs,
            expected_epoch=expected_epoch,
            automatic=automatic,
            history=history,
        )

    def episode_sources(self, context, ref, *, after_sequence=0, limit=32):
        with self._core.storage.read(context) as tx:
            return tx.episodes.sources(ref, after_sequence=after_sequence, limit=limit)

    def accept_consolidation(self, context, value, *, scope_id, remaining_seconds=None):
        from .consolidate import accept_consolidation

        return accept_consolidation(
            self._core.storage,
            self._core.clock,
            context,
            value,
            scope_id=scope_id,
            remaining_seconds=self._core.config.write_timeout_seconds
            if remaining_seconds is None
            else remaining_seconds,
        )

    def register_artifact(self, context, *, remaining_seconds=None, **registration):
        budget = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        deadline = time.monotonic() + float(budget)

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        try:
            with advisory_file_lock(
                context.binding.data_directory / "scope-recall-retained.lock", timeout_seconds=remaining()
            ):
                with self._core.storage.write(context, remaining_seconds=remaining()) as tx:
                    return tx.artifacts.register(**registration, now=self._core.clock.utc_now())
        except TimeoutError as exc:
            raise ContractError("DEADLINE_EXCEEDED", "retained_lock") from exc

    def artifact(self, context, ref, revision):
        with self._core.storage.read(context) as tx:
            return tx.artifacts.get(ref, revision)

    def open_artifact(self, context, ref, revision):
        with self._core.storage.read(context) as tx:
            return tx.artifacts.open(ref, revision)

    def reference(self, context, ref, revision=None):
        with self._core.storage.read(context) as tx:
            return tx.references.get(ref, revision)
