"""Async Plugin Event Bus with per-plugin timeout and fault isolation (Blind Spot #6)."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
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

    def __init__(self, default_timeout: float = 5.0) -> None:
        self.default_timeout = default_timeout
        self._subscribers: dict[str, list[RegisteredPlugin]] = defaultdict(list)
        self.error_log: list[dict[str, Any]] = []

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

    async def emit(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch event to all subscribed plugins concurrently with strict timeout & exception isolation."""
        plugins = self._subscribers.get(event_name, [])
        if not plugins:
            return {"succeeded": [], "failed": []}

        succeeded: list[str] = []
        failed: list[dict[str, str]] = []

        async def _run_one(plugin: RegisteredPlugin) -> None:
            try:
                await asyncio.wait_for(plugin.handler(payload), timeout=plugin.timeout_seconds)
                succeeded.append(plugin.name)
            except asyncio.TimeoutError:
                err = f"Plugin '{plugin.name}' timed out after {plugin.timeout_seconds}s on '{event_name}'"
                logger.warning(err)
                self.error_log.append({"plugin": plugin.name, "event": event_name, "error": "timeout"})
                failed.append({"plugin": plugin.name, "error": "timeout"})
            except Exception as exc:
                err = f"Plugin '{plugin.name}' failed on '{event_name}': {exc}"
                logger.warning(err)
                self.error_log.append({"plugin": plugin.name, "event": event_name, "error": str(exc)})
                failed.append({"plugin": plugin.name, "error": str(exc)})

        await asyncio.gather(*(_run_one(p) for p in plugins))
        return {"succeeded": succeeded, "failed": failed}
