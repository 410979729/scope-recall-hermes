from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Any

from .sql_store import store_row
from .vector_runtime import upsert_vector_record

logger = logging.getLogger(__name__)


def start_writer(provider: Any) -> None:
    if provider._writer_thread and provider._writer_thread.is_alive():
        return
    provider._stop.clear()
    provider._writer_thread = threading.Thread(target=writer_loop, args=(provider,), daemon=True, name="scope-recall-writer")
    provider._writer_thread.start()



def writer_loop(provider: Any) -> None:
    while not provider._stop.is_set():
        try:
            job = provider._write_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            if job is None:
                return
            if job.get("kind") == "flush":
                event = job.get("event")
                if isinstance(event, threading.Event):
                    event.set()
                continue
            if job.get("kind") == "store":
                store_now(
                    provider,
                    content=job["content"],
                    source=job["source"],
                    target=job["target"],
                    session_id=job.get("session_id") or provider._session_id,
                    metadata=job.get("metadata") or {},
                )
        except Exception:
            logger.exception("Scope Recall background write failed")
        finally:
            provider._write_queue.task_done()



def flush_writer(provider: Any, timeout: float = 2.0) -> bool:
    if not provider._writer_thread:
        return True
    done = threading.Event()
    provider._write_queue.put({"kind": "flush", "event": done})
    return done.wait(timeout=timeout)



def shutdown_writer(provider: Any, timeout: float = 3.0) -> None:
    flush_writer(provider, timeout=timeout)
    provider._stop.set()
    if provider._writer_thread and provider._writer_thread.is_alive():
        provider._write_queue.put(None)
        provider._writer_thread.join(timeout=timeout)
    provider._writer_thread = None



def enqueue_store(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    provider._write_queue.put(
        {
            "kind": "store",
            "content": content,
            "source": source,
            "target": target,
            "session_id": session_id,
            "metadata": metadata or {},
        }
    )



def store_now(
    provider: Any,
    *,
    content: str,
    source: str,
    target: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    del metadata
    conn = provider._require_conn()
    memory_id = uuid.uuid4().hex
    with provider._lock:
        memory_id, summary, updated_at = store_row(
            conn,
            memory_id=memory_id,
            scope_id=provider._scope_id,
            platform=provider._scope.platform,
            user_id=provider._scope.user_id,
            chat_id=provider._scope.chat_id,
            thread_id=provider._scope.thread_id,
            gateway_session_key=provider._scope.gateway_session_key,
            agent_identity=provider._scope.agent_identity,
            agent_workspace=provider._scope.agent_workspace,
            session_id=session_id,
            source=source,
            target=target,
            content=content,
        )
    upsert_vector_record(
        provider,
        id=memory_id,
        source=source,
        target=target,
        content=content,
        summary=summary,
        updated_at=updated_at,
    )
    return memory_id
