"""Async SQLAlchemy database engine, session management, and safe full-script SQL migration runner."""
from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path
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

    async def run_migration_script(self, sql_script: str) -> None:
        """Execute the complete PostgreSQL script in an asyncpg transaction."""
        assert self.engine is not None, "DatabaseManager not initialized"
        async with self.engine.connect() as conn:
            raw_connection = await conn.get_raw_connection()
            driver_connection = raw_connection.driver_connection
            async with driver_connection.transaction():
                await driver_connection.execute(sql_script)

    async def apply_versioned_migrations(
        self,
        migrations: list[tuple[str, str]],
    ) -> list[str]:
        """Apply ordered (version, sql_script) migrations once, tracked by checksum in `schema_migrations`."""
        assert self.engine is not None, "DatabaseManager not initialized"
        dialect = self.engine.dialect.name
        applied: list[str] = []

        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version VARCHAR(128) PRIMARY KEY, "
                    "checksum VARCHAR(64) NOT NULL, "
                    "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )

        for version, sql_script in migrations:
            checksum = hashlib.sha256(sql_script.encode("utf-8")).hexdigest()
            async with self.engine.begin() as conn:
                existing = (
                    await conn.execute(
                        text("SELECT checksum FROM schema_migrations WHERE version = :v"),
                        {"v": version},
                    )
                ).scalar_one_or_none()
            if existing == checksum:
                continue
            if existing is not None:
                raise RuntimeError(f"Migration checksum mismatch for '{version}'")

            if dialect == "postgresql":
                await self.run_migration_script(sql_script)
            else:
                async with self.engine.begin() as conn:
                    for stmt in [s.strip() for s in sql_script.split(";") if s.strip()]:
                        await conn.execute(text(stmt))

            async with self.engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO schema_migrations (version, checksum) VALUES (:v, :c)"),
                    {"v": version, "c": checksum},
                )
            applied.append(version)

        return applied

    async def apply_migration_files(self, db_dir: Path, filenames: tuple[str, ...]) -> list[str]:
        items: list[tuple[str, str]] = []
        for name in filenames:
            path = db_dir / name
            if path.exists():
                items.append((name, path.read_text(encoding="utf-8")))
        return await self.apply_versioned_migrations(items)

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
