"""Explicit PostgreSQL connections, isolated from production settings."""
from contextlib import contextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
import psycopg
from psycopg.conninfo import conninfo_to_dict


class DatabaseConfigurationError(ValueError):
    pass


class DatabaseConnectionError(RuntimeError):
    pass


def database_url(*, load_local_env=True):
    if load_local_env:
        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    value = (os.environ.get("DATABASE_URL") or "").strip()
    if not value:
        raise DatabaseConfigurationError("DATABASE_URL is required for learning database operations.")
    if not value.startswith(("postgresql://", "postgres://")):
        raise DatabaseConfigurationError("DATABASE_URL must be a PostgreSQL URL.")
    try:
        options = conninfo_to_dict(value)
    except Exception:
        raise DatabaseConfigurationError("DATABASE_URL is not a valid PostgreSQL connection configuration.") from None
    if (options.get("host") or "").endswith(".neon.tech") and options.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise DatabaseConfigurationError("Neon connections require an SSL-enabled DATABASE_URL.")
    return value


@contextmanager
def connect_database(*, load_local_env=True):
    """Commit on success, rollback on failure, always close; never log the URL."""
    url = database_url(load_local_env=load_local_env)
    try:
        connection = psycopg.connect(url, connect_timeout=15)
    except Exception:
        raise DatabaseConnectionError("PostgreSQL connection failed; check configuration and connectivity.") from None
    with connection:
        yield connection
