"""Async SQLAlchemy database engine, session management, and safe full-script SQL migration runner."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def split_sql_statements_safe(sql_script: str) -> list[str]:
    """Strip SQL '--' line comments cleanly BEFORE splitting by ';', never dropping SQL after comments."""
    cleaned_lines = []
    for line in sql_script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        cleaned_lines.append(line)
    joined = "\n".join(cleaned_lines)
    return [stmt.strip() for stmt in joined.split(";") if stmt.strip()]


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

    async def run_migration_script(self, sql_script: str) -> int:
        """Execute a full migration script atomically inside a transaction (never skipping commented blocks)."""
        assert self.engine is not None, "DatabaseManager not initialized"
        stmts = split_sql_statements_safe(sql_script)
        if not stmts:
            return 0
        async with self.engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(text(stmt))
        return len(stmts)

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
