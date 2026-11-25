"""Pytest fixtures for Companion System v1.0."""
from __future__ import annotations

import pytest_asyncio

from app.core.database import DatabaseManager
from app.core.redis_store import StateStore


@pytest_asyncio.fixture
async def db_session():
    mgr = DatabaseManager()
    mgr.init("sqlite+aiosqlite:///:memory:")
    await mgr.create_all()
    async with mgr.session_factory() as session:
        yield session
    await mgr.dispose()


@pytest_asyncio.fixture
async def state_store():
    store = StateStore(force_memory=True)
    await store.connect()
    yield store
    await store.close()
