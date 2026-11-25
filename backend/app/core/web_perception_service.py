"""Internet Hot-Topic & External Perception Dual-Channel Service:
- Slow Channel: Scheduled RSS/JSON feed ingestion -> distilled into character's '今日刷网简报'
  injected as background context in chat (NEVER broadcast/spammed to groups).
- Fast Channel: On-demand web search triggered when users mention new slang or breaking news,
  digested and retold in character's natural voice.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import DailyBriefing, HotTopicSource, ModelProvider
from app.security import decrypt_secret

logger = logging.getLogger(__name__)

FAST_SEARCH_TRIGGERS = (
    "什么梗",
    "啥梗",
    "热搜",
    "最新消息",
    "发生什么事",
    "发生了啥",
    "帮我查",
    "搜一下",
    "今天新闻",
    "刚发布",
)


def needs_fast_web_search(text: str) -> bool:
    return any(cue in text for cue in FAST_SEARCH_TRIGGERS)


class WebPerceptionService:
    """Dual-channel external awareness (Slow Daily Briefing + Fast On-Demand Search)."""

    def __init__(
        self,
        master_key: str,
        feed_fetcher: Callable[[str], Awaitable[str]] | None = None,
        search_caller: Callable[[str], Awaitable[list[dict[str, str]]]] | None = None,
    ) -> None:
        self.master_key = master_key
        self._feed_fetcher = feed_fetcher
        self._search_caller = search_caller

    async def refresh_daily_briefing(self, session: AsyncSession) -> DailyBriefing | None:
        """Slow channel: fetch enabled RSS/Atom/JSON sources and distill today's background briefing."""
        stmt = select(HotTopicSource).where(HotTopicSource.is_enabled.is_(True))
        sources = list((await session.execute(stmt)).scalars().all())
        if not sources:
            return None

        collected: list[dict[str, Any]] = []
        for src in sources:
            try:
                raw = await self._fetch_feed(src.feed_url)
                titles = self._parse_feed_titles(raw)[:5]
                for t in titles:
                    collected.append({"source": src.name, "category": src.category, "title": t})
            except Exception as exc:
                logger.warning("Hot topic feed '%s' failed: %s", src.name, exc)

        if not collected:
            return None

        digest_lines = [f"· [{item['category']}/{item['source']}] {item['title']}" for item in collected[:12]]
        digest_text = "今日刷网简报（仅供聊天背景常识，绝不主动群发播报）：\n" + "\n".join(digest_lines)

        briefing = DailyBriefing(
            briefing_date=date.today(),
            category="daily",
            digest_text=digest_text,
            source_items=collected[:12],
        )
        session.add(briefing)
        await session.flush()
        return briefing

    async def get_latest_briefing_text(self, session: AsyncSession) -> str:
        stmt = select(DailyBriefing).order_by(DailyBriefing.created_at.desc()).limit(1)
        row = (await session.execute(stmt)).scalar_one_or_none()
        return row.digest_text if row else ""

    async def fast_search(self, session: AsyncSession, query: str) -> str:
        """Fast channel: on-demand search via configured 'web_search' slot in Model Control Center."""
        if self._search_caller is not None:
            items = await self._search_caller(query)
            if not items:
                return ""
            return "\n".join(f"- {it.get('title', '')}: {it.get('snippet', '')}" for it in items[:4])

        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == "web_search", ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        provider = (await session.execute(stmt)).scalars().first()
        if provider is None:
            return ""

        api_key = decrypt_secret(provider.api_key_enc, self.master_key)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
                resp = await client.post(
                    provider.base_url,
                    headers=headers,
                    json={"query": query, "max_results": 4},
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results") or data.get("data") or []
                lines = [
                    f"- {r.get('title', '')}: {r.get('content') or r.get('snippet', '')}"
                    for r in results[:4]
                ]
                return "\n".join(lines)
        except Exception as exc:
            logger.warning("Fast web search failed for query '%s': %s", query, exc)
            return ""

    async def _fetch_feed(self, url: str) -> str:
        if self._feed_fetcher is not None:
            return await self._feed_fetcher(url)
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    @staticmethod
    def _parse_feed_titles(raw: str) -> list[str]:
        raw = raw.strip()
        if not raw:
            return []
        # Support JSON arrays/objects or RSS/Atom XML
        if raw.startswith("{") or raw.startswith("["):
            import json
            data = json.loads(raw)
            items = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
            return [str(it.get("title", "")).strip() for it in items if isinstance(it, dict) and it.get("title")]

        root = ET.fromstring(raw)
        titles: list[str] = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag in ("item", "entry"):
                for child in elem:
                    if child.tag.split("}")[-1] == "title" and child.text:
                        titles.append(child.text.strip())
                        break
        return titles
