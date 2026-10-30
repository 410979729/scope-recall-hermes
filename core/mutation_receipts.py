# -*- coding: utf-8 -*-
"""Idempotent replay receipts for mutation requests (request-id keyed).

Mirrors ``Deletions.request_key``/receipt: the key binds installation, the
allowed scopes and project/branch to the request id, so a replay of the same id
from another installation or scope never collides with this one. Only committed
outcomes are stored, and a replay hit is read-only: it never bumps the memory
epoch and never writes a second mutation.

Why this exists: ``revise`` accepted a caller-supplied ``request_id`` and both
host adapters dropped it, so a retried revise had nothing to consult. The retry
carried the ``expected_revision`` the first call had already consumed, failed the
head compare-and-swap and reported ``VERSION_CONFLICT`` — the same code a
genuine concurrent writer produces, which is the one outcome a client must not
silently absorb. A client that could not confirm its revise either duplicated a
fact or invented a conflict that never happened.
"""
from __future__ import annotations

import hashlib
import json

from ..contracts import ContractError, TrustedContext


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class MutationReceipts:
    def __init__(self, tx) -> None:
        self._tx = tx

    def request_key(self, request_id: str) -> tuple[str, str]:
        """Digest the trusted identity material bound to this request id."""
        ctx: TrustedContext = self._tx.context
        material = [
            ctx.binding.installation_id,
            sorted(ctx.allowed_scope_ids),
            ctx.project_id,
            ctx.branch_id,
            request_id,
        ]
        digest = hashlib.sha256(_canonical(material).encode()).hexdigest()
        return "revise-" + digest, digest

    def payload_digest(self, request: dict) -> str:
        """Digest the request itself, so the same id under a different body is a reuse rather than a replay."""
        body = {k: v for k, v in request.items() if k != "request_id"}
        return hashlib.sha256(_canonical(body).encode()).hexdigest()

    def find(self, request_key: str, payload_sha256: str) -> dict | None:
        """Return the archived outcome row, or None when this is not a replay.

        A hit with a different payload digest is an id reuse conflict, not a
        replay: the caller must surface ``REQUEST_ID_REUSED`` before any
        ``VERSION_CONFLICT`` check fires, because a replay sees
        ``current_revision > expected_revision``.
        """
        row = (
            self._tx._check()
            .execute(
                "SELECT request_key,request_id,payload_sha256,claim_ref,claim_revision,"
                "disposition,state,scope_id,project_id,branch_id FROM mutation_receipts "
                "WHERE request_key=?",
                (request_key,),
            )
            .fetchone()
        )
        if row is None:
            return None
        if row["payload_sha256"] != payload_sha256:
            raise ContractError("REQUEST_ID_REUSED", "request_id")
        ctx: TrustedContext = self._tx.context
        if row["scope_id"] not in ctx.allowed_scope_ids or (row["project_id"], row["branch_id"]) != (
            ctx.project_id,
            ctx.branch_id,
        ):
            raise ContractError("ACCESS_DENIED", "receipt_unavailable")
        return dict(row)

    def record(
        self,
        request_key: str,
        request_id: str,
        payload_sha256: str,
        claim_ref: str,
        claim_revision: int,
        disposition: str,
        state: str,
        created_at: str,
    ) -> None:
        ctx: TrustedContext = self._tx.context
        scope_id = sorted(ctx.allowed_scope_ids)[0] if ctx.allowed_scope_ids else None
        conn = self._tx._check(write=True)
        conn.execute(
            "INSERT INTO mutation_receipts(request_key,request_id,payload_sha256,"
            "installation_id,scope_id,project_id,branch_id,claim_ref,claim_revision,"
            "disposition,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_key,
                request_id,
                payload_sha256,
                ctx.binding.installation_id,
                scope_id,
                ctx.project_id,
                ctx.branch_id,
                claim_ref,
                claim_revision,
                disposition,
                state,
                created_at,
            ),
        )
