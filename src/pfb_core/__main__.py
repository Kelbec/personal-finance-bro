"""CLI di Personal Finance Bro.

python -m pfb_core init                         crea il database e i dati iniziali
python -m pfb_core import "Trade Republic" file.csv
python -m pfb_core import BPER Lista_Movimenti.xls
python -m pfb_core review                       movimenti da categorizzare
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlmodel import select

from pfb_core.db import get_engine, init_db, session
from pfb_core.importer import ImportFailed, import_file
from pfb_core.models import Account
from pfb_core.rules import pending_review
from pfb_core.seed import seed_all


def _cmd_init(args) -> None:
    engine = get_engine(args.db)
    init_db(engine)
    config = Path(args.config)
    with session(engine) as s:
        counts = seed_all(s, config if config.exists() else None)
    print(f"Database pronto in {args.db}: {counts}")
    if not config.exists():
        print("config.toml non trovato: conti non creati (vedi config.example.toml)")


def _cmd_import(args) -> None:
    engine = get_engine(args.db)
    with session(engine) as s:
        account = s.exec(select(Account).where(Account.name == args.account)).first()
        if account is None:
            names = ", ".join(s.exec(select(Account.name)).all())
            raise SystemExit(f"Conto {args.account!r} non trovato. Conti disponibili: {names}")
        try:
            r = import_file(s, account, args.file)
        except ImportFailed as exc:
            raise SystemExit(f"Import non riuscito: {exc}") from exc
    print(
        f"{args.file}: {r.new} nuovi, {r.duplicates} già presenti, {r.trades_new} trade\n"
        f"  categorizzati {r.categorized}, giroconti {r.internal}, da rivedere {r.to_review}"
        f"  → copertura automatica {r.coverage:.0%}"
    )
    if r.balance_saved:
        print("  saldo di fine mese salvato")
    for msg in r.skipped:
        print(f"  saltata: {msg}")


def _cmd_review(args) -> None:
    engine = get_engine(args.db)
    with session(engine) as s:
        pending = pending_review(s)
        for tx in pending:
            who = tx.counterparty or tx.description[:40]
            print(f"{tx.date}  {tx.amount:>10.2f}  {tx.type:<28} {who}")
        print(f"\n{len(pending)} movimenti da rivedere")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pfb_core")
    parser.add_argument("--db", default="data/pfb.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="crea il database e carica categorie, strumenti, conti")
    p_init.add_argument("--config", default="config.toml")
    p_init.set_defaults(func=_cmd_init)

    p_imp = sub.add_parser("import", help="importa un export bancario")
    p_imp.add_argument("account", help="nome del conto, es. 'Trade Republic' o BPER")
    p_imp.add_argument("file")
    p_imp.set_defaults(func=_cmd_import)

    p_rev = sub.add_parser("review", help="elenca i movimenti da categorizzare")
    p_rev.set_defaults(func=_cmd_review)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
