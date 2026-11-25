"""Async SQLAlchemy database engine and session management."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DatabaseManager:
    def __init__(self) -> None:
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    def init(self, database_url: str, echo: bool = False) -> None:
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        self.engine = create_async_engine(database_url, echo=echo, connect_args=connect_args)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def create_all(self) -> None:
        assert self.engine is not None, "DatabaseManager not initialized"
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        assert self.session_factory is not None, "DatabaseManager not initialized"
        async with self.session_factory() as session:
            yield session


db_manager = DatabaseManager()
