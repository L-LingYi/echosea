"""v1.8.1 Tests: Release 404 logger fallback fix, 3-tier inspect/TOTP confirmation, Tier 3 auto-update scheduler & WebUI."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import httpx
import pyotp
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.alert_service import AlertService
from app.core.database import DatabaseManager
from app.core.scheduler import CoreScheduler
from app.core.update_service import UpdateService, is_patch_upgrade
from app.main import create_app
from app.models.db import AlertChannel, AlertEvent
from app.security import generate_totp_secret


def _load_host_updater_module():
    source = Path(__file__).resolve().parents[1] / "ops" / "updater" / "host_updater.py"
    spec = importlib.util.spec_from_file_location("host_updater_v181", source)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_release_404_fallback_no_logger_nameerror_and_host_commit_lookup():
    async def fake_get(url: str, headers: dict[str, str]):
        if "releases/latest" in url:
            req = httpx.Request("GET", url)
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("Not Found", request=req, response=resp)
        if "commits/main" in url:
            return {
                "sha": "deadbeef1234567890abcdef1234567890abcdef",
                "commit": {"message": "fix: release fallback logger\n\ndetails"},
                "html_url": "https://github.com/L-LingYi/echosea/commit/deadbeef",
            }
        if url.endswith("/check"):
            return {"current_commit": "0000111122223333444455556666777788889999"}
        raise AssertionError(f"Unexpected GET {url}")

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_http_url="",
        admin_default_username="admin",
        admin_default_password="FallbackTestPass2026!",
        app_git_commit="",
        update_github_repo="L-LingYi/echosea",
        update_git_branch="main",
        update_agent_url="http://host.docker.internal:9099",
        update_webhook_secret="b" * 64,
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        update_http_get=fake_get,
    )
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "FallbackTestPass2026!"}).json()["access_token"]
        res = client.get("/api/system/update/check", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        body = res.json()
        assert "error" not in body
        assert body["update_available"] is True
        assert body["latest_version"] == "deadbeef1234"
        assert body["target_ref"] == "deadbeef1234567890abcdef1234567890abcdef"
        assert body["current_commit"].startswith("00001111")


def test_tier2_inspect_and_totp_or_password_confirm_token(tmp_path):
    host_mod = _load_host_updater_module()
    host_mod.STATE = tmp_path
    host_mod.SECRET = "c" * 64

    target_commit = "e" * 40
    diff_sha = "f" * 64

    async def fake_post(url: str, headers: dict[str, str], body: bytes):
        if url.endswith("/inspect"):
            return {
                "from_commit": "1" * 40,
                "to_commit": target_commit,
                "target_version": "1.8.2",
                "update_available": True,
                "tier": 2,
                "requires_confirmation": True,
                "protected_files": ["Dockerfile"],
                "reasons": ["受保护文件变更：Dockerfile"],
                "diff": "+ENV DEMO=1",
                "diff_sha256": diff_sha,
            }
        return {"ok": True}

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_http_url="",
        admin_default_username="admin",
        admin_default_password="ConfirmPass2026!",
        update_github_repo="L-LingYi/echosea",
        update_agent_url="http://host.docker.internal:9099",
        update_webhook_secret="c" * 64,
    )
    app = create_app(settings_override=settings, force_memory_store=True, update_http_post=fake_post)
    with TestClient(app) as client:
        html = client.get("/").text
        ops_js = client.get("/static/js/ops.js").text
        assert "updateConfirmModal" in html
        assert "updAutoEnabled" in html
        assert "openUpdateConfirmModal" in ops_js
        assert "v1.8.1" in html

        tok_login = client.post("/api/auth/login", json={"username": "admin", "password": "ConfirmPass2026!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {tok_login}"}

        insp = client.post("/api/system/update/inspect", headers=headers, json={"target_ref": "main"})
        assert insp.status_code == 200
        assert insp.json()["requires_confirmation"] is True

        # Without TOTP enabled: wrong password rejected, valid password issues valid host token
        bad_pwd = client.post(
            "/api/system/update/confirm-token",
            headers=headers,
            json={"target_ref": "main", "to_commit": target_commit, "diff_sha256": diff_sha, "password": "wrong"},
        )
        assert bad_pwd.status_code == 401

        ok_pwd = client.post(
            "/api/system/update/confirm-token",
            headers=headers,
            json={"target_ref": "main", "to_commit": target_commit, "diff_sha256": diff_sha, "password": "ConfirmPass2026!"},
        )
        assert ok_pwd.status_code == 200
        confirm_tok = ok_pwd.json()["confirm_token"]
        assert host_mod.verify_and_consume_confirm_token(confirm_tok, target_commit, diff_sha) == "admin"

        # Enable TOTP and verify TOTP code is strictly enforced
        setup_res = client.post("/api/auth/totp/setup", headers=headers).json()
        totp_secret = setup_res["secret"]
        code = pyotp.TOTP(totp_secret).now()
        assert client.post("/api/auth/totp/enable", headers=headers, json={"code": code}).status_code == 200

        # Password alone no longer suffices when TOTP is enabled
        rejected_pwd = client.post(
            "/api/system/update/confirm-token",
            headers=headers,
            json={"target_ref": "main", "to_commit": target_commit, "diff_sha256": diff_sha, "password": "ConfirmPass2026!"},
        )
        assert rejected_pwd.status_code == 401

        ok_totp = client.post(
            "/api/system/update/confirm-token",
            headers=headers,
            json={"target_ref": "main", "to_commit": target_commit, "diff_sha256": diff_sha, "totp_code": pyotp.TOTP(totp_secret).now()},
        )
        assert ok_totp.status_code == 200
        assert host_mod.verify_and_consume_confirm_token(ok_totp.json()["confirm_token"], target_commit, diff_sha) == "admin"


def test_is_patch_upgrade_helper():
    assert is_patch_upgrade("1.8.1", "v1.8.2") is True
    assert is_patch_upgrade("1.8.1", "1.8.1") is True
    assert is_patch_upgrade("1.8.1", "v1.9.0") is False
    assert is_patch_upgrade("1.8.1", "v2.0.0") is False
    assert is_patch_upgrade("1.8.1", "deadbeef1234") is False


@pytest.mark.asyncio
async def test_tier3_scheduled_auto_update_boundaries(db_session):
    qq_sent: list[tuple[int, str]] = []
    alert_svc = AlertService(master_key="test-master-key-32-bytes-long-00")

    async def fake_qq(admin_qq: int, text: str) -> bool:
        qq_sent.append((admin_qq, text))
        return True

    alert_svc.set_qq_sender(fake_qq)
    from app.security import encrypt_secret
    db_session.add(
        AlertChannel(
            channel_type="qq_admin",
            name="Admin QQ",
            config_enc=encrypt_secret('{"admin_qq": 123456}', "test-master-key-32-bytes-long-00"),
            is_enabled=True,
        )
    )
    await db_session.flush()

    scenario = {"tag": "v1.8.2", "requires_confirmation": False, "protected_files": [], "commit": "a" * 40}
    triggers: list[dict] = []

    async def fake_get(url: str, headers: dict[str, str]):
        return {"tag_name": scenario["tag"], "body": "patch", "html_url": "https://example.com"}

    async def fake_post(url: str, headers: dict[str, str], body: bytes):
        if url.endswith("/inspect"):
            return {
                "from_commit": "0" * 40,
                "to_commit": scenario["commit"],
                "target_version": scenario["tag"].lstrip("v"),
                "update_available": True,
                "tier": 2 if scenario["requires_confirmation"] else 1,
                "requires_confirmation": scenario["requires_confirmation"],
                "protected_files": scenario["protected_files"],
            }
        triggers.append({"url": url, "body": body})
        return {"ok": True}

    upd_svc = UpdateService(
        current_version="1.8.1",
        current_commit="0" * 40,
        github_repo="L-LingYi/echosea",
        updater_url="http://127.0.0.1:9099",
        updater_secret="d" * 64,
        alert_service=alert_svc,
        http_get=fake_get,
        http_post=fake_post,
    )
    scheduler = CoreScheduler(
        life_schedule=None,
        web_perception=None,
        reflection_service=None,
        backup_service=None,
        alert_service=alert_svc,
        update_service=upd_svc,
    )

    # 1. Disabled by default -> no-op at 04:05
    dt_day1 = datetime(2026, 11, 27, 4, 5, tzinfo=timezone.utc)
    res_off = await scheduler.run_tick(db_session, now=dt_day1)
    assert not any(x.startswith("auto_update") for x in res_off["executed"])
    assert triggers == []

    # 2. Enabled + Tier 2 -> does NOT trigger, sends QQ alert once
    await upd_svc.save_auto_update_config(db_session, {"auto_update_enabled": True, "auto_update_hour": 4})
    scenario.update(tag="v1.8.2", requires_confirmation=True, protected_files=["Dockerfile"], commit="b" * 40)
    res_t2 = await scheduler.run_tick(db_session, now=dt_day1)
    assert "auto_update:notified" in res_t2["executed"]
    assert triggers == []
    assert len(qq_sent) == 1
    assert "Dockerfile" in qq_sent[0][1]

    # 3. Cross-version (1.8.1 -> v1.9.0) even if Tier 1 -> does NOT trigger, sends QQ alert
    dt_day2 = datetime(2026, 11, 28, 4, 5, tzinfo=timezone.utc)
    scenario.update(tag="v1.9.0", requires_confirmation=False, protected_files=[], commit="c" * 40)
    res_cross = await scheduler.run_tick(db_session, now=dt_day2)
    assert "auto_update:notified" in res_cross["executed"]
    assert triggers == []
    assert len(qq_sent) == 2
    assert "跨版本" in qq_sent[1][1]

    # 4. Tier 1 + patch version (1.8.1 -> v1.8.2) -> triggers auto-update!
    dt_day3 = datetime(2026, 11, 29, 4, 5, tzinfo=timezone.utc)
    scenario.update(tag="v1.8.2", requires_confirmation=False, protected_files=[], commit="d" * 40)
    res_ok = await scheduler.run_tick(db_session, now=dt_day3)
    assert "auto_update:triggered" in res_ok["executed"]
    assert len(triggers) == 1
