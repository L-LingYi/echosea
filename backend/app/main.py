"""Companion System FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import time
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
from app.core.scheduler import CoreScheduler
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService
from app.core.tts_service import DoubaoTTSService
from app.core.update_service import UpdateService
from app.core.web_perception_service import WebPerceptionService
from app.models.db import AdminUser
from app.plugins.sdk import AutoStickerReactionPlugin, PluginManager, RemoteBackupSyncPlugin
from app.security import LoginRateLimiter, hash_password

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _configure_runtime_time_and_logging(settings: Settings, apply_process_tz: bool = True) -> None:
    """Align process timezone (for stdlib logging/time) and ensure timestamps on app/uvicorn logs."""
    if apply_process_tz and settings.app_timezone:
        os.environ["TZ"] = settings.app_timezone
        if hasattr(time, "tzset"):
            time.tzset()

    level = getattr(logging, str(settings.log_level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(logger_name)
        for handler in uv_logger.handlers:
            handler.setFormatter(formatter)


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
    image_fetcher=None,
    enable_background_scheduler: bool = True,
) -> FastAPI:
    settings = settings_override or get_settings()
    _configure_runtime_time_and_logging(settings, apply_process_tz=(settings_override is None))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Reject known-insecure default secrets in production at server startup
        # (kept inside lifespan so importing app.main in tests/CLI without .env still works)
        _INSECURE_DEFAULTS = {
            "change-me-32-byte-master-key-for-aes-gcm!",
            "change-me-jwt-signing-secret-key-2026!",
        }
        if settings.environment == "production" and settings_override is None:
            import logging as _log
            _startup_log = _log.getLogger(__name__)
            if settings.master_encryption_key in _INSECURE_DEFAULTS:
                raise RuntimeError("MASTER_ENCRYPTION_KEY must be changed from the default value in production!")
            if settings.jwt_secret_key in _INSECURE_DEFAULTS:
                raise RuntimeError("JWT_SECRET_KEY must be changed from the default value in production!")
            if settings.admin_default_password in ("companion2026", "ChangeMeStrongPass2026!"):
                _startup_log.warning(
                    "SECURITY WARNING: ADMIN_DEFAULT_PASSWORD is still set to a known default. "
                    "Please change it immediately after first login."
                )
        app.state.settings = settings
        app.state.shutting_down = False
        app.state.in_flight_tasks = set()
        app.state.login_rate_limiter = LoginRateLimiter()

        # 1. Database initialization
        db_mgr = DatabaseManager()
        db_mgr.init(settings.database_url)
        await db_mgr.create_all()
        if db_mgr.engine.dialect.name == "postgresql":
            await db_mgr.apply_migration_files(
                Path(__file__).resolve().parents[2] / "db",
                ("migrations_v1_1.sql", "migrations_v1_2.sql", "migrations_v1_3.sql", "migrations_v1_4.sql", "migrations_v1_5.sql"),
            )
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
        app.state.state_store = store

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
        multimodal_service = MultimodalService(image_fetcher=image_fetcher)
        sticker_service = StickerService()
        tts_service = DoubaoTTSService(master_key=settings.master_encryption_key, synthesizer=tts_synthesizer)
        reflection_service = ReflectionService(model_hub=model_hub, timezone_name=settings.app_timezone)
        web_perception = WebPerceptionService(
            master_key=settings.master_encryption_key,
            feed_fetcher=feed_fetcher,
            search_caller=search_caller,
            searxng_url=settings.searxng_url,
            default_free_search_enabled=settings.default_free_search_enabled,
            search_timeout_seconds=settings.web_search_timeout_seconds,
            webpage_fetch_enabled=settings.webpage_fetch_enabled,
            timezone_name=settings.app_timezone,
        )
        life_schedule = LifeScheduleService(timezone_name=settings.app_timezone)
        backup_service = BackupService(
            master_key=settings.master_encryption_key,
            http_client_factory=backup_http_client_factory,
            timezone_name=settings.app_timezone,
        )
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
            multimodal_service=multimodal_service,
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
            debounce_window_seconds=settings.debounce_window_seconds,
            debounce_max_wait_seconds=settings.debounce_max_wait_seconds,
            bubble_delay_min_seconds=settings.bubble_delay_min_seconds,
            bubble_delay_max_seconds=settings.bubble_delay_max_seconds,
            enable_background_image_caption=settings.enable_background_image_caption,
            session_factory=db_mgr.session_factory,
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

        # 5. Optional Forward WS client loop to SnowLuma & Unified CoreScheduler
        stop_event = asyncio.Event()
        forward_task: asyncio.Task | None = None
        if settings.snowluma_mode in ("forward_ws", "dual") and not force_memory_store:
            async def _on_forward_event(data: dict):
                if app.state.shutting_down:
                    return
                await pipeline.enqueue_or_handle_raw_event(db_mgr.session_factory, data)

            forward_task = asyncio.create_task(adapter.run_forward_ws_loop(_on_forward_event, stop_event))

        core_scheduler = CoreScheduler(
            life_schedule=life_schedule,
            web_perception=web_perception,
            reflection_service=reflection_service,
            backup_service=backup_service,
            alert_service=alert_service,
            update_service=update_service,
            timezone_name=settings.app_timezone,
        )
        app.state.core_scheduler = core_scheduler
        scheduler_task: asyncio.Task | None = None
        if enable_background_scheduler and not force_memory_store:
            scheduler_task = asyncio.create_task(
                core_scheduler.run_loop(db_mgr.session_factory, stop_event, tick_seconds=60.0),
                name="core_scheduler",
            )

        yield

        app.state.shutting_down = True
        stop_event.set()
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await asyncio.wait_for(scheduler_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if forward_task is not None:
            forward_task.cancel()
            try:
                await asyncio.wait_for(forward_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        drain_timeout = float(getattr(settings, "shutdown_drain_timeout_seconds", 12.0))
        pending_ws = [t for t in app.state.in_flight_tasks if not t.done()]
        if pending_ws:
            _, still_ws = await asyncio.wait(pending_ws, timeout=drain_timeout)
            for t in still_ws:
                t.cancel()
            if still_ws:
                await asyncio.gather(*still_ws, return_exceptions=True)

        await adapter.drain_tasks(timeout=min(5.0, drain_timeout))
        await pipeline.debouncer.drain(timeout=3.0)
        await pipeline.drain_background_tasks(timeout=3.0)
        await event_bus.drain_background(timeout=min(5.0, drain_timeout))
        await model_hub.aclose()
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
