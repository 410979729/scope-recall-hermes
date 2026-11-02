"""Bound an MCP stdio service to its input pipe and the process that launched it."""

from __future__ import annotations

import os
import threading
from contextlib import suppress

import anyio
from mcp.server.stdio import stdio_server

from ...runtime.process_probe import launching_process, probe_process

SHUTDOWN_SECONDS = 3.0


def _exit_deadline() -> None:
    """End this service after graceful cancellation if an SDK thread remains blocked."""
    timer = threading.Timer(SHUTDOWN_SECONDS, lambda: os._exit(0))
    timer.daemon = True
    timer.start()


async def _serve(server) -> None:
    parent = launching_process()
    disconnected = anyio.Event()
    async with stdio_server() as (incoming, outgoing):
        send, receive = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as group:

            async def forward() -> None:
                try:
                    async with send:
                        async for message in incoming:
                            await send.send(message)
                finally:
                    disconnected.set()

            async def lifetime() -> None:
                while not disconnected.is_set():
                    current = probe_process(parent.pid)
                    if not current.running or (
                        parent.start_token is not None and current.start_token != parent.start_token
                    ):
                        break
                    with anyio.move_on_after(0.25):
                        await disconnected.wait()
                _exit_deadline()
                group.cancel_scope.cancel()

            group.start_soon(forward)
            group.start_soon(lifetime)
            with suppress(anyio.get_cancelled_exc_class()):
                await server._lowlevel_server.run(
                    receive, outgoing, server._lowlevel_server.create_initialization_options()
                )
            group.cancel_scope.cancel()


def run_stdio(server) -> None:
    """Serve until EOF or launcher death, allowing at most three seconds for cleanup."""
    anyio.run(_serve, server)
