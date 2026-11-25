"""Redis state store with seamless async in-memory fallback for tests and single-node dev."""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    aioredis = None


class StateStore:
    """Unified async store for active conversation threads, rate limits, recalls, and debounce."""

    def __init__(self, redis_url: str | None = None, force_memory: bool = False) -> None:
        self._redis_url = redis_url
        self._force_memory = force_memory
        self._redis: Any = None
        # In-memory fallback structures
        self._kv: dict[str, tuple[str, float | None]] = {}
        self._zsets: dict[str, list[float]] = defaultdict(list)

    async def connect(self) -> None:
        if self._force_memory or not self._redis_url or aioredis is None:
            return
        try:
            client = aioredis.from_url(self._redis_url, decode_responses=True, socket_connect_timeout=2)
            await client.ping()
            self._redis = client
        except Exception:
            # Graceful fallback to in-memory store if Redis is unreachable
            self._redis = None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @property
    def backend_name(self) -> str:
        return "redis" if self._redis is not None else "memory"

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        if self._redis is not None:
            await self._redis.set(key, raw, ex=ttl_seconds)
            return
        expires_at = (time.monotonic() + ttl_seconds) if ttl_seconds else None
        self._kv[key] = (raw, expires_at)

    async def get_json(self, key: str) -> Any | None:
        if self._redis is not None:
            raw = await self._redis.get(key)
            return json.loads(raw) if raw else None
        item = self._kv.get(key)
        if not item:
            return None
        raw, expires_at = item
        if expires_at is not None and time.monotonic() > expires_at:
            self._kv.pop(key, None)
            return None
        return json.loads(raw)

    async def delete(self, key: str) -> None:
        if self._redis is not None:
            await self._redis.delete(key)
            return
        self._kv.pop(key, None)

    async def mark_recalled(self, message_id: str, ttl_seconds: int = 3600) -> None:
        await self.set_json(f"recalled:{message_id}", True, ttl_seconds=ttl_seconds)

    async def is_recalled(self, message_id: str) -> bool:
        return bool(await self.get_json(f"recalled:{message_id}"))

    async def check_rate_limit(self, bucket: str, limit: int, window_seconds: int = 60) -> bool:
        """Return True if request is allowed, False if rate limit exceeded."""
        now = time.time()
        key = f"rl:{bucket}"
        if self._redis is not None:
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - window_seconds)
            pipe.zcard(key)
            pipe.zadd(key, {f"{now}:{time.monotonic_ns()}": now})
            pipe.expire(key, window_seconds + 5)
            _, count, _, _ = await pipe.execute()
            return int(count) < limit

        cutoff = now - window_seconds
        ts_list = [t for t in self._zsets[key] if t > cutoff]
        if len(ts_list) >= limit:
            self._zsets[key] = ts_list
            return False
        ts_list.append(now)
        self._zsets[key] = ts_list
        return True

    # --- Active thread tracking for 3-layer Gatekeeper ---
    async def record_peer_interaction(self, group_id: int, user_a: int, user_b: int, ttl_seconds: int = 180) -> None:
        """Track active peer-to-peer conversation pair inside a group."""
        pair = sorted([user_a, user_b])
        key = f"peer_thread:{group_id}:{pair[0]}:{pair[1]}"
        await self.set_json(key, {"users": pair, "ts": time.time()}, ttl_seconds=ttl_seconds)
        # Also record last active peer for each user
        await self.set_json(f"last_peer:{group_id}:{user_a}", {"peer": user_b, "ts": time.time()}, ttl_seconds=ttl_seconds)

    async def get_active_peer(self, group_id: int, user_qq: int) -> int | None:
        data = await self.get_json(f"last_peer:{group_id}:{user_qq}")
        if data and isinstance(data, dict):
            return int(data["peer"])
        return None

    async def record_user_attention(
        self, group_id: int, user_qq: int, ttl_seconds: int = 260
    ) -> None:
        """Refresh per-user focused attention timestamp after bot interacts with this user."""
        await self.set_json(
            f"attention:{group_id}:{user_qq}",
            {"ts": time.time()},
            ttl_seconds=ttl_seconds,
        )

    async def clear_user_attention(self, group_id: int, user_qq: int) -> None:
        await self.delete(f"attention:{group_id}:{user_qq}")

    async def get_user_attention_age_seconds(self, group_id: int, user_qq: int) -> float | None:
        data = await self.get_json(f"attention:{group_id}:{user_qq}")
        if data and isinstance(data, dict) and "ts" in data:
            return max(0.0, time.time() - float(data["ts"]))
        return None

    async def record_group_bot_activity(self, group_id: int, ttl_seconds: int = 210) -> None:
        """Refresh group-wide recent bot reply timestamp for multi-user topic continuity."""
        await self.set_json(
            f"group_bot_active:{group_id}",
            {"ts": time.time()},
            ttl_seconds=ttl_seconds,
        )

    async def get_group_bot_activity_age_seconds(self, group_id: int) -> float | None:
        data = await self.get_json(f"group_bot_active:{group_id}")
        if data and isinstance(data, dict) and "ts" in data:
            return max(0.0, time.time() - float(data["ts"]))
        return None

    async def record_bot_turn(self, group_id: int, reset: bool = False) -> int:
        key = f"bot_turns:{group_id}"
        if reset:
            await self.set_json(key, {"count": 0, "ts": time.time()}, ttl_seconds=600)
            return 0
        data = await self.get_json(key) or {"count": 0}
        count = int(data.get("count", 0)) + 1
        await self.set_json(key, {"count": count, "ts": time.time()}, ttl_seconds=600)
        return count

    async def get_consecutive_bot_turns(self, group_id: int) -> int:
        data = await self.get_json(f"bot_turns:{group_id}")
        return int(data.get("count", 0)) if data else 0


class MessageDebouncer:
    """2-4s debounce window per (scene, group_id, sender_qq) to merge burst messages."""

    def __init__(self, window_seconds: float = 2.5) -> None:
        self.window_seconds = window_seconds
        self._buffers: dict[str, list[Any]] = defaultdict(list)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def push(self, key: str, item: Any, callback: Any) -> None:
        self._buffers[key].append(item)
        existing = self._tasks.get(key)
        if existing and not existing.done():
            existing.cancel()

        async def _flush() -> None:
            try:
                await asyncio.sleep(self.window_seconds)
                batch = self._buffers.pop(key, [])
                self._tasks.pop(key, None)
                if batch:
                    await callback(batch)
            except asyncio.CancelledError:
                return

        self._tasks[key] = asyncio.create_task(_flush())
