"""Async Plugin Event Bus with per-plugin timeout and fault isolation (Blind Spot #6)."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

PluginHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class RegisteredPlugin:
    name: str
    event_name: str
    handler: PluginHandler
    timeout_seconds: float = 5.0


class PluginEventBus:
    """Decoupled async hook bus so peripheral plugins never crash or stall the main chat pipeline."""

    def __init__(self, default_timeout: float = 5.0, max_error_log: int = 200) -> None:
        self.default_timeout = default_timeout
        self._subscribers: dict[str, list[RegisteredPlugin]] = defaultdict(list)
        self.error_log: deque[dict[str, Any]] = deque(maxlen=max(1, max_error_log))
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._bg_semaphore = asyncio.Semaphore(4)

    def subscribe(
        self,
        event_name: str,
        plugin_name: str,
        handler: PluginHandler,
        timeout_seconds: float | None = None,
    ) -> None:
        self._subscribers[event_name].append(
            RegisteredPlugin(
                name=plugin_name,
                event_name=event_name,
                handler=handler,
                timeout_seconds=timeout_seconds or self.default_timeout,
            )
        )

    def list_plugins(self) -> list[dict[str, Any]]:
        result = []
        for event_name, plugins in self._subscribers.items():
            for p in plugins:
                result.append(
                    {
                        "plugin": p.name,
                        "event": event_name,
                        "timeout_seconds": p.timeout_seconds,
                    }
                )
        return result

    async def _run_plugin(self, plugin: RegisteredPlugin, event_name: str, payload: dict[str, Any]) -> tuple[str, bool, str | None]:
        try:
            await asyncio.wait_for(plugin.handler(payload), timeout=plugin.timeout_seconds)
            return plugin.name, True, None
        except asyncio.TimeoutError:
            err = f"Plugin '{plugin.name}' timed out after {plugin.timeout_seconds}s on '{event_name}'"
            logger.warning(err)
            self.error_log.append({"plugin": plugin.name, "event": event_name, "error": "timeout"})
            return plugin.name, False, "timeout"
        except Exception as exc:
            err = f"Plugin '{plugin.name}' failed on '{event_name}': {exc}"
            logger.warning(err)
            self.error_log.append({"plugin": plugin.name, "event": event_name, "error": str(exc)})
            return plugin.name, False, str(exc)

    async def emit(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch event to all subscribed plugins concurrently with strict timeout & exception isolation."""
        plugins = self._subscribers.get(event_name, [])
        if not plugins:
            return {"succeeded": [], "failed": []}

        results = await asyncio.gather(*(self._run_plugin(p, event_name, payload) for p in plugins))
        succeeded = [name for name, ok, _ in results if ok]
        failed = [{"plugin": name, "error": err or "error"} for name, ok, err in results if not ok]
        return {"succeeded": succeeded, "failed": failed}

    def emit_background(
        self,
        event_name: str,
        payload: dict[str, Any],
        max_concurrency: int | None = None,
    ) -> list[asyncio.Task[Any]]:
        """Dispatch event to subscribed plugins in tracked background tasks without blocking caller."""
        plugins = self._subscribers.get(event_name, [])
        if not plugins:
            return []

        sem = self._bg_semaphore if max_concurrency is None else asyncio.Semaphore(max(1, max_concurrency))
        tasks: list[asyncio.Task[Any]] = []
        for plugin in plugins:
            async def _runner(p: RegisteredPlugin = plugin) -> None:
                async with sem:
                    await self._run_plugin(p, event_name, payload)

            task = asyncio.create_task(_runner(), name=f"plugin:{event_name}:{plugin.name}")
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            tasks.append(task)
        return tasks

    async def drain_background(self, timeout: float = 5.0) -> None:
        """Await all in-flight background plugin tasks during graceful shutdown."""
        pending = [t for t in self._bg_tasks if not t.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=max(0.1, timeout))
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)
