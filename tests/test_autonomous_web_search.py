"""Tests for LLM-autonomous tool-calling web search, filler reply, isolated session, and free fallback."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.alert_service import AlertService
from app.core.model_hub import ModelHubService
from app.core.web_perception_service import WebPerceptionService
from app.main import create_app


@pytest.mark.asyncio
async def test_model_hub_tool_calling_opencode_session_isolation_and_400_fallback(monkeypatch, db_session):
    requests: list[httpx.Request] = []
    reject_tools_once = [False]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if reject_tools_once[0] and "tools" in body:
            return httpx.Response(400, json={"error": "tools unsupported"})
        if body.get("tools"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "我帮你搜搜",
                                "reasoning_content": "need live info",
                                "tool_calls": [
                                    {
                                        "id": "tc_1",
                                        "type": "function",
                                        "function": {"name": "web_search", "arguments": '{"query": "DeepSeek V4"}'},
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 18},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "查到了，刚出的新特性挺香"}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 20},
            },
        )

    client_cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_cls(transport=httpx.MockTransport(respond), **kw))

    master_key = "test-master-key-autonomous-search"
    hub = ModelHubService(master_key, AlertService(master_key))
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "OpenCode-DeepSeek",
            "protocol": "openai_compatible",
            "base_url": "https://opencode.ai/inference/go/openai/v1",
            "model_name": "deepseek-v4.1-flash",
            "api_key": "oc-key",
        },
    )
    await db_session.commit()

    tools = [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}]
    res1 = await hub.chat_complete(
        db_session,
        slot="main_brain",
        messages=[{"role": "user", "content": "DeepSeek V4 怎么样"}],
        session_scope="group:1001",
        tools=tools,
    )
    assert res1.content == "我帮你搜搜"
    assert len(res1.tool_calls) == 1
    assert res1.raw_message.get("reasoning_content") == "need live info"

    res2 = await hub.chat_complete(
        db_session,
        slot="main_brain",
        messages=[{"role": "user", "content": "summarize"}],
        session_scope="search:req_abc",
    )
    assert res2.content == "查到了，刚出的新特性挺香"
    # Verify OpenCode Go isolates normal group chat and background search into different sessions
    assert requests[0].headers["x-opencode-session"] != requests[1].headers["x-opencode-session"]

    # Verify automatic fallback retry without `tools` if a gateway rejects tools with HTTP 400
    reject_tools_once[0] = True
    res3 = await hub.chat_complete(
        db_session,
        slot="main_brain",
        messages=[{"role": "user", "content": "hi"}],
        session_scope="group:1001",
        tools=tools,
    )
    assert res3.content == "查到了，刚出的新特性挺香"
    assert "tools" in json.loads(requests[2].content)
    assert "tools" not in json.loads(requests[3].content)


@pytest.mark.asyncio
async def test_web_perception_searxng_free_fallback_and_ssrf_guard(monkeypatch, db_session):
    bing_html = """
    <html><body><ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://example.com/news1">今日热点新闻标题</a></h2>
        <div class="b_caption"><p>这是来自免 API Bing 解析的摘要内容。</p></div>
      </li>
    </ol></body></html>
    """
    article_html = "<html><body><script>bad()</script><h1>正文标题</h1><p>第一段详细内容。</p></body></html>"

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "searxng:8080/search" in url:
            if "trigger_searxng" in url:
                return httpx.Response(
                    200,
                    json={"results": [{"title": "SearXNG命中", "url": "https://example.com/sx", "content": "聚合结果"}]},
                )
            return httpx.Response(503, text="down")
        if "cn.bing.com/search" in url:
            return httpx.Response(200, text=bing_html)
        if url == "https://example.com/news1":
            return httpx.Response(200, text=article_html, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)

    client_cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_cls(transport=httpx.MockTransport(respond), **kw))

    svc = WebPerceptionService(
        master_key="k",
        searxng_url="http://searxng:8080",
        default_free_search_enabled=True,
    )
    # 1. Local SearXNG tier works with zero API keys
    sx_out = await svc.fast_search(db_session, "trigger_searxng")
    assert "SearXNG命中" in sx_out and "聚合结果" in sx_out

    # 2. When SearXNG is unavailable, automatically falls back to built-in free Bing HTML parser
    free_out = await svc.fast_search(db_session, "今天有啥热点")
    assert "今日热点新闻标题" in free_out
    assert "免 API Bing 解析" in free_out

    # 3. Webpage reader extracts readable text while stripping scripts
    page_text = await svc.fetch_webpage("https://example.com/news1")
    assert "正文标题" in page_text and "第一段详细内容" in page_text
    assert "bad()" not in page_text

    # 4. SSRF protection blocks localhost / private IPs / non-HTTP schemes
    assert "无法访问" in await svc.fetch_webpage("http://127.0.0.1:8000/healthz")
    assert "无法访问" in await svc.fetch_webpage("http://192.168.1.1/admin")
    assert "无法访问" in await svc.fetch_webpage("file:///etc/passwd")


@pytest.mark.asyncio
async def test_autonomous_search_sends_filler_first_and_synthesizes_in_isolated_session():
    call_records: list[dict] = []
    search_queries_received: list[str] = []

    async def fake_search(query: str):
        search_queries_received.append(query)
        return [{"title": "小米15 Ultra 发布", "url": "https://example.com/mi15u", "snippet": "搭载徕卡长焦与新传感器"}]

    async def fake_llm(provider, api_key, messages, temperature, max_tokens, tools=None, session_scope=None, **_kwargs):
        call_records.append(
            {
                "messages": messages,
                "tools": tools,
                "session_scope": session_scope,
            }
        )
        has_tool_msg = any(m.get("role") == "tool" for m in messages)
        if not has_tool_msg:
            # First turn: LLM autonomously decides to search (even though no hardcoded keyword is present!)
            assert tools and any(t["function"]["name"] == "web_search" for t in tools)
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "等我帮你查查看哈",
                            "tool_calls": [
                                {
                                    "id": "call_mi15u",
                                    "type": "function",
                                    "function": {"name": "web_search", "arguments": '{"query": "小米15 Ultra 参数配置"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 25},
            }
        # Second turn: runs in isolated search:<request_id> session with tool output
        assert session_scope and session_scope.startswith("search:req_")
        return {
            "choices": [{"message": {"content": "刚查了下，小米15 Ultra 主打徕卡长焦和新传感器，拍照挺猛的"}}],
            "usage": {"prompt_tokens": 450, "completion_tokens": 32},
        }

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-auto-search",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="AutoSearchTest2026!",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        http_caller=fake_llm,
        search_caller=fake_search,
    )

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AutoSearchTest2026!"},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "main_brain",
                "role": "primary",
                "name": "OpenCode-DeepSeek",
                "base_url": "https://opencode.ai/inference/go/openai/v1",
                "model_name": "deepseek-v4.1-flash",
                "api_key": "sk-test-123456",
            },
        )

        # Notice message has NO hardcoded trigger keyword ("啥梗"/"热搜"/"搜一下")
        resp = client.post(
            "/api/onebot/event?access_token=tok-auto-search",
            json={
                "post_type": "message",
                "message_type": "group",
                "self_id": 888888,
                "group_id": 7788,
                "user_id": 50001,
                "message_id": "msg_mi15u",
                "sender": {"user_id": 50001, "nickname": "阿凯"},
                "message": [{"type": "text", "data": {"text": "小呆呆，小米15 Ultra 配置怎么样？"}}],
            },
        ).json()

        assert resp["handled"] == "direct_pass"
        assert resp["filler"] == "等我帮你查查看哈"
        assert resp["search_queries"] == ["小米15 Ultra 参数配置"]
        assert search_queries_received == ["小米15 Ultra 参数配置"]
        assert len(resp["deliveries"]) >= 2
        assert resp["deliveries"][0]["text"] == "等我帮你查查看哈"
        assert "小米15 Ultra" in resp["bubbles"][0]

        # Verify session isolation between turn 1 (group:7788) and turn 2 (search:req_...)
        assert call_records[0]["session_scope"] == "group:7788"
        assert call_records[1]["session_scope"].startswith("search:")
        assert call_records[0]["session_scope"] != call_records[1]["session_scope"]

        # Verify web_search stage is traced in dashboard traces
        traces = client.get(f"/api/dashboard/traces?request_id={resp['request_id']}", headers=headers).json()
        assert any(t["stage"] == "web_search" and t["status"] == "ok" for t in traces)
