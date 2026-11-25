"""v1.2.0 Industry-Standard Online Update Tests:
- HMAC-SHA256 signature verification & 60s replay rejection
- Local repo mode vs GitHub public repo Release check
- Signed trigger, single-flight concurrency lock, HMAC callback, and rollback alerting
"""
from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.core.update_service import sign_updater_payload, verify_updater_signature
from app.main import create_app


def test_hmac_signature_verification_and_replay_protection():
    secret = "super-strong-hmac-secret"
    body = b'{"job_uid":"upd_1"}'
    now_ts = str(int(time.time()))
    sig = sign_updater_payload(secret, now_ts, body)

    assert verify_updater_signature(secret, now_ts, sig, body) is True
    # Tampered body rejected
    assert verify_updater_signature(secret, now_ts, sig, b'{"job_uid":"upd_2"}') is False
    # Stale timestamp (>60s old) rejected (anti-replay)
    old_ts = str(int(time.time()) - 120)
    old_sig = sign_updater_payload(secret, old_ts, body)
    assert verify_updater_signature(secret, old_ts, old_sig, body) is False


def test_online_update_github_check_trigger_callback_and_rollback_alert():
    posted_requests = []

    async def fake_github_get(url: str, headers: dict[str, str]):
        if url.startswith("http://host.docker.internal:9099/result/"):
            return {"status": "running"}
        assert "api.github.com/repos/demo/companion/releases/latest" in url
        return {
            "tag_name": "v9.9.0",
            "body": "新增语音情绪微调与性能优化",
            "html_url": "https://github.com/demo/companion/releases/tag/v9.9.0",
        }

    async def fake_updater_post(url: str, headers: dict[str, str], body: bytes):
        posted_requests.append({"url": url, "headers": headers, "body": body})
        # Verify the outbound trigger signature is valid!
        assert verify_updater_signature(
            "hmac-test-secret",
            headers["X-Updater-Timestamp"],
            headers["X-Updater-Signature"],
            body,
        )
        return {"ok": True, "message": "宿主机更新守护进程已受理"}

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_http_url="",
        admin_default_username="admin",
        admin_default_password="UpdateTestPass2026!",
        update_github_repo="demo/companion",
        update_git_branch="main",
        update_agent_url="http://host.docker.internal:9099",
        update_webhook_secret="hmac-test-secret",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        update_http_get=fake_github_get,
        update_http_post=fake_updater_post,
    )

    with TestClient(app) as client:
        html = client.get("/").text
        assert "checkSystemUpdate()" in html and "triggerSystemUpdate()" in html

        token = client.post("/api/auth/login", json={"username": "admin", "password": "UpdateTestPass2026!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Check for updates against GitHub public release
        chk = client.get("/api/system/update/check", headers=headers).json()
        assert chk["mode"] == "github_public"
        assert chk["update_available"] is True
        assert chk["latest_version"] == "v9.9.0"

        # 2. Trigger signed update
        trig = client.post("/api/system/update/trigger", headers=headers, json={"target_ref": "v9.9.0"})
        assert trig.status_code == 200
        job_uid = trig.json()["job_uid"]
        assert trig.json()["status"] == "running"
        assert len(posted_requests) == 1

        # 3. Concurrent trigger while job is running -> rejected (400)
        dup = client.post("/api/system/update/trigger", headers=headers, json={"target_ref": "v9.9.0"})
        assert dup.status_code == 400

        # 4. Unsigned callback rejected (403)
        bad_cb = client.post("/api/system/update/callback", json={"job_uid": job_uid, "status": "succeeded"})
        assert bad_cb.status_code == 403

        # 5. Signed rollback callback updates job status AND creates critical alert!
        cb_payload = {
            "job_uid": job_uid,
            "status": "rolled_back",
            "rolled_back": True,
            "from_commit": "abc1234",
            "to_commit": "def5678",
            "log_summary": "healthz timeout; auto-rolled back to abc1234",
        }
        cb_bytes = json.dumps(cb_payload, separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        sig = sign_updater_payload("hmac-test-secret", ts, cb_bytes)
        ok_cb = client.post(
            "/api/system/update/callback",
            content=cb_bytes,
            headers={"Content-Type": "application/json", "X-Updater-Timestamp": ts, "X-Updater-Signature": sig},
        )
        assert ok_cb.status_code == 200
        assert ok_cb.json()["rolled_back"] is True

        # Verify audit job + critical alert recorded
        st = client.get("/api/system/update/status", headers=headers).json()
        assert st["jobs"][0]["status"] == "rolled_back"
        alerts = client.get("/api/alerts/events", headers=headers).json()
        assert any(a["category"] == "online_update" and a["severity"] == "critical" for a in alerts)
