"""Isolated Web Search Tool-Call Orchestrator for Main Brain."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.model_hub import ModelHubService
from app.core.prompt_builder import build_prompt_messages, sanitize_anti_ai_flavor
from app.core.sticker_service import extract_sticker_tags
from app.core.web_perception_service import WebPerceptionService
from app.models.db import PersonaVersion
from app.models.schemas import GatekeeperDecision, MessageEnvelope

logger = logging.getLogger(__name__)

SEARCH_FILLER_PHRASES = (
    "等我查一下哈",
    "我帮你翻翻看",
    "稍等，我去搜搜看",
    "我看看网上咋说的",
)

WEB_SEARCH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "搜索互联网获取最新消息、热梗科普、实时数据或你不确定的事实。"
                "日常闲聊或已有常识即可回答的问题不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "提炼后的简洁精准搜索关键词（不要直接整段复制聊天原话）",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "抓取并阅读指定公网 HTTP/HTTPS 网页的正文内容（用于查看新闻详情或用户发来的链接）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要阅读的公网 http:// 或 https:// 链接",
                    }
                },
                "required": ["url"],
            },
        },
    },
]


class SearchOrchestrator:
    """Executes isolated multi-round web search + webpage fetch sessions without polluting main context."""

    def __init__(self, model_hub: ModelHubService, web_perception: WebPerceptionService | None) -> None:
        self.model_hub = model_hub
        self.web_perception = web_perception

    def active_search_tools(self) -> list[dict[str, Any]] | None:
        if self.web_perception is None:
            return None
        if self.web_perception.webpage_fetch_enabled:
            return WEB_SEARCH_TOOLS
        return [t for t in WEB_SEARCH_TOOLS if t.get("function", {}).get("name") != "fetch_webpage"]

    @staticmethod
    def pick_search_filler(raw_content: str, banned_phrases: list[str], request_id: str) -> str:
        """Choose immediate non-blocking filler text before running web search."""
        text_only, _ = extract_sticker_tags(raw_content or "")
        candidate = re.sub(r"</?tts\s*>", "", text_only, flags=re.IGNORECASE).strip()
        if candidate:
            cleaned = sanitize_anti_ai_flavor(candidate, banned_phrases)
            if cleaned and cleaned != "emmm":
                return cleaned[:60]
        idx = sum(ord(ch) for ch in request_id) % len(SEARCH_FILLER_PHRASES)
        return SEARCH_FILLER_PHRASES[idx]

    @staticmethod
    def parse_tool_args(raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {"query": raw_args.strip()}
        return {}

    async def execute_isolated_search_session(
        self,
        session: AsyncSession,
        persona: PersonaVersion,
        memory_ctx: dict[str, Any],
        envelope: MessageEnvelope,
        decision: GatekeeperDecision,
        briefing_text: str,
        base_messages: list[dict[str, Any]],
        initial_completion: Any,
        request_id: str,
        trace_fn: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[Any, list[str]]:
        """Run tool calls + answer synthesis in a fresh isolated model session (`search:{request_id}`)."""
        search_session_scope = f"search:{request_id}"
        search_messages = list(base_messages)
        current = initial_completion
        queries: list[str] = []
        collected_snippets: list[str] = []
        t_search = time.perf_counter()

        for round_idx in range(2):
            if not current.tool_calls or self.web_perception is None:
                break
            assistant_msg = dict(current.raw_message) if current.raw_message else {}
            assistant_msg.setdefault("role", "assistant")
            assistant_msg.setdefault("content", current.content or None)
            assistant_msg["tool_calls"] = current.tool_calls
            search_messages.append(assistant_msg)

            for tc_idx, tc in enumerate(current.tool_calls[:3]):
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "")
                args = self.parse_tool_args(fn.get("arguments"))
                tc_id = str(tc.get("id") or f"call_{round_idx}_{tc_idx}")

                if fn_name == "web_search":
                    q = str(args.get("query") or envelope.raw_text).strip()
                    queries.append(q)
                    res = await self.web_perception.fast_search(session, q)
                    tool_output = res or "未找到相关公开搜索结果，请如实告知没查到确切信息，不要编造。"
                elif fn_name == "fetch_webpage":
                    url = str(args.get("url") or "").strip()
                    queries.append(url)
                    tool_output = await self.web_perception.fetch_webpage(url)
                else:
                    tool_output = f"未知工具: {fn_name}"

                collected_snippets.append(tool_output)
                search_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": tool_output,
                    }
                )

            next_tools = self.active_search_tools() if round_idx == 0 else None
            try:
                current = await self.model_hub.chat_complete(
                    session=session,
                    slot="main_brain",
                    messages=search_messages,
                    temperature=0.75,
                    max_tokens=600 if decision.action == "direct_pass" else 180,
                    source="production",
                    request_id=request_id,
                    session_scope=search_session_scope,
                    tools=next_tools,
                )
            except Exception as exc:
                logger.warning(
                    "Tool-role synthesis failed in isolated search session (%s); falling back to context injection: %s",
                    search_session_scope,
                    exc,
                )
                fallback_msgs = build_prompt_messages(
                    persona,
                    memory_ctx,
                    envelope,
                    decision,
                    briefing_text=briefing_text,
                    fast_search_text="\n\n".join(collected_snippets)[:4000],
                    enable_search_tools=False,
                )
                current = await self.model_hub.chat_complete(
                    session=session,
                    slot="main_brain",
                    messages=fallback_msgs,
                    temperature=0.75,
                    max_tokens=600 if decision.action == "direct_pass" else 180,
                    source="production",
                    request_id=request_id,
                    session_scope=search_session_scope,
                )
                break

        if trace_fn is not None:
            await trace_fn(
                session,
                request_id,
                envelope.scene,
                envelope.group_id,
                "web_search",
                "ok" if queries else "skipped",
                latency_ms=int((time.perf_counter() - t_search) * 1000),
                meta={"queries": queries, "session_scope": search_session_scope},
            )
        return current, queries
