from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.backend.settings import Settings


def create_database_engine(settings: Settings) -> AsyncEngine | None:
    database_url = settings.async_database_url()
    if database_url is None:
        return None

    connect_args = (
        {"ssl": "require"}
        if database_url.drivername == "postgresql+asyncpg"
        else {}
    )
    return create_async_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )


async def check_database_connection(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
