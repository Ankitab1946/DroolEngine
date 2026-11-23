"""Central environment and connection configuration shared by API and worker.

Loads local .env without overriding explicitly provided Docker/system environment.
Do not log full connection URLs: they include passwords.
"""
import os
import re
from pathlib import Path
from urllib.parse import quote
from sqlalchemy.engine import URL
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def postgres_enabled() -> bool:
    raw = os.getenv("ENABLE_POSTGRES", "false").strip().lower()
    if raw not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError("ENABLE_POSTGRES must be true/false")
    return raw in {"true", "1", "yes", "on"}


def postgres_schema() -> str:
    schema = os.getenv("PG_SCHEMA", "dq_app").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", schema):
        raise ValueError("PG_SCHEMA must be a safe unquoted PostgreSQL identifier")
    if schema.lower() in {"public", "pg_catalog", "information_schema"} or schema.lower().startswith("pg_"):
        raise ValueError("Use a dedicated DQ schema, not a system/public schema")
    return schema


def database_connection():
    """Return (SQLAlchemy URL, safe driver connect args)."""
    if not postgres_enabled():
        sqlite_path = os.getenv("SQLITE_DB_PATH", "dq_demo.db").strip()
        if not sqlite_path or sqlite_path == ":memory:":
            raise ValueError("SQLITE_DB_PATH must be a file path")
        path = Path(sqlite_path).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        # URL.create handles spaces, Windows drive letters and special characters.
        return URL.create("sqlite", database=str(path)), {"check_same_thread": False}
    host = os.getenv("PG_HOST", "localhost").strip()
    database = os.getenv("PG_DATABASE", "dq").strip()
    user = os.getenv("POSTGRES_USER", "dq")
    password = os.getenv("POSTGRES_PASSWORD", "")
    try:
        port = int(os.getenv("PG_PORT", "5432"))
    except ValueError as exc:
        raise ValueError("PG_PORT must be an integer") from exc
    if not host or not database or not user or not (1 <= port <= 65535):
        raise ValueError("Invalid PostgreSQL host, database, user or port")
    sslmode = os.getenv("POSTGRES_SSLMODE", "prefer").strip()
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("Invalid POSTGRES_SSLMODE")
    schema = postgres_schema()
    url = f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}"
    # psycopg's 'options' safely selects the configured schema for unqualified ORM tables.
    return url, {"sslmode": sslmode, "options": f"-csearch_path={schema},public"}
