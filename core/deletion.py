"""Public forget use case. Authorization precedes effects; receipts follow commit."""
from __future__ import annotations

import re

from ..contracts import ContractError, validate_model_request
from .visibility import allowed
from .source_qualification import AUTHORITY_QUESTION


_DELETE = re.compile(r"删除|删掉|忘掉|忘记|清除|\b(?:delete|erase|forget)\b",re.I)
_SUPPRESS = re.compile(r"不要主动提|别再主动|不再主动提|\b(?:suppress|do not mention|don't mention)\b",re.I)
_NEGATED = re.compile(r"(?:不要|别|不许|暂不|不能|不必|不得|不准)[^。！？!?;；\n]{0,4096}(?:删除|删掉|忘掉|忘记|清除)|\b(?:do not|don't|don’t|never|must not)\s+(?:delete|erase|forget)\b",re.I)
_NON_COMMAND = re.compile(r"如果|假如|假设|倘若|举例|例如|原文|转述|引用|客户说|他说|她说|\b(?:if|unless|suppose|example|quoted?|said)\b", re.I)


def authorize_forget(tx,request,targets):
    ctx = tx.context
    if set(request["expected_revisions"]) != set(request["target_refs"]):
        raise ContractError("ACCESS_DENIED","explicit_target_versions_required")
    proofs = []
    proof_refs = []
    for scope in sorted({t.scope_id for t in targets}):
        row = tx._check().execute("""SELECT event_id,source_revision FROM source_events WHERE origin='human_direct'
            AND session_id=? AND scope_id=? AND project_id IS ? AND branch_id IS ? AND read_blocked=0 ORDER BY rowid DESC LIMIT 1""",
            (ctx.session_id,scope,ctx.project_id,ctx.branch_id)).fetchone()
        if row is None:
            raise ContractError("ACCESS_DENIED","forget_not_authorized")
        source = tx.claims.current_human((f"{row[0]}@{row[1]}",),scope)
        proofs.append(source.event["content"])
        proof_refs.append(source.ref)
    raw = "\n".join(proofs)
    if _NEGATED.search(raw) or _NON_COMMAND.search(raw) or AUTHORITY_QUESTION.search(raw) or not (_DELETE.search(raw) if request["mode"]=="delete" else _SUPPRESS.search(raw)):
        raise ContractError("ACCESS_DENIED","forget_not_authorized")
    # Explicit multi-selection is preserved. Natural-language ambiguous batches
    # are not expanded from a matching subject or a model-provided list.
    if len(targets)>1 and not all(t.ref in raw for t in targets):
        raise ContractError("ACCESS_DENIED","explicit_batch_required")
    for target in targets:
        if not allowed(tx,target.kind,target.ref):
            raise ContractError("ACCESS_DENIED","target_unavailable")
        if target.ref not in raw:
            if target.kind == "claim":
                versions = tx.claims.versions(target.ref)
                head = next(v for v in versions if v.revision==v.current_revision)
                if not all(head.payload[field] in raw for field in ("subject","predicate","value_text")):
                    raise ContractError("ACCESS_DENIED","target_not_bound")
            else:
                source = tx.source(target.ref,target.revision)
                if not source or not source.event["content"] or source.event["content"] not in raw:
                    raise ContractError("ACCESS_DENIED","target_not_bound")
        if target.revision != request["expected_revisions"][target.ref]:
            raise ContractError("VERSION_CONFLICT")
    return tuple(proof_refs)


def forget(storage,clock,context,value,*,remaining_seconds=1.0):
    request = validate_model_request("forget_request",value,context)
    with storage.write(context,remaining_seconds=remaining_seconds) as tx:
        op,_ = tx.deletions.request_key(request)
        prior = tx.deletions.receipt(op)
        if prior is not None:
            return prior
        targets = tuple(tx.deletions.target(ref) for ref in request["target_refs"])
        proofs = authorize_forget(tx,request,targets)
        # The command itself can quote the material being forgotten; do not
        # leave another readable copy in an audit/capture entry.
        proof_targets = tuple(tx.deletions.target(ref) for ref in proofs)
        affected = tx.deletions.closure((*targets,*proof_targets),delete=request["mode"]=="delete")
        op = tx.deletions.block(request,affected,now=clock.utc_now())
        result = tx.deletions.receipt(op)
    return result


def purge_sqlite(storage,context,operation_id,*,remaining_seconds=1.0):
    with storage.write(context,remaining_seconds=remaining_seconds) as tx:
        result = tx.deletions.purge_sqlite(operation_id)
    return result
