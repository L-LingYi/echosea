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
        self._event_seq: int = 0

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

    # --- Login failure rate limiting (distributed via Redis when connected, in-memory fallback otherwise) ---
    async def is_login_blocked(self, ip: str, max_attempts: int = 5, window_seconds: int = 300) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        key = f"login_fail:{ip}"
        if self._redis is not None:
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            _, count = await pipe.execute()
            return int(count) >= max_attempts
        ts_list = [t for t in self._zsets[key] if t > cutoff]
        self._zsets[key] = ts_list
        return len(ts_list) >= max_attempts

    async def record_login_failure(self, ip: str, window_seconds: int = 300) -> None:
        now = time.time()
        cutoff = now - window_seconds
        key = f"login_fail:{ip}"
        if self._redis is not None:
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zadd(key, {f"{now}:{time.monotonic_ns()}": now})
            pipe.expire(key, window_seconds + 10)
            await pipe.execute()
            return
        ts_list = [t for t in self._zsets[key] if t > cutoff]
        ts_list.append(now)
        self._zsets[key] = ts_list

    async def reset_login_failures(self, ip: str) -> None:
        key = f"login_fail:{ip}"
        if self._redis is not None:
            await self._redis.delete(key)
            return
        self._zsets.pop(key, None)

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
        """Refresh per-user focused attention timestamp and monotonic sequence after bot interacts with this user."""
        seq = self._event_seq + 1
        self._event_seq = seq
        await self.set_json(
            f"attention:{group_id}:{user_qq}",
            {"ts": time.time(), "seq": seq},
            ttl_seconds=ttl_seconds,
        )

    async def clear_user_attention(self, group_id: int, user_qq: int) -> None:
        await self.delete(f"attention:{group_id}:{user_qq}")

    async def get_user_attention_age_seconds(self, group_id: int, user_qq: int) -> float | None:
        data = await self.get_json(f"attention:{group_id}:{user_qq}")
        if data and isinstance(data, dict) and "ts" in data:
            return max(0.0, time.time() - float(data["ts"]))
        return None

    async def get_user_attention_meta(self, group_id: int, user_qq: int) -> dict[str, Any] | None:
        data = await self.get_json(f"attention:{group_id}:{user_qq}")
        return data if isinstance(data, dict) else None

    async def record_group_human_message(
        self,
        group_id: int,
        sender_qq: int,
        has_hook: bool = False,
        ttl_seconds: int = 300,
    ) -> None:
        """Track the latest human speaker in a group to detect third-party topic interruptions."""
        seq = self._event_seq + 1
        self._event_seq = seq
        await self.set_json(
            f"group_last_human:{group_id}",
            {"qq": int(sender_qq), "ts": time.time(), "seq": seq, "has_hook": bool(has_hook)},
            ttl_seconds=ttl_seconds,
        )

    async def get_last_group_human_message(self, group_id: int) -> dict[str, Any] | None:
        data = await self.get_json(f"group_last_human:{group_id}")
        return data if isinstance(data, dict) else None

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

    # --- Sticker send throttling (cooldown + min non-sticker reply gap) ---
    async def get_sticker_throttle_state(self, scope: str) -> dict[str, Any]:
        data = await self.get_json(f"sticker_throttle:{scope}")
        return data if isinstance(data, dict) else {}

    async def record_sticker_sent(self, scope: str) -> None:
        await self.set_json(
            f"sticker_throttle:{scope}",
            {"last_sent_ts": time.time(), "replies_since": 0},
            ttl_seconds=86400,
        )

    async def record_reply_without_sticker(self, scope: str) -> int:
        state = await self.get_sticker_throttle_state(scope)
        if "last_sent_ts" not in state:
            return 0
        replies_since = int(state.get("replies_since", 0)) + 1
        await self.set_json(
            f"sticker_throttle:{scope}",
            {"last_sent_ts": state["last_sent_ts"], "replies_since": replies_since},
            ttl_seconds=86400,
        )
        return replies_since


class MessageDebouncer:
    """2.0-2.5s debounce window per (scene, group_id, sender_qq) with max_wait cap to merge burst messages."""

    def __init__(
        self,
        window_seconds: float = 2.2,
        max_batch_size: int = 8,
        max_wait_seconds: float = 6.0,
    ) -> None:
        self.window_seconds = window_seconds
        self.max_batch_size = max(1, max_batch_size)
        self.max_wait_seconds = max(0.1, float(max_wait_seconds))
        self._buffers: dict[str, list[Any]] = defaultdict(list)
        self._first_push_ts: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._in_flight: set[asyncio.Task[None]] = set()

    async def _dispatch_batch(self, batch: list[Any], callback: Any) -> None:
        if batch:
            await callback(batch)

    async def push(self, key: str, item: Any, callback: Any) -> None:
        now = time.monotonic()
        first_ts = self._first_push_ts.setdefault(key, now)
        self._buffers[key].append(item)
        existing = self._tasks.pop(key, None)
        if existing and not existing.done():
            existing.cancel()

        if len(self._buffers[key]) >= self.max_batch_size:
            batch = self._buffers.pop(key, [])
            self._first_push_ts.pop(key, None)
            task = asyncio.create_task(self._dispatch_batch(batch, callback), name=f"debounce_flush_cap:{key}")
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
            return

        remaining_cap = max(0.05, self.max_wait_seconds - (now - first_ts))
        delay = min(self.window_seconds, remaining_cap)

        async def _flush() -> None:
            try:
                await asyncio.sleep(delay)
                batch = self._buffers.pop(key, [])
                self._first_push_ts.pop(key, None)
                self._tasks.pop(key, None)
                await self._dispatch_batch(batch, callback)
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(_flush(), name=f"debounce:{key}")
        self._tasks[key] = task

    async def flush_key_now(self, key: str, callback: Any) -> list[Any]:
        """Cancel timer for key and immediately return/flush any buffered items."""
        existing = self._tasks.pop(key, None)
        if existing and not existing.done():
            existing.cancel()
        self._first_push_ts.pop(key, None)
        batch = self._buffers.pop(key, [])
        if batch and callback is not None:
            await callback(batch)
        return batch

    def remove_recalled_message(self, message_id: str) -> int:
        """Drop a recalled message from pending debounce buffers before it flushes."""
        if not message_id:
            return 0
        removed = 0
        for key in list(self._buffers.keys()):
            before = len(self._buffers[key])
            self._buffers[key] = [
                it for it in self._buffers[key]
                if str(getattr(it, "message_id", it.get("message_id", "") if isinstance(it, dict) else "")) != message_id
            ]
            removed += before - len(self._buffers[key])
            if not self._buffers[key]:
                self._buffers.pop(key, None)
                self._first_push_ts.pop(key, None)
                t = self._tasks.pop(key, None)
                if t and not t.done():
                    t.cancel()
        return removed

    async def drain(self, timeout: float = 3.0) -> None:
        """Cancel waiting debounce timers and wait for in-flight flush callbacks during shutdown."""
        for key, t in list(self._tasks.items()):
            if not t.done():
                t.cancel()
        self._tasks.clear()
        self._buffers.clear()
        self._first_push_ts.clear()
        pending = [t for t in self._in_flight if not t.done()]
        if pending:
            _, still = await asyncio.wait(pending, timeout=max(0.1, timeout))
            for t in still:
                t.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
