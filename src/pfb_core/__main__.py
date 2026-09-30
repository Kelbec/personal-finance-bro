"""CLI minima: `python -m pfb_core init` crea il database e carica i dati iniziali."""

from __future__ import annotations

import argparse
from pathlib import Path

from pfb_core.db import get_engine, init_db, session
from pfb_core.seed import seed_all


def main() -> None:
    parser = argparse.ArgumentParser(prog="pfb_core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="crea il database e carica categorie, strumenti, conti")
    p_init.add_argument("--db", default="data/pfb.sqlite")
    p_init.add_argument("--config", default="config.toml")
    args = parser.parse_args()

    if args.cmd == "init":
        engine = get_engine(args.db)
        init_db(engine)
        config = Path(args.config)
        with session(engine) as s:
            counts = seed_all(s, config if config.exists() else None)
        print(f"Database pronto in {args.db}: {counts}")
        if not config.exists():
            print("config.toml non trovato: conti non creati (vedi config.example.toml)")


if __name__ == "__main__":
    main()
