"""Connessione SQLite e creazione dello schema."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from pfb_core import models  # noqa: F401  (registra le tabelle nel metadata)


def get_engine(path: str | Path = "data/pfb.sqlite", *, echo: bool = False) -> Engine:
    """Engine SQLite su file; `":memory:"` per i test."""
    if str(path) == ":memory:":
        # una sola connessione condivisa: anche i thread (es. la sincronizzazione) vedono i dati
        engine = create_engine("sqlite://", echo=echo, poolclass=StaticPool,
                               connect_args={"check_same_thread": False})  # fmt: skip
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", echo=echo)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):  # SQLite non applica le FK di default
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


def session(engine: Engine) -> Session:
    return Session(engine)
