"""Companion System FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api import alerts, auth, dashboard, memory, onebot, perception, persona, providers, stickers, system_update
from app.config import Settings, get_settings
from app.core.alert_service import AlertService
from app.core.backup_service import BackupService
from app.core.brain_service import CompanionPipeline
from app.core.database import DatabaseManager
from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.life_schedule_service import LifeScheduleService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.multimodal_service import MultimodalService
from app.core.redis_store import StateStore
from app.core.reflection_service import ReflectionService
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService
from app.core.tts_service import DoubaoTTSService
from app.core.update_service import UpdateService
from app.core.web_perception_service import WebPerceptionService
from app.models.db import AdminUser
from app.plugins.sdk import AutoStickerReactionPlugin, PluginManager, RemoteBackupSyncPlugin
from app.security import LoginRateLimiter, hash_password

logger = logging.getLogger(__name__)


def create_app(
    settings_override: Settings | None = None,
    force_memory_store: bool = False,
    http_caller=None,
    tts_synthesizer=None,
    feed_fetcher=None,
    search_caller=None,
    systemone_caller=None,
    update_http_get=None,
    update_http_post=None,
    backup_http_client_factory=None,
    preset_fetcher=None,
) -> FastAPI:
    settings = settings_override or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.shutting_down = False
        app.state.login_rate_limiter = LoginRateLimiter()

        # 1. Database initialization
        db_mgr = DatabaseManager()
        db_mgr.init(settings.database_url)
        await db_mgr.create_all()
        if db_mgr.engine.dialect.name == "postgresql":
            for mig_name in ("migrations_v1_1.sql", "migrations_v1_2.sql"):
                mig_path = Path(__file__).resolve().parents[2] / "db" / mig_name
                await db_mgr.run_migration_script(mig_path.read_text(encoding="utf-8"))
        app.state.db_manager = db_mgr

        # Seed default admin user if table is empty
        async with db_mgr.session_factory() as session:
            existing = (await session.execute(select(AdminUser).limit(1))).scalar_one_or_none()
            if existing is None:
                session.add(
                    AdminUser(
                        username=settings.admin_default_username,
                        password_hash=hash_password(settings.admin_default_password),
                    )
                )
                await session.commit()

        # 2. Redis / In-Memory State Store
        store = StateStore(redis_url=settings.redis_url, force_memory=force_memory_store)
        await store.connect()
        app.state.store = store

        # 3. Core Services (Phases 1 + 2 + 3)
        event_bus = PluginEventBus(default_timeout=settings.plugin_timeout_seconds)
        alert_service = AlertService(master_key=settings.master_encryption_key)
        identity_service = IdentityService()
        gatekeeper = GatekeeperService(
            store=store,
            soft_entry_threshold=settings.soft_entry_threshold,
            max_consecutive_turns=settings.max_consecutive_group_turns,
            attention_hot_seconds=settings.attention_hot_seconds,
            attention_decay_seconds=settings.attention_decay_seconds,
            max_attention_turns=settings.max_attention_group_turns,
            group_topic_warmth_seconds=settings.group_topic_warmth_seconds,
            group_topic_warmth_floor=settings.group_topic_warmth_floor,
        )
        memory_service = MemoryService(dm_bridge_window_minutes=settings.dm_bridge_window_minutes)
        model_hub = ModelHubService(
            master_key=settings.master_encryption_key,
            alert_service=alert_service,
            daily_budget_usd=settings.daily_budget_usd,
            budget_alert_ratio=settings.budget_alert_ratio,
            http_caller=http_caller,
            systemone_caller=systemone_caller,
        )
        adapter = SnowLumaAdapter(
            ws_url=settings.snowluma_ws_url,
            http_url=settings.snowluma_http_url,
            access_token=settings.snowluma_access_token,
            http_access_token=settings.snowluma_http_access_token,
            bot_qq=settings.bot_qq,
        )
        multimodal_service = MultimodalService()
        sticker_service = StickerService()
        tts_service = DoubaoTTSService(master_key=settings.master_encryption_key, synthesizer=tts_synthesizer)
        reflection_service = ReflectionService(model_hub=model_hub)
        web_perception = WebPerceptionService(
            master_key=settings.master_encryption_key,
            feed_fetcher=feed_fetcher,
            search_caller=search_caller,
            searxng_url=settings.searxng_url,
            default_free_search_enabled=settings.default_free_search_enabled,
            search_timeout_seconds=settings.web_search_timeout_seconds,
        )
        life_schedule = LifeScheduleService()
        backup_service = BackupService(master_key=settings.master_encryption_key, http_client_factory=backup_http_client_factory)
        update_service = UpdateService(
            current_version=settings.app_version,
            current_commit=settings.app_git_commit,
            github_repo=settings.update_github_repo,
            git_branch=settings.update_git_branch,
            updater_url=settings.update_agent_url,
            updater_secret=settings.update_webhook_secret,
            alert_service=alert_service,
            http_get=update_http_get,
            http_post=update_http_post,
        )

        pipeline = CompanionPipeline(
            store=store,
            identity_service=identity_service,
            gatekeeper=gatekeeper,
            memory_service=memory_service,
            model_hub=model_hub,
            adapter=adapter,
            event_bus=event_bus,
            sticker_service=sticker_service,
            tts_service=tts_service,
            web_perception=web_perception,
            admin_qq=settings.admin_qq,
            group_rate_limit_per_min=settings.group_rate_limit_per_minute,
            global_rate_limit_per_min=settings.global_rate_limit_per_minute,
            ambient_summary_trigger_count=settings.ambient_summary_trigger_count,
            ideal_gatekeeper_ms=settings.ideal_gatekeeper_ms,
            ideal_reply_ms=settings.ideal_reply_ms,
            gatekeeper_fallback_timeout_seconds=settings.gatekeeper_fallback_timeout_seconds,
            stale_soft_entry_seconds=settings.stale_soft_entry_seconds,
            stale_soft_entry_min_msgs=settings.stale_soft_entry_min_msgs,
            smart_quote_min_intervening=settings.smart_quote_min_intervening,
            smart_quote_min_delay_seconds=settings.smart_quote_min_delay_seconds,
        )

        async def _send_admin_qq(admin_qq: int, text: str) -> bool:
            async with db_mgr.session_factory() as s:
                delivery = await adapter.send_message(s, "private", admin_qq, text)
                await s.commit()
                return delivery.status == "sent"

        alert_service.set_qq_sender(_send_admin_qq)

        app.state.event_bus = event_bus
        app.state.alert_service = alert_service
        app.state.identity_service = identity_service
        app.state.gatekeeper = gatekeeper
        app.state.memory_service = memory_service
        app.state.model_hub = model_hub
        app.state.adapter = adapter
        app.state.multimodal_service = multimodal_service
        app.state.sticker_service = sticker_service
        app.state.tts_service = tts_service
        app.state.reflection_service = reflection_service
        app.state.web_perception = web_perception
        app.state.life_schedule = life_schedule
        app.state.update_service = update_service
        app.state.backup_service = backup_service
        app.state.preset_fetcher = preset_fetcher
        app.state.pipeline = pipeline

        # 4. Load Built-in Modular Plugins onto Event Bus
        plugin_manager = PluginManager(event_bus)
        plugin_manager.load(AutoStickerReactionPlugin(), app.state)
        plugin_manager.load(RemoteBackupSyncPlugin(), app.state)
        app.state.plugin_manager = plugin_manager

        # 5. Optional Forward WS client loop to SnowLuma
        stop_event = asyncio.Event()
        forward_task: asyncio.Task | None = None
        if settings.snowluma_mode in ("forward_ws", "dual") and not force_memory_store:
            async def _on_forward_event(data: dict):
                if app.state.shutting_down:
                    return
                async with db_mgr.session_factory() as s:
                    await pipeline.handle_raw_onebot_event(s, data)
                    await s.commit()

            forward_task = asyncio.create_task(adapter.run_forward_ws_loop(_on_forward_event, stop_event))

        async def _backup_scheduler_loop() -> None:
            while not app.state.shutting_down:
                try:
                    await asyncio.sleep(600)
                    if app.state.shutting_down:
                        break
                    async with db_mgr.session_factory() as s:
                        await backup_service.run_scheduled_if_due(s, alert_service=alert_service)
                        await s.commit()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("Automated backup scheduler tick failed: %s", exc)

        backup_task = asyncio.create_task(_backup_scheduler_loop())

        yield

        app.state.shutting_down = True
        backup_task.cancel()
        stop_event.set()
        if forward_task is not None:
            forward_task.cancel()
            try:
                await asyncio.wait_for(forward_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await store.close()
        await db_mgr.dispose()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(providers.router)
    app.include_router(memory.router)
    app.include_router(persona.router)
    app.include_router(alerts.router)
    app.include_router(stickers.router)
    app.include_router(perception.router)
    app.include_router(system_update.router)
    app.include_router(onebot.router)

    @app.get("/healthz")
    async def healthz():
        result = {"status": "ok", "version": settings.app_version, "gateway": "SnowLuma"}
        if settings.app_git_commit:
            result["commit"] = settings.app_git_commit
        return result

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/")
        async def serve_index():
            return FileResponse(str(frontend_dir / "index.html"))

    return app


app = create_app()
