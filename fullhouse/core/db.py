"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from fullhouse.core import models  # noqa: F401  (registers tables)

_engines: dict[str, Engine] = {}


def get_engine(db_url: str) -> Engine:
    if db_url not in _engines:
        kwargs = {"connect_args": {"check_same_thread": False}} if db_url.startswith("sqlite") else {}
        _engines[db_url] = create_engine(db_url, **kwargs)
    return _engines[db_url]


def init_db(db_url: str) -> Engine:
    engine = get_engine(db_url)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _backfill_tokens(engine)
    return engine


def _backfill_tokens(engine: Engine) -> None:
    """Clients created before owner pages existed get their private token."""
    from fullhouse.core.models import new_token

    with engine.begin() as conn:
        rows = conn.execute(text('SELECT id FROM client WHERE owner_token IS NULL')).fetchall()
        for (client_id,) in rows:
            conn.execute(text("UPDATE client SET owner_token = :t WHERE id = :i"), {"t": new_token(), "i": client_id})


def _add_missing_columns(engine: Engine) -> None:
    """create_all never alters existing tables; add columns introduced since a database was created."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = column.type.compile(dialect=engine.dialect)
                default = column.default.arg if column.default is not None and column.default.is_scalar else None
                clause = f" DEFAULT '{default}'" if isinstance(default, str) else (
                    f" DEFAULT {default}" if default is not None else "")
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}{clause}'))


@contextmanager
def session_scope(db_url: str) -> Iterator[Session]:
    engine = init_db(db_url)
    with Session(engine, expire_on_commit=False) as session:
        yield session
