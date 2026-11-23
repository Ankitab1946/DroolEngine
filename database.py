"""Shared SQLAlchemy session factory and non-destructive schema guard.

For the demo, create_all creates *missing* tables; it never migrates existing ones.
An incompatible table is rejected with a readable explanation before seeding.
"""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from common.config import database_connection, postgres_enabled, postgres_schema

DATABASE_URL, connect_args = database_connection()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass


def check_schema_compatibility(connection):
    """Report missing mapped columns, never silently change existing tables."""
    inspector = inspect(connection)
    incompatible = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name, schema=table.schema):
            continue  # create_all will create it
        actual = {column['name'] for column in inspector.get_columns(table.name, schema=table.schema)}
        missing = sorted(set(table.columns.keys()) - actual)
        if missing:
            incompatible.append((table.name, missing))
    if incompatible:
        details = '; '.join(f'{name}: missing {", ".join(columns)}' for name, columns in incompatible)
        raise RuntimeError(
            'Existing DQ database schema does not match this application: ' + details + '. '
            'SQLAlchemy create_all() does NOT upgrade existing tables. '
            'Your existing data was not modified. '
            'For a disposable local SQLite demo, set SQLITE_DB_PATH=dq_demo_fresh.db in .env '
            'and restart (the old database remains intact). '
            'For a database with data you must retain, back it up and apply a reviewed Alembic migration. '
            'Verify that you are running the updated project, where the attribute demo table is '
            'demo_cfv_attribute_masters with column prj_id.'
        )


def init_db():
    from common import models  # noqa: F401
    with engine.begin() as conn:
        if postgres_enabled():
            schema = postgres_schema()
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        check_schema_compatibility(conn)
        Base.metadata.create_all(conn)


def session_scope():
    return SessionLocal()
