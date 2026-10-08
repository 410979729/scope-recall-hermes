"""The maintenance passes an operator runs on a store (``MemoryCore.operations``): repairing and requalifying
claims, retiring rootless proposals, unindexing withheld tool outputs, retrying failed work, re-embedding into a
new space and purging."""

from __future__ import annotations

import time

from ..contracts import ContractError, TrustedContext
from . import lexical_index
from .file_lock import advisory_file_lock
from .work_storage import respace_refusal


class Operations:
    def __init__(self, core) -> None:
        self._core = core

    def repair_claim_frames(
        self, context: TrustedContext, *, after_ref: str = "", limit: int = 16, remaining_seconds: float | None = None
    ):
        """Revalidate one bounded page of legacy frames without model calls."""
        from .requalify import repair_frames

        with self._core.storage.write(
            context,
            remaining_seconds=self._core.config.write_timeout_seconds
            if remaining_seconds is None
            else remaining_seconds,
        ) as tx:
            return repair_frames(tx, now=self._core.clock.utc_now(), after_ref=after_ref, limit=limit)

    def requalify_claims(
        self,
        context: TrustedContext,
        *,
        after_ref: str = "",
        limit: int = 16,
        dry_run: bool = True,
        remaining_seconds: float | None = None,
    ):
        """Re-judge one bounded page of stored claims after a rule change.

        The preview runs in a read transaction, so it cannot write even by
        mistake; only ``dry_run=False`` opens the write path.
        """
        from .requalify import requalify_claims as _requalify

        seconds = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        opener = self._core.storage.read if dry_run else self._core.storage.write
        with opener(context, remaining_seconds=seconds) as tx:
            return _requalify(
                tx, now=self._core.clock.utc_now(), after_ref=after_ref, limit=limit, dry_run=dry_run
            ).to_dict()

    def retire_rootless_proposals(
        self,
        context: TrustedContext,
        *,
        after_ref: str = "",
        limit: int = 16,
        dry_run: bool = True,
        remaining_seconds: float | None = None,
    ):
        """Retire one bounded page of proposals no derivation root supports; a preview unless ``dry_run=False``."""
        from .requalify import retire_rootless_proposals as _retire

        seconds = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        opener = self._core.storage.read if dry_run else self._core.storage.write
        with opener(context, remaining_seconds=seconds) as tx:
            return _retire(
                tx, now=self._core.clock.utc_now(), after_ref=after_ref, limit=limit, dry_run=dry_run
            ).to_dict()

    def unindex_withheld_outputs(
        self,
        context: TrustedContext,
        *,
        after_id: int = 0,
        limit: int = 500,
        dry_run: bool = True,
        remaining_seconds: float | None = None,
    ):
        """Drop the postings of one bounded page of withheld tool outputs' placeholders beyond their error text; a
        preview unless ``dry_run=False``.  The sources stay; only the lexical index loses what it never
        needed."""
        seconds = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        opener = self._core.storage.read if dry_run else self._core.storage.write
        with opener(context, remaining_seconds=seconds) as tx:
            return lexical_index.unindex_withheld(
                tx._check(write=not dry_run), context.allowed_scope_ids, after_id=after_id, limit=limit, dry_run=dry_run
            )

    def retry_failed_work(
        self,
        context: TrustedContext,
        *,
        include_terminal: bool = False,
        limit: int = 64,
        dry_run: bool = True,
        remaining_seconds: float | None = None,
    ):
        """Grant one bounded re-look to failures a shipped fix may have cured.

        The preview runs in a read transaction, so it cannot write even by
        mistake; only ``dry_run=False`` opens the write path.
        """
        seconds = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        opener = self._core.storage.read if dry_run else self._core.storage.write
        with opener(context, remaining_seconds=seconds) as tx:
            return tx.work.retry_failed(
                now=self._core.clock.utc_now(), include_terminal=include_terminal, limit=limit, dry_run=dry_run
            )

    def respace_embeddings(
        self,
        context: TrustedContext,
        *,
        space_id: str,
        action: str = "status",
        dry_run: bool = True,
        remaining_seconds: float | None = None,
    ) -> dict:
        """Report, start or cancel the store's re-embed run into ``space_id`` (``core/work_storage.py``).

        ``run`` is the run as it stands after the action (before it, in a preview).  ``to_reopen`` is what a run that
        is going still has to look at, or else what a new one would reopen; ``waiting`` the embeddings still waiting
        anywhere, which a run started now reopens once they are done: paid twice, as is whatever the new space
        embedded before the start.  The run covers the whole store:
        each worker in the space reopens pages of it while fewer than the queue's ceiling wait anywhere, and claims
        its own share.  A preview only reads; a start it would refuse is refused in the preview too.
        """
        if action not in ("status", "start", "restart", "cancel") or type(dry_run) is not bool:
            raise ContractError("INPUT_INVALID", "respace_action")
        writes = action != "status" and not dry_run
        seconds = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        report: dict = {"embedding_space": space_id, "action": action, "applied": writes}
        if writes:
            with self._core.storage.write(context, remaining_seconds=seconds) as tx:
                if action == "cancel":
                    report["cancelled"] = tx.work.cancel_respace()
                else:
                    tx.work.start_respace(space_id, now=self._core.clock.utc_now(), restart=action == "restart")
        # Counting walks the queue: kept out of the write, and so off the writer lease.
        with self._core.storage.read(context, remaining_seconds=seconds) as tx:
            run = tx.work.respace_run()
            if not writes and action in ("start", "restart"):
                refusal = respace_refusal(run, space_id, restart=action == "restart")
                if refusal is not None:
                    raise ContractError("VERSION_CONFLICT", refusal)
            going = run is not None and not run["completed"] and (writes or action == "status")
            report["run"] = run
            report["space_matches"] = run is None or run["embedding_space"] == space_id
            report["to_reopen"] = tx.work.respace_remaining(at_most=run["next_work_id"] if going else None)
            report["waiting"] = tx.work.embed_queue()["pending"]
        return report

    def purge_sqlite(self, context: TrustedContext, operation_id: str, *, remaining_seconds: float | None = None):
        from .deletion import purge_sqlite

        return purge_sqlite(
            self._core.storage,
            context,
            operation_id,
            remaining_seconds=self._core.config.write_timeout_seconds
            if remaining_seconds is None
            else remaining_seconds,
        )

    def purge_attachments(self, context, operation_id, *, remaining_seconds=None):
        from .retained_artifacts import RetainedBlob, erase_retained

        budget = self._core.config.write_timeout_seconds if remaining_seconds is None else remaining_seconds
        deadline = time.monotonic() + float(budget)

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        try:
            with advisory_file_lock(
                context.binding.data_directory / "scope-recall-retained.lock", timeout_seconds=remaining()
            ):
                with self._core.storage.read(context, remaining_seconds=remaining()) as tx:
                    plan = tx.deletions.attachment_plan(operation_id)
                if plan.get("already_done"):
                    with self._core.storage.read(context, remaining_seconds=remaining()) as tx:
                        return tx.deletions.receipt(operation_id)
                for entry in plan["entries"]:
                    if remaining() <= 0:
                        raise ContractError("DEADLINE_EXCEEDED")
                    if entry.get("shared"):
                        continue
                    erase_retained(context.binding, RetainedBlob(**entry["blob"]))
                if remaining() <= 0:
                    raise ContractError("DEADLINE_EXCEEDED")
                with self._core.storage.write(context, remaining_seconds=remaining()) as tx:
                    return tx.deletions.finalize_attachments(operation_id, plan, erased=True)
        except TimeoutError as exc:
            raise ContractError("DEADLINE_EXCEEDED", "retained_lock") from exc
