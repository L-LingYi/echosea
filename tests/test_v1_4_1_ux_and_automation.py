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
    assert "v1.5.2" in readme
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
        assert client.get("/healthz").json()["version"] == "1.5.2"
        html = client.get("/").text
        # Dark mode support
        assert 'data-theme="dark"' in html
        assert "toggleTheme()" in html
        assert "max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain;" in html
        # Links-only sticker recommendations + local upload (no remote preset download button)
        assert "stickerFileInput" in html
        assert "前往仓库下载" in html
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
