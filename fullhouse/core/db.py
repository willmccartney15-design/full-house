"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

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
    return engine


@contextmanager
def session_scope(db_url: str) -> Iterator[Session]:
    engine = init_db(db_url)
    with Session(engine, expire_on_commit=False) as session:
        yield session
