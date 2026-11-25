"""Regression coverage for OpenCode Go's session and DeepSeek output requirements."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.alert_service import AlertService
from app.core.model_hub import ModelHubService


@pytest.mark.asyncio
async def test_go_session_is_stable_for_a_group_and_json_is_gatekeeper_only(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    master_key = "local-example-master-key"
    hub = ModelHubService(master_key, AlertService(master_key))
    provider = SimpleNamespace(
        base_url="https://opencode.ai/inference/go/openai/v1",
        slot="gatekeeper", model_name="deepseek-v4.1-flash", timeout_seconds=5,
    )
    messages = [{"role": "user", "content": "Return JSON for this synthetic sample."}]

    for group in ("group:123456", "group:123456", "group:654321"):
        await hub._call_openai_compatible(
            provider, "test-key", messages, 0.1, 100, session_scope=group, gatekeeper_json=True
        )
    provider.slot = "main_brain"
    await hub._call_openai_compatible(provider, "test-key", messages, 0.7, 250, session_scope="group:123456")

    session_headers = [request.headers["x-opencode-session"] for request in requests]
    assert session_headers[0] == session_headers[1]
    assert session_headers[0].startswith("ses_") and "123456" not in session_headers[0]
    assert session_headers[2] != session_headers[0] != session_headers[3]
    screening = json.loads(requests[0].content)
    assert screening["thinking"] == {"type": "disabled"}
    assert screening["response_format"] == {"type": "json_object"}
    chat = json.loads(requests[3].content)
    assert "thinking" not in chat and "response_format" not in chat
