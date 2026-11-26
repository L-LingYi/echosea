"""v1.4.4 Tests: Dark Mode, Links-Only Sticker Presets + Local File Upload, Automated Backup Scheduler & README version."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.backup_service import BackupService
from app.core.database import DatabaseManager
from app.main import create_app


def test_v1_4_1_readme_version_and_frontend_dark_mode_and_guides():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "v1.9.4" in readme
    assert "v1.3.3" not in readme

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v141",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="V141TestPassword!",
    )
    app = create_app(settings_override=settings, force_memory_store=True)

    with TestClient(app) as client:
        assert client.get("/healthz").json()["version"] == "1.9.4"
        html = client.get("/").text
        css = client.get("/static/css/app.css").text
        api_js = client.get("/static/js/api.js").text
        # Dark mode support (modularized CSS & JS)
        assert 'data-theme="dark"' in css
        assert "function toggleTheme()" in api_js
        assert "max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain;" in css
        # Links-only sticker recommendations + local upload (no remote preset download button)
        assert "stickerFileInput" in html
        assert "前往仓库下载" in client.get("/static/js/stickers.js").text
        assert "importStickerPreset('" not in html
        # Backup automation + guided wizard
        assert "bkAutoEnabled" in html
        assert "bkInterval" in html
        assert "bkGuideBox" in html

        token = client.post("/api/auth/login", json={"username": "admin", "password": "V141TestPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Sticker presets must NOT contain dead 404 default_data_url
        presets = client.get("/api/stickers/presets", headers=headers).json()
        assert len(presets) >= 3
        for p in presets:
            assert p.get("repo_url", "").startswith("https://")
            assert "default_data_url" not in p

        # Local JSONL file upload import works cleanly (including description-only entries)
        jsonl = "\n".join([
            json.dumps({"url": "https://cdn.example.com/local1.jpg", "description": "笑死我了，太离谱"}, ensure_ascii=False),
            json.dumps({"url": "https://cdn.example.com/local2.png", "tags": ["摸鱼", "摆烂"], "caption": "打工人摸鱼"}, ensure_ascii=False),
        ])
        up = client.post(
            "/api/stickers/presets/upload",
            headers=headers,
            json={"filename": "my_memes.jsonl", "content": jsonl, "limit": 20},
        )
        assert up.status_code == 200, up.text
        assert up.json()["imported_count"] == 2

        # Backup config persists schedule_interval_hours & auto_enabled
        cfg = client.put(
            "/api/ops/backup-config",
            headers=headers,
            json={"provider": "local_only", "remote_path": "/EchoSea/auto", "auto_enabled": True, "schedule_interval_hours": 12, "retention_count": 7},
        ).json()
        assert cfg["auto_enabled"] is True
        assert cfg["schedule_interval_hours"] == 12
        assert cfg["retention_count"] == 7


@pytest.mark.asyncio
async def test_v1_4_1_automated_backup_scheduler_runs_when_due(tmp_path):
    db_mgr = DatabaseManager()
    db_mgr.init("sqlite+aiosqlite:///:memory:")
    await db_mgr.create_all()

    svc = BackupService(master_key="test-key-v141", local_backup_dir=str(tmp_path))
    async with db_mgr.session_factory() as session:
        # Disabled by default -> does nothing
        assert await svc.run_scheduled_if_due(session) is None

        # Enable auto backup (every 6h)
        await svc.save_config(session, {"provider": "local_only", "auto_enabled": True, "schedule_interval_hours": 6, "retention_count": 2})
        await session.commit()

        # First tick -> due immediately, runs backup and sets last_auto_backup_at
        rec1 = await svc.run_scheduled_if_due(session)
        assert rec1 is not None and rec1.status == "success"
        await session.commit()

        # Second tick right away -> not due yet, skips!
        assert await svc.run_scheduled_if_due(session) is None

        # Retention cleanup keeps only newest N files
        for idx in range(4):
            (tmp_path / f"echosea_backup_20261125_00000{idx}.json.gz").write_bytes(b"gz")
        removed = svc.cleanup_old_local_backups(retention_count=2)
        assert removed >= 2
        assert len(list(tmp_path.glob("echosea_backup_*.json.gz"))) == 2


@pytest.mark.asyncio
async def test_sticker_image_endpoint_and_reflection_auto_review(tmp_path):
    from app.models.db import StickerAsset

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    local_img = tmp_path / "cached_sticker.bin"
    local_img.write_bytes(png_bytes)

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-stk",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="StkTestPassword!",
    )
    app = create_app(settings_override=settings, force_memory_store=True)
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "StkTestPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        async def _seed():
            async with app.state.db_manager.session_factory() as s:
                s1 = StickerAsset(
                    phash="a1b2c3d4e5f60718",
                    file_path=str(local_img),
                    source_url="https://example.com/ok.png",
                    emotion_tags=["摸鱼摆烂"],
                    context_hint="聊到上班摸鱼时使用",
                    appearance_count=1,
                    positive_reactions=0,
                    status="pending",
                )
                s2 = StickerAsset(
                    phash="1122334455667788",
                    file_path="",
                    source_url="https://example.com/err.png",
                    emotion_tags=["fun"],
                    context_hint="群聊语境：看这个报错截图",
                    appearance_count=1,
                    positive_reactions=0,
                    status="pending",
                )
                s.add_all([s1, s2])
                await s.commit()
                await s.refresh(s1)
                await s.refresh(s2)
                return s1.id, s2.id

        id1, id2 = await _seed()

        # 1. Image proxy endpoint serves cached local PNG cleanly
        img_resp = client.get(f"/api/stickers/{id1}/image", headers=headers)
        assert img_resp.status_code == 200
        assert img_resp.headers["content-type"] == "image/png"
        assert img_resp.content == png_bytes

        # 2. Nightly reflection auto-reviews pending stickers (approves valid meme, rejects screenshot noise)
        refl = client.post("/api/ops/reflection/run", headers=headers, json={}).json()
        assert refl["stickers_approved"] == 1
        assert refl["stickers_rejected"] == 1

        listed = {x["id"]: x["status"] for x in client.get("/api/stickers", headers=headers).json()}
        assert listed[id1] == "approved"
        assert listed[id2] == "rejected"
