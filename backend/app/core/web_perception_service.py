"""Internet Hot-Topic & External Perception Dual-Channel Service:
- Slow Channel: Scheduled RSS/JSON feed ingestion -> distilled into character's '今日刷网简报'
  injected as background context in chat (NEVER broadcast/spammed to groups).
- Fast Channel: LLM-autonomous web search & webpage reading with 3-tier zero-config fallback:
  1) Configured 'web_search' slot provider (Tavily/Serper/Bocha/SearXNG/custom)
  2) Local/Docker SearXNG JSON endpoint (no API key required)
  3) Built-in pure-httpx free search fallback (Bing / DuckDuckGo HTML)
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET
from datetime import date
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

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

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def needs_fast_web_search(text: str) -> bool:
    """Legacy keyword heuristic kept for backward compatibility."""
    return any(cue in text for cue in FAST_SEARCH_TRIGGERS)


class _BingHTMLParser(HTMLParser):
    """Extract search result titles, URLs, and snippets from Bing SERP HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_algo = False
        self._algo_depth = 0
        self._in_title_link = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._current_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        cls = attr_map.get("class") or ""
        if tag == "li" and "b_algo" in cls.split():
            self._in_algo = True
            self._algo_depth = 1
            self._title_parts = []
            self._snippet_parts = []
            self._current_url = ""
            return
        if not self._in_algo:
            return
        if tag == "li":
            self._algo_depth += 1
        elif tag == "a" and not self._current_url and (attr_map.get("href") or "").startswith("http"):
            self._current_url = str(attr_map["href"])
            self._in_title_link = True
        elif tag in ("p", "div") and ("b_caption" in cls or "b_lineclamp" in cls or "b_algoSlug" in cls):
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._in_algo:
            return
        if tag == "a" and self._in_title_link:
            self._in_title_link = False
        elif tag in ("p", "div") and self._in_snippet:
            self._in_snippet = False
        elif tag == "li":
            self._algo_depth -= 1
            if self._algo_depth <= 0:
                title = re.sub(r"\s+", " ", "".join(self._title_parts)).strip()
                snippet = re.sub(r"\s+", " ", "".join(self._snippet_parts)).strip()
                if title and self._current_url:
                    self.results.append(
                        {"title": title, "url": self._current_url, "snippet": snippet}
                    )
                self._in_algo = False

    def handle_data(self, data: str) -> None:
        if not self._in_algo:
            return
        if self._in_title_link:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


class _DDGHTMLParser(HTMLParser):
    """Extract search results from DuckDuckGo HTML endpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._cur_title: list[str] = []
        self._cur_snippet: list[str] = []
        self._cur_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        cls = (attr_map.get("class") or "").split()
        if tag == "a" and "result__a" in cls:
            raw_href = str(attr_map.get("href") or "")
            if "uddg=" in raw_href:
                qs = parse_qs(urlparse(raw_href).query)
                raw_href = unquote(qs.get("uddg", [raw_href])[0])
            self._cur_url = raw_href
            self._cur_title = []
            self._cur_snippet = []
            self._in_title = True
        elif tag in ("a", "div", "span") and "result__snippet" in cls:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        elif self._in_snippet and tag in ("a", "div", "span"):
            self._in_snippet = False
            title = re.sub(r"\s+", " ", "".join(self._cur_title)).strip()
            snippet = re.sub(r"\s+", " ", "".join(self._cur_snippet)).strip()
            if title and self._cur_url:
                self.results.append({"title": title, "url": self._cur_url, "snippet": snippet})
            self._cur_url = ""

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._cur_title.append(data)
        elif self._in_snippet:
            self._cur_snippet.append(data)


class _ReadableTextParser(HTMLParser):
    """Extract readable text from HTML while ignoring script/style/noscript tags."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript", "svg") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag.lower() in ("p", "div", "li", "h1", "h2", "h3", "br", "tr"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in joined.splitlines()]
        return "\n".join(ln for ln in lines if ln)


_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
)
_PROXY_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_fake_ip: bool) -> bool:
    if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
        return True
    if any(ip_obj in net for net in _PRIVATE_NETS):
        return True
    if not allow_fake_ip and ip_obj in _PROXY_FAKE_IP_NET:
        return True
    return False


def _is_public_http_url(url: str) -> bool:
    """Block non-HTTP(S) schemes and private/loopback/link-local hosts to prevent SSRF."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal")):
        return False
    try:
        ip_obj = ipaddress.ip_address(host)
        return not _is_forbidden_ip(ip_obj, allow_fake_ip=False)
    except ValueError:
        pass
    if "." not in host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for info in infos:
            addr = info[4][0]
            ip_obj = ipaddress.ip_address(addr.split("%")[0])
            if _is_forbidden_ip(ip_obj, allow_fake_ip=True):
                return False
    except Exception:
        return True
    return True


class WebPerceptionService:
    """Dual-channel external awareness (Slow Daily Briefing + Fast On-Demand / Autonomous Search)."""

    def __init__(
        self,
        master_key: str,
        feed_fetcher: Callable[[str], Awaitable[str]] | None = None,
        search_caller: Callable[[str], Awaitable[list[dict[str, str]]]] | None = None,
        searxng_url: str = "",
        default_free_search_enabled: bool = True,
        search_timeout_seconds: float = 8.0,
    ) -> None:
        self.master_key = master_key
        self._feed_fetcher = feed_fetcher
        self._search_caller = search_caller
        self.searxng_url = searxng_url.strip().rstrip("/")
        self.default_free_search_enabled = default_free_search_enabled
        self.search_timeout_seconds = search_timeout_seconds

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

    @staticmethod
    def _format_search_items(items: list[dict[str, Any]], max_results: int = 5) -> str:
        lines: list[str] = []
        for idx, it in enumerate(items[:max_results], start=1):
            title = str(it.get("title") or "").strip()
            snippet = str(it.get("content") or it.get("snippet") or "").strip()
            url = str(it.get("url") or "").strip()
            if not (title or snippet):
                continue
            row = f"{idx}. {title}: {snippet}" if snippet else f"{idx}. {title}"
            if url:
                row += f" ({url})"
            lines.append(row)
        return "\n".join(lines)

    async def _query_searxng(
        self,
        base_url: str,
        query: str,
        max_results: int = 5,
        headers: dict[str, str] | None = None,
        timeout: float = 3.0,
    ) -> list[dict[str, str]]:
        search_url = base_url if base_url.rstrip("/").endswith("/search") else f"{base_url.rstrip('/')}/search"
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=1.5), follow_redirects=True) as client:
            resp = await client.get(
                search_url,
                params={"q": query, "format": "json", "language": "zh-CN"},
                headers=headers or {},
            )
            resp.raise_for_status()
            data = resp.json()
        raw_items = data.get("results") or data.get("data") or []
        return [
            {
                "title": str(r.get("title") or ""),
                "url": str(r.get("url") or ""),
                "snippet": str(r.get("content") or r.get("snippet") or ""),
            }
            for r in raw_items[:max_results]
            if isinstance(r, dict)
        ]

    async def _free_web_search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        """Zero-API-key built-in search fallback (Bing CN HTML -> DuckDuckGo HTML)."""
        encoded = quote_plus(query)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.search_timeout_seconds, connect=3.0),
            follow_redirects=True,
            headers=_BROWSER_HEADERS,
        ) as client:
            # 1. Bing (works reliably across CN & global networks without API key)
            for bing_url in (
                f"https://cn.bing.com/search?q={encoded}&setlang=zh-hans",
                f"https://www.bing.com/search?q={encoded}",
            ):
                try:
                    resp = await client.get(bing_url)
                    if resp.status_code == 200 and resp.text:
                        parser = _BingHTMLParser()
                        parser.feed(resp.text)
                        if parser.results:
                            return parser.results[:max_results]
                except Exception as exc:
                    logger.debug("Free Bing search attempt failed (%s): %s", bing_url, exc)

            # 2. DuckDuckGo HTML fallback (global networks)
            try:
                resp = await client.get(f"https://html.duckduckgo.com/html/?q={encoded}")
                if resp.status_code == 200 and resp.text:
                    parser = _DDGHTMLParser()
                    parser.feed(resp.text)
                    if parser.results:
                        return parser.results[:max_results]
            except Exception as exc:
                logger.debug("Free DDG HTML search failed: %s", exc)
        return []

    async def fast_search(self, session: AsyncSession, query: str, max_results: int = 5) -> str:
        """Execute search across configured slot -> SearXNG -> zero-API built-in fallback."""
        query = (query or "").strip()
        if not query:
            return ""

        if self._search_caller is not None:
            items = await self._search_caller(query)
            return self._format_search_items(items or [], max_results=max_results)

        # Tier 1: Configured 'web_search' slot in Model Control Center
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == "web_search", ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        providers = list((await session.execute(stmt)).scalars().all())
        for provider in providers:
            api_key = decrypt_secret(provider.api_key_enc, self.master_key)
            extra = provider.extra_config or {}
            headers = {"Content-Type": "application/json"}
            if isinstance(extra.get("extra_headers"), dict):
                headers.update({str(k): str(v) for k, v in extra["extra_headers"].items()})
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                is_get = (
                    str(extra.get("method", "")).upper() == "GET"
                    or "searx" in provider.name.lower()
                    or "searx" in provider.base_url.lower()
                    or (provider.base_url.rstrip("/").endswith("/search") and not api_key)
                )
                if is_get:
                    items = await self._query_searxng(
                        provider.base_url,
                        query,
                        max_results=max_results,
                        headers=headers,
                        timeout=float(provider.timeout_seconds),
                    )
                else:
                    async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
                        resp = await client.post(
                            provider.base_url,
                            headers=headers,
                            json={"query": query, "max_results": max_results},
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        items = data.get("results") or data.get("data") or []
                formatted = self._format_search_items(items, max_results=max_results)
                if formatted:
                    return formatted
            except Exception as exc:
                logger.warning("Configured web_search provider '%s' failed for '%s': %s", provider.name, query, exc)

        # Tier 2: Bundled / local SearXNG container (no API key required)
        if self.searxng_url:
            try:
                items = await self._query_searxng(self.searxng_url, query, max_results=max_results, timeout=4.0)
                formatted = self._format_search_items(items, max_results=max_results)
                if formatted:
                    return formatted
            except Exception as exc:
                logger.debug("SearXNG endpoint '%s' unavailable: %s", self.searxng_url, exc)

        # Tier 3: Zero-config pure-httpx free search fallback
        if self.default_free_search_enabled:
            items = await self._free_web_search(query, max_results=max_results)
            return self._format_search_items(items, max_results=max_results)

        return ""

    async def fetch_webpage(self, url: str, max_chars: int = 2800) -> str:
        """Fetch and extract clean plain text from a public HTTP(S) webpage (SSRF-protected)."""
        url = (url or "").strip()
        if not _is_public_http_url(url):
            return "无法访问该地址（仅支持公网 HTTP/HTTPS 页面）。"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.search_timeout_seconds, connect=3.0),
                follow_redirects=True,
                headers=_BROWSER_HEADERS,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" in ctype or "text/plain" in ctype:
                    return re.sub(r"\s+", " ", resp.text).strip()[:max_chars]
                parser = _ReadableTextParser()
                parser.feed(resp.text)
                text = parser.text()
                return text[:max_chars] if text else "页面正文为空或需要登录查看。"
        except Exception as exc:
            logger.warning("Webpage fetch failed for '%s': %s", url, exc)
            return f"网页抓取失败: {type(exc).__name__}"

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
