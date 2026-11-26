"""v1.4.0 Tests: EchoSea (回声海) Rename, Sticker Public Presets, Aliyun Drive Backup Config & Hot-Topic CRUD."""
from __future__ import annotations

import json
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class _FakeAliyunClient:
    """In-memory async httpx client stub for Aliyun Drive OpenAPI & WebDAV tests."""

    def __init__(self, calls: list[tuple[str, str]]):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url: str, headers=None, json=None):
        self.calls.append(("POST", url))
        req = httpx.Request("POST", url)
        if url.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "ali-at-123", "refresh_token": "ali-rt-new"}, request=req)
        if url.endswith("/user/getDriveInfo"):
            return httpx.Response(200, json={"backup_drive_id": "drv-backup-001", "resource_drive_id": "drv-res-002"}, request=req)
        if url.endswith("/openFile/create"):
            if json and json.get("type") == "folder":
                return httpx.Response(200, json={"file_id": f"folder_{json['name']}"}, request=req)
            return httpx.Response(
                200,
                json={
                    "file_id": "file-999",
                    "upload_id": "up-888",
                    "rapid_upload": False,
                    "part_info_list": [{"part_number": 1, "upload_url": "https://upload.alipan.test/part1"}],
                },
                request=req,
            )
        if url.endswith("/openFile/complete"):
            return httpx.Response(200, json={"file_id": "file-999", "status": "available"}, request=req)
        return httpx.Response(404, json={"error": "unknown"}, request=req)

    async def put(self, url: str, content=None, auth=None, headers=None):
        self.calls.append(("PUT", url))
        return httpx.Response(200, text="ok", request=httpx.Request("PUT", url))

    async def request(self, method: str, url: str, headers=None, auth=None):
        self.calls.append((method, url))
        return httpx.Response(201 if method == "MKCOL" else 200, text="ok", request=httpx.Request(method, url))


def test_v1_4_0_echosea_branding_sticker_presets_hot_sources_and_aliyun_backup():
    aliyun_calls: list[tuple[str, str]] = []

    async def fake_preset_fetcher(url: str) -> str:
        return json.dumps(
            [
                {"url": "https://cdn.example.com/meme1.jpg", "tags": ["大受震撼", "吐槽"], "description": "李安大受震撼"},
                {"url": "https://cdn.example.com/meme2.gif", "tags": "猫咪,好奇", "caption": "小猫咪双手合十"},
            ],
            ensure_ascii=False,
        )

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v140",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="V140TestPassword!",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        backup_http_client_factory=lambda: _FakeAliyunClient(aliyun_calls),
        preset_fetcher=fake_preset_fetcher,
    )

    with TestClient(app) as client:
        # 1. EchoSea (回声海) branding & v1.9.3
        h = client.get("/healthz").json()
        assert h["version"] == "1.9.3"
        html = client.get("/").text
        assert "EchoSea" in html and "回声海" in html
        assert "推荐公开表情包图库" in html
        assert "异地备份配置" in html
        assert "热点渠道源管理" in html

        token = client.post("/api/auth/login", json={"username": "admin", "password": "V140TestPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Sticker public presets list + on-demand import + deduplication
        presets = client.get("/api/stickers/presets", headers=headers).json()
        assert len(presets) >= 3
        assert any(p["id"] == "emo_visual_data" for p in presets)

        imp1 = client.post(
            "/api/stickers/presets/import",
            headers=headers,
            json={"preset_id": "emo_visual_data", "limit": 10},
        ).json()
        assert imp1["ok"] is True
        assert imp1["imported_count"] == 2
        imp2 = client.post(
            "/api/stickers/presets/import",
            headers=headers,
            json={"preset_id": "emo_visual_data", "limit": 10},
        ).json()
        assert imp2["imported_count"] == 0
        assert imp2["skipped_duplicates"] == 2

        # 3. Hot-topic source presets + CRUD (add, toggle, delete)
        h_presets = client.get("/api/ops/hot-sources/presets", headers=headers).json()
        assert len(h_presets) >= 3
        added = client.post("/api/ops/hot-sources", headers=headers, json=h_presets[0]).json()
        assert added["ok"] is True
        toggled = client.patch(f"/api/ops/hot-sources/{added['id']}", headers=headers, json={"is_enabled": False}).json()
        assert toggled["is_enabled"] is False
        deleted = client.delete(f"/api/ops/hot-sources/{added['id']}", headers=headers).json()
        assert deleted["ok"] is True

        # 4. Aliyun Drive OpenAPI backup config, OAuth URL, connection test & real upload to custom path
        saved_cfg = client.put(
            "/api/ops/backup-config",
            headers=headers,
            json={
                "provider": "aliyun_openapi",
                "remote_path": "/MyCustom/EchoSeaBackups",
                "aliyun_client_id": "ali-cid-001",
                "aliyun_client_secret": "ali-sec-001",
                "aliyun_refresh_token": "ali-rt-initial",
                "aliyun_drive_type": "backup",
            },
        ).json()
        assert saved_cfg["aliyun_authorized"] is True
        assert saved_cfg["remote_path"] == "/MyCustom/EchoSeaBackups"
        assert "ali-rt-initial" not in json.dumps(saved_cfg)  # Secret must stay masked!

        oauth_res = client.post("/api/ops/backup-config/oauth-url", headers=headers, json={}).json()
        assert oauth_res["ok"] is True and "openapi.alipan.com/oauth/authorize" in oauth_res["authorize_url"]

        test_res = client.post("/api/ops/backup-config/test", headers=headers).json()
        assert test_res["ok"] is True and "drv-backup-001" in test_res["message"]

        trig_res = client.post("/api/ops/backups/trigger", headers=headers, json={}).json()
        assert trig_res["ok"] is True
        assert trig_res["status"] == "success"
        assert trig_res["remote_target"].startswith("aliyun_openapi:/MyCustom/EchoSeaBackups/echosea_backup_")
        assert any(m == "PUT" and "upload.alipan.test" in u for m, u in aliyun_calls)

        # Verify BackupRecord persisted
        bks = client.get("/api/ops/backups", headers=headers).json()
        assert len(bks) >= 1 and bks[0]["status"] == "success"


def test_v1_4_0_security_and_jsonl_regressions():
    import pyotp

    jsonl_payload = "\n".join([
        json.dumps({"url": "https://cdn.example.com/jsonl1.jpg", "description": "这只小猫咪看起来非常可爱又好奇"}, ensure_ascii=False),
        json.dumps({"url": "https://cdn.example.com/jsonl2.gif", "caption": "反正我大受震撼，太离谱了"}, ensure_ascii=False),
    ])

    async def fake_jsonl_fetcher(url: str) -> str:
        return jsonl_payload

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v140-reg",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="V140RegPassword!",
    )
    app = create_app(settings_override=settings, force_memory_store=True, preset_fetcher=fake_jsonl_fetcher)

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "V140RegPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Backup filename path traversal must be rejected with HTTP 400
        for bad_name in ("../escape.gz", "../../etc/passwd", "/tmp/evil.gz", "sub/dir.gz", ".hidden.gz"):
            r = client.post("/api/ops/backups/trigger", headers=headers, json={"filename": bad_name})
            assert r.status_code == 400, f"Expected 400 for {bad_name}, got {r.status_code}"

        # 2. TOTP setup must NOT disable already-active 2FA before verification succeeds
        s1 = client.post("/api/auth/totp/setup", headers=headers).json()
        code1 = pyotp.TOTP(s1["secret"]).now()
        en1 = client.post("/api/auth/totp/enable", headers=headers, json={"code": code1})
        assert en1.status_code == 200 and en1.json()["totp_enabled"] is True

        # Re-running setup while 2FA is active must keep totp_enabled=True and old secret working!
        s2 = client.post("/api/auth/totp/setup", headers=headers).json()
        assert s2["pending"] is True
        assert s2["totp_enabled"] is True
        me = client.get("/api/auth/me", headers=headers).json()
        assert me["totp_enabled"] is True
        # Logging in without TOTP must still be rejected (401/403/400), and old TOTP code still works
        login_no_totp = client.post("/api/auth/login", json={"username": "admin", "password": "V140RegPassword!"})
        assert login_no_totp.status_code != 200
        login_old_totp = client.post("/api/auth/login", json={"username": "admin", "password": "V140RegPassword!", "totp_code": pyotp.TOTP(s1["secret"]).now()})
        assert login_old_totp.status_code == 200

        # 3. Multi-line JSONL + description-only (no tags field) must import cleanly with auto-extracted tags
        imp = client.post(
            "/api/stickers/presets/import",
            headers=headers,
            json={"preset_id": "chinese_meme_desc", "limit": 10},
        )
        assert imp.status_code == 200, imp.text
        assert imp.json()["imported_count"] == 2
        stickers = client.get("/api/stickers", headers=headers).json()
        by_url = {s["source_url"]: s for s in stickers}
        assert "可爱" in by_url["https://cdn.example.com/jsonl1.jpg"]["emotion_tags"]
        assert "震撼" in by_url["https://cdn.example.com/jsonl2.gif"]["emotion_tags"]
