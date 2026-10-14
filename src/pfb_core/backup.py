"""Copia del database tra PC e telefono.

Il telefono tiene un suo SQLite: per partire con i dati del PC si importa il file pfb.sqlite,
e dopo un import fatto sul telefono lo si riesporta verso il PC. Prima di sostituire il
database corrente ne viene salvata una copia accanto.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

# tabelle senza le quali il file non è un database dell'app
REQUIRED_TABLES = {"account", "transaction", "category"}


@dataclass
class DbInfo:
    transactions: int
    accounts: int
    last_date: dt.date | None


class InvalidDatabase(ValueError):
    pass


def inspect(path: str | Path) -> DbInfo:
    """Controlla che il file sia un database dell'app e ne riassume il contenuto."""
    path = Path(path)
    if not path.is_file():
        raise InvalidDatabase(f"{path.name}: file inesistente")
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as con:
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = REQUIRED_TABLES - tables
            if missing:
                raise InvalidDatabase(
                    f"{path.name}: non è un database di Personal Finance Bro "
                    f"(mancano {', '.join(sorted(missing))})"
                )
            n_tx, last = con.execute('SELECT COUNT(*), MAX(date) FROM "transaction"').fetchone()
            (n_acc,) = con.execute("SELECT COUNT(*) FROM account").fetchone()
    except sqlite3.DatabaseError as exc:
        raise InvalidDatabase(f"{path.name}: non è un database SQLite ({exc})") from exc
    return DbInfo(n_tx, n_acc, dt.date.fromisoformat(last[:10]) if last else None)


def snapshot_bytes(db_path: str | Path) -> bytes:
    """Copia coerente del database (backup API di SQLite), anche con l'app aperta."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "copia.sqlite"
        src = sqlite3.connect(Path(db_path))
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return out.read_bytes()


def replace_db(new_file: str | Path, db_path: str | Path, now: dt.datetime | None = None) -> Path:
    """Sostituisce il database con `new_file` dopo averlo validato; ritorna la copia del vecchio.

    Chi chiama deve aver chiuso le connessioni (engine.dispose()) prima.
    """
    inspect(new_file)
    db_path = Path(db_path)
    now = now or dt.datetime.now()
    backup = db_path.with_name(f"{db_path.stem}.backup-{now:%Y%m%d-%H%M%S}{db_path.suffix}")
    if db_path.exists():
        shutil.copy2(db_path, backup)
    tmp = db_path.with_suffix(".nuovo")
    shutil.copyfile(new_file, tmp)
    tmp.replace(db_path)
    return backup
