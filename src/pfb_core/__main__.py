"""CLI di Personal Finance Bro.

python -m pfb_core init                         crea il database e i dati iniziali
python -m pfb_core import "Trade Republic" file.csv
python -m pfb_core import BPER Lista_Movimenti.xls
python -m pfb_core review                       movimenti da categorizzare
python -m pfb_core position "Trade Republic" IE00BK5BQT80 120.5 98.40 --data 2026-08-31
python -m pfb_core balance "Trade Republic" 2026-08-31 1250.00
python -m pfb_core prices                       scarica i prezzi (uv sync --extra prices)
python -m pfb_core snapshot                     ricalcola gli snapshot di fine mese
python -m pfb_core portfolio                    riepilogo del patrimonio
python -m pfb_core storico-cashflow CashFlow.xlsx           movimenti dai fogli Google
python -m pfb_core storico-patrimonio Asset_allocation.xlsx snapshot mensili e posizioni
python -m pfb_core sync-sheet [--prova]        invia i movimenti al foglio Google condiviso
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

from sqlmodel import select

from pfb_core.db import get_engine, init_db, session
from pfb_core.history import (
    import_asset_allocation,
    import_cashflow,
    initial_positions_from_snapshots,
    instruments_at,
)
from pfb_core.importer import ImportFailed, import_file
from pfb_core.models import Account
from pfb_core.portfolio import (
    add_initial_position,
    build_all_snapshots,
    portfolio_summary,
    reconcile_initial_positions,
    set_balance,
)
from pfb_core.prices import fetch_prices
from pfb_core.rules import pending_review
from pfb_core.seed import seed_all
from pfb_core.sheets_sync import (
    GspreadClient,
    SheetLayoutError,
    load_sheet_config,
    plan_sync,
    run_sync,
)


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


def _account(s, name: str) -> Account:
    account = s.exec(select(Account).where(Account.name == name)).first()
    if account is None:
        names = ", ".join(s.exec(select(Account.name)).all())
        raise SystemExit(f"Conto {name!r} non trovato. Conti disponibili: {names}")
    return account


def _date(text: str | None) -> dt.date:
    return dt.date.fromisoformat(text) if text else dt.date.today()


def _cmd_position(args) -> None:
    with session(get_engine(args.db)) as s:
        t = add_initial_position(s, _account(s, args.account), args.isin, Decimal(args.shares),
                                 Decimal(args.avg_cost), _date(args.data))  # fmt: skip
        print(f"Posizione di partenza: {t.shares} quote di {args.isin} al {t.date}")


def _cmd_balance(args) -> None:
    with session(get_engine(args.db)) as s:
        snap = set_balance(s, _account(s, args.account), _date(args.data), Decimal(args.value))
        print(f"Saldo {args.account} al {snap.month}: {snap.value} €")


def _cmd_prices(args) -> None:
    with session(get_engine(args.db)) as s:
        r = fetch_prices(s, start=_date(args.da) if args.da else None)
    for ticker, n in r.saved.items():
        print(f"{ticker}: {n} prezzi nuovi")
    for ticker in r.missing_symbol:
        print(f"{ticker}: manca il simbolo Yahoo (config.toml → [[instruments]])")
    for ticker, err in r.errors.items():
        print(f"{ticker}: errore — {err}")


def _cmd_snapshot(args) -> None:
    with session(get_engine(args.db)) as s:
        warnings = build_all_snapshots(s, _date(args.fino))
    for w in warnings:
        print(f"  ! {w}")
    print("Snapshot aggiornati")


def _cmd_portfolio(args) -> None:
    with session(get_engine(args.db)) as s:
        p = portfolio_summary(s, _date(args.data))
    print(f"Patrimonio al {p.day}: {p.net_worth:,.2f} €")
    print(f"  titoli {p.securities:,.2f} €  ·  liquidità {p.total_cash:,.2f} €")
    print(f"  costo di carico {p.invested:,.2f} €  ·  P&L {p.pnl:+,.2f} €")
    if p.xirr is not None and p.annualized:
        print(f"  rendimento annuo (XIRR) dal {p.xirr_since}: {p.xirr:.2%}")
    elif p.period_return is not None:
        print(f"  rendimento dal {p.xirr_since} (non annualizzato): {p.period_return:+.2%}")
    print()
    print(f"  {'':<6} {'quote':>12}  {'prezzo medio':>12}  {'valore':>13}")
    for pos in p.positions:
        value = f"{pos.value:>11,.2f} €" if pos.value is not None else "  senza prezzo"
        pnl = f"{pos.pnl_pct:+.1%}" if pos.pnl_pct is not None else ""
        print(
            f"  {pos.instrument.ticker:<6} {pos.shares:>12.4f}  {pos.avg_price:>10,.2f} €"
            f"  {value}  {pnl}"
        )
    for name, value in p.cash.items():
        print(f"  {name:<20} {value:>11,.2f} €")
    print()
    for k, v in sorted(p.allocation.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<10} {v:.1%}")


def _cmd_history_cashflow(args) -> None:
    engine = get_engine(args.db)
    init_db(engine)
    with session(engine) as s:
        r = import_cashflow(s, args.file, args.fogli)
    print(f"Fogli letti: {', '.join(r.sheets) or 'nessuno'}")
    print(f"{r.new} movimenti nuovi, {r.duplicates} già presenti")
    for owner, n in r.by_owner.items():
        print(f"  Storico {owner}: {n}")
    for label, n in r.unknown_categories.items():
        print(f"  ! categoria sconosciuta {label!r}: {n} movimenti saltati")


def _cmd_history_portfolio(args) -> None:
    engine = get_engine(args.db)
    init_db(engine)
    with session(engine) as s:
        broker = _account(s, args.broker)
        r = import_asset_allocation(s, args.file, broker)
        if not r.months:
            raise SystemExit("Nessun foglio anno (es. '2025') con valori trovato")
        print(f"{r.snapshots} valori mensili, da {r.months[0]:%m/%Y} a {r.last_month:%m/%Y}")
        for name in r.created_accounts:
            print(f"  conto creato: {name}")
        for name in r.created_instruments:
            print(f"  strumento creato (senza ISIN): {name}")
        for name in r.closed_accounts:
            print(f"  conto non più nel foglio, saldo azzerato: {name}")
        if args.senza_posizioni:
            return
        start = r.last_month
        fetched = fetch_prices(s, start=start - dt.timedelta(days=10), end=start,
                               isins=instruments_at(s, broker, start))  # fmt: skip
        for ticker, err in fetched.errors.items():
            print(f"  ! prezzi {ticker}: {err}")
        pos = initial_positions_from_snapshots(s, broker, start)
        print(f"Posizioni di partenza al {start} (costo di carico = prezzo del giorno):")
        for ticker, shares, value in pos.created:
            print(f"  {ticker:<6} {shares:>14.8f} quote  {value:>11,.2f} €")
        for ticker in pos.missing_price:
            print(f"  ! {ticker}: nessun prezzo al {start}, posizione non creata")


def _cmd_sync_sheet(args) -> None:
    cfg = load_sheet_config(args.config)
    if cfg is None:
        raise SystemExit("Sincronizzazione non configurata: aggiungi [google_sheet] a config.toml")
    engine = get_engine(args.db)
    init_db(engine)
    with session(engine) as s:
        plan = plan_sync(s, cfg)
        for owner, n in plan.no_tab.items():
            print(f"  ! {owner}: {n} movimenti senza scheda in [google_sheet.schede]")
        if not plan.rows:
            print("Niente da inviare: il foglio è aggiornato")
            return
        print(f"{len(plan.rows)} movimenti dal {plan.first} al {plan.last}")
        try:
            result = run_sync(s, plan, GspreadClient(cfg), write=not args.prova)
        except (SheetLayoutError, RuntimeError) as exc:
            raise SystemExit(f"Sincronizzazione non possibile: {exc}") from exc
    for tab, layout in result.layouts.items():
        cols = ", ".join(f"{name}={chr(65 + j)}" for name, j in layout.columns.items())
        kind = "testo" if layout.amount_as_text else "numero"
        print(f"  {tab}: intestazione alla riga {layout.header_row} ({cols}; importo come {kind})")
    for rows in result.preview.values():
        for row in rows[: args.righe]:
            print("    " + " | ".join(str(v) for v in row))
        if len(rows) > args.righe:
            print(f"    ... e altre {len(rows) - args.righe}")
    verb = "da scrivere" if args.prova else "scritte"
    for tab, n in result.written.items():
        print(f"  {tab}: {n} righe {verb}")
    print(f"  già presenti nel foglio: {result.already_there}")
    if args.prova:
        print("Prova: nessuna riga scritta")


def _cmd_reconcile(args) -> None:
    with session(get_engine(args.db)) as s:
        rows = reconcile_initial_positions(s, apply=not args.prova)
        if not rows:
            print("Nessuna posizione di partenza: quote e prezzo medio vengono tutti dai trade")
        for r in rows:
            esito = ("tolta: si usano i trade" if r.removed
                     else "da togliere: i trade la coprono" if r.covered
                     else "resta: i trade non coprono la posizione")  # fmt: skip
            print(
                f"  {r.ticker:<6} al {r.day}: stimate {r.initial_shares:.4f}, dai trade "
                f"{r.traded_shares:.4f} ({r.coverage:.1%}) → {esito}"
            )
        if any(r.removed for r in rows):
            build_all_snapshots(s)
            print("Snapshot ricalcolati")
    if args.prova:
        print("Prova: nessuna modifica")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # € e accenti nel terminale di Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
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

    p_pos = sub.add_parser("position", help="posizione di partenza su uno strumento")
    p_pos.add_argument("account")
    p_pos.add_argument("isin")
    p_pos.add_argument("shares", help="quote possedute")
    p_pos.add_argument("avg_cost", help="prezzo medio di carico")
    p_pos.add_argument("--data", help="data della posizione (YYYY-MM-DD), default oggi")
    p_pos.set_defaults(func=_cmd_position)

    p_bal = sub.add_parser("balance", help="saldo di un conto a una data (ancora)")
    p_bal.add_argument("account")
    p_bal.add_argument("data", help="YYYY-MM-DD; vale come saldo di fine mese")
    p_bal.add_argument("value")
    p_bal.set_defaults(func=_cmd_balance)

    p_pr = sub.add_parser("prices", help="scarica i prezzi mancanti da Yahoo Finance")
    p_pr.add_argument("--da", help="scarica dal giorno indicato (YYYY-MM-DD)")
    p_pr.set_defaults(func=_cmd_prices)

    p_sn = sub.add_parser("snapshot", help="ricalcola gli snapshot di fine mese")
    p_sn.add_argument("--fino", help="ultimo giorno da considerare, default oggi")
    p_sn.set_defaults(func=_cmd_snapshot)

    p_pf = sub.add_parser("portfolio", help="riepilogo del patrimonio")
    p_pf.add_argument("--data", help="data di riferimento, default oggi")
    p_pf.set_defaults(func=_cmd_portfolio)

    p_hc = sub.add_parser("storico-cashflow", help="movimenti storici dai fogli Google (.xlsx)")
    p_hc.add_argument("file")
    p_hc.add_argument(
        "--fogli",
        nargs="+",
        help="fogli da leggere; default: tutti quelli "
        "con colonne Causale, Categoria, Importo, Data, ..., Intestatario",
    )
    p_hc.set_defaults(func=_cmd_history_cashflow)

    p_hp = sub.add_parser(
        "storico-patrimonio", help="valori mensili dai fogli anno e posizioni di partenza"
    )
    p_hp.add_argument("file")
    p_hp.add_argument("--broker", default="Trade Republic", help="conto dei titoli")
    p_hp.add_argument(
        "--senza-posizioni",
        action="store_true",
        help="importa solo lo storico, senza creare le posizioni di partenza",
    )
    p_hp.set_defaults(func=_cmd_history_portfolio)

    p_sync = sub.add_parser("sync-sheet", help="invia i movimenti al foglio Google condiviso")
    p_sync.add_argument("--config", default="config.toml")
    p_sync.add_argument(
        "--prova", action="store_true", help="legge il foglio e mostra le righe, senza scrivere"
    )
    p_sync.add_argument("--righe", type=int, default=10, help="righe mostrate in prova")
    p_sync.set_defaults(func=_cmd_sync_sheet)

    p_rec = sub.add_parser(
        "riconcilia",
        help="dopo l'import della storia completa del broker, toglie le "
        "posizioni di partenza stimate che i trade ricostruiscono già",
    )
    p_rec.add_argument("--prova", action="store_true", help="mostra il confronto senza modifiche")
    p_rec.set_defaults(func=_cmd_reconcile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
