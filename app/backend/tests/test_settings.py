from app.backend.settings import Settings


def test_postgres_connection_string_uses_asyncpg_driver() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://backend:encoded%40password@db.example.test:5432/postgres",
    )

    url = settings.async_database_url()

    assert url is not None
    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "encoded@password"


def test_placeholder_connection_string_is_not_used() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql+asyncpg://postgres.ref:[YOUR-PASSWORD]"
            "@aws-0-eu-west-2.pooler.supabase.com:5432/postgres"
        ),
    )

    assert settings.async_database_url() is None


def test_sqlite_async_url_is_supported_for_tests() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
    )

    url = settings.async_database_url()

    assert url is not None
    assert url.drivername == "sqlite+aiosqlite"
