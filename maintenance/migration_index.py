"""Background index paging for converted truth; no host activation or conversion.

The job orchestrator owns its lock and persistence. This module only schedules
one bounded Core page; vectors remain derived and no model is called here.
"""
# Historical callers also import maintenance.migrate_v2 without the package alias.
from scope_recall.contracts import TrustedContext
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.index_rebuild import queue_embedding_page


def queue_index_page(binding, value: dict, *, limit: int = 128) -> dict:
    """Return job progress after one idempotent index page under the caller's lock."""
    context = TrustedContext(
        binding,
        "upgrade-index:" + value["operation_id"],
        binding.scope_ids,
        "host_generated",
    )
    page = queue_embedding_page(
        SQLiteStorage(binding),
        context,
        after_key=value.get("index_cursor"),
        watermark=value.get("index_watermark"),
        limit=limit,
    )
    value.update(
        index_cursor=list(page["after_key"]),
        index_watermark=list(page["watermark"]),
        index_queue_complete=page["finished"],
        vector_state="queued",
        next_agent_action="start_existing_runtime_worker"
        if page["finished"]
        else "queue_next_index_page",
    )
    return value
