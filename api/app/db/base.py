"""Declarative base and async session management."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


def build_engine(database_url: str | None = None):
    """Create an async engine for the configured SQLite database."""
    url = database_url or get_settings().database_url
    return create_async_engine(url, future=True, echo=False)


engine = build_engine()
SessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session with rollback on error."""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables on startup. Migrations are intentionally out of scope."""
    from app import models  # noqa: F401  (register mappers)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)