"""Run a synchronous Surface from async code.

Playwright's sync API refuses to start inside a running event loop, and its
objects are thread-affine — they must be created and used on one thread. The
Agent SDK is async. Rather than maintain a second async implementation of a
surface that phase 1 already tested, the surface gets its own thread and every
call is marshalled to it.

The tradeoff, stated plainly: one extra thread and a queue hop per action, in
exchange for the surface staying a single tested implementation. Actions are
sequential anyway — the loop takes one action per turn — so the hop costs
nothing that matters.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Callable

from ..surface.web import WebSurface


class _Stop:
    pass


class SurfaceThread:
    """A WebSurface living on its own thread, callable from an event loop."""

    def __init__(self, **surface_kwargs: Any):
        self._calls: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._surface: WebSurface | None = None
        self._kwargs = surface_kwargs

        self._thread = threading.Thread(target=self._serve, name="surface", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error:
            raise self._error

    # -- the worker --------------------------------------------------------

    def _serve(self) -> None:
        try:
            self._surface = WebSurface(**self._kwargs)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self._error = exc
            self._ready.set()
            return

        self._ready.set()
        while True:
            item = self._calls.get()
            if isinstance(item, _Stop):
                try:
                    self._surface.close()
                finally:
                    return
            fn, result_box, done = item
            try:
                result_box["value"] = fn(self._surface)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the awaiter
                result_box["error"] = exc
            finally:
                done.set()

    # -- the async face ----------------------------------------------------

    async def call(self, fn: Callable[[WebSurface], Any]) -> Any:
        """Run `fn(surface)` on the surface thread and await its result."""
        loop = asyncio.get_running_loop()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._calls.put((fn, box, done))
        await loop.run_in_executor(None, done.wait)
        if "error" in box:
            raise box["error"]
        return box["value"]

    async def close(self) -> None:
        self._calls.put(_Stop())
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._thread.join, 10)
