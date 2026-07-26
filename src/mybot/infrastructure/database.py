"""Shared async engine and session factory built from application settings."""

from math import ceil

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mybot.settings import Settings


def create_database_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={
            "connect_timeout": ceil(settings.database_connect_timeout_seconds),
            "options": (
                "-c statement_timeout="
                f"{ceil(settings.database_read_timeout_seconds * 1000)}"
            ),
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
