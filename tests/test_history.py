"""Storico dai fogli: workbook finti generati al volo, nessun dato reale."""

import datetime as dt
from decimal import Decimal

import openpyxl
import pytest
from sqlmodel import select

from pfb_core.history import (
    MONTHS,
    import_asset_allocation,
    import_cashflow,
    initial_positions_from_snapshots,
)
from pfb_core.metrics import month_summary
from pfb_core.models import Account, AccountKind, Instrument, Snapshot, Trade, Transaction
from pfb_core.portfolio import build_all_snapshots, cash_balance, portfolio_summary
from pfb_core.prices import set_price

D = Decimal
HEADER = ["Causale", "Categoria", "Importo", "Data", "Mese", "Intestatario"]
VWCE = "IE00BK5BQT80"


def _cashflow_xlsx(path, sheets: dict[str, list[tuple]]):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(HEADER)
        for causale, cat, amount, day, owner in rows:
            ws.append([causale, cat, amount, dt.datetime.combine(day, dt.time()), day.month, owner])
    ws = wb.create_sheet("Riepilogo")  # foglio con altre colonne: ignorato
    ws.append(["Data", "Movimento", "Entrata/Uscita"])
    wb.save(path)
    return path


ROWS_A = [
    ("Stipendio", "🤑 Stipendio/Pensione", 2000, dt.date(2025, 1, 27), "Anna"),
    ("Spesa", "🍲 Alimentari/Igiene", 50.5, dt.date(2025, 1, 10), "Anna"),
    ("Caffè", "🍔 Ristorante/Asporto", 1.2, dt.date(2025, 1, 11), "Anna"),
    ("Caffè", "🍔 Ristorante/Asporto", 1.2, dt.date(2025, 1, 11), "Anna"),  # doppio legittimo
    ("PAC", "📈 Investimento", 300, dt.date(2025, 1, 15), "Anna"),
    ("Rimborso", "🟢 Entrata extra fuori budget", 40, dt.date(2025, 1, 20), "Anna"),
    ("Cinema", " 🎬 Intrattenimento/Socialità", 9, dt.date(2025, 1, 5), "Bea"),
]


@pytest.fixture
def cashflow_file(tmp_path):
    return _cashflow_xlsx(
        tmp_path / "CashFlow.xlsx",
        {
            "CashFlowTotale": ROWS_A,
            # foglio personale: stessi movimenti di Anna più uno che manca nel totale
            "CashFlowAnna": [r for r in ROWS_A if r[4] == "Anna"]
            + [("Gadget", "🎮 Hobbie/Gadget", 15, dt.date(2025, 2, 1), "Anna")],
        },
    )


def test_cashflow_union_signs_and_owners(s, cashflow_file):
    r = import_cashflow(s, cashflow_file)
    assert r.sheets == ["CashFlowTotale", "CashFlowAnna"]
    assert r.new == 8  # 7 del totale (incluso il doppio caffè) + il gadget del foglio di Anna
    assert r.by_owner == {"Anna": 7, "Bea": 1}
    assert not r.unknown_categories

    anna = s.exec(select(Account).where(Account.name == "Storico Anna")).one()
    assert anna.kind == AccountKind.STORICO
    txs = s.exec(select(Transaction).where(Transaction.account_id == anna.id)).all()
    amounts = sorted(t.amount for t in txs)
    assert amounts == sorted([D("2000"), D("-50.50"), D("-1.20"), D("-1.20"), D("-300"),
                              D("40"), D("-15")])  # fmt: skip

    jan = month_summary(s, dt.date(2025, 1, 1))
    assert jan.income == D("2000")
    assert jan.saved == D("300")
    assert jan.expenses == D("50.50") + D("2.40") + D("9")
    assert jan.uncategorized == 0


def test_cashflow_is_idempotent(s, cashflow_file):
    import_cashflow(s, cashflow_file)
    again = import_cashflow(s, cashflow_file)
    assert again.new == 0
    assert again.duplicates == 8


def test_cashflow_reports_unknown_category(s, tmp_path):
    f = _cashflow_xlsx(tmp_path / "x.xlsx",
                       {"CF": [("?", "🦄 Unicorni", 5, dt.date(2025, 3, 1), "Anna")]})  # fmt: skip
    r = import_cashflow(s, f)
    assert r.new == 0
    assert r.unknown_categories == {"🦄 Unicorni": 1}


# --- patrimonio --------------------------------------------------------------
MONTH_HEADER = [None, *MONTHS, "Last Update"]


def _year_sheet(wb, year, cash: dict, bonds: dict, stocks: dict):
    ws = wb.create_sheet(str(year))
    ws.append(MONTH_HEADER)
    ws.append(["CASH", 0.1, 0.1])  # riga di sezione con le percentuali: ignorata
    for name, values in cash.items():
        ws.append([name, *values])
    ws.append(["Total", 999, 999])
    ws.append([])
    ws.append(["BONDS"])
    for name, values in bonds.items():
        ws.append([name, *values])
    ws.append(["Total", 999])
    ws.append(["STOCKS"])
    for name, values in stocks.items():
        ws.append([name, *values])
    ws.append(["Total", 999])
    ws.append([None, 0.05])  # variazione mensile senza etichetta
    ws.append(["AGGREGATED"])
    ws.append(["Total", 12345])


@pytest.fixture
def allocation_file(tmp_path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    wb.create_sheet("ISIN_mapping")
    _year_sheet(
        wb, 2025,
        cash={"Conto BPER": [100, 110], "Prepagata BPER": [20, 25],
              "Trade Repubblic": [1000, 1100]},
        bonds={"Fondo Arca Obbligazioni": [500, 0]},
        stocks={"VWCE": [1600, 1700]},
    )  # fmt: skip
    _year_sheet(
        wb, 2026,
        cash={"Conto BPER": [None, None, None, None, None, None, None, 120],
              "Trade Repubblic": [None] * 7 + [1500]},
        bonds={},
        stocks={"VWCE": [None] * 7 + [1800], "EIMI": [None] * 7 + [480]},
    )  # fmt: skip
    path = tmp_path / "Asset_allocation.xlsx"
    wb.save(path)
    return path


def test_allocation_snapshots(s, tr_account, bper_account, allocation_file):
    r = import_asset_allocation(s, allocation_file, tr_account)
    assert r.created_accounts == ["Prepagata BPER"]
    assert r.created_instruments == ["Fondo Arca Obbligazioni"]
    assert r.last_month == dt.date(2026, 8, 31)
    assert r.months[0] == dt.date(2025, 1, 31)
    assert r.closed_accounts == ["Prepagata BPER (08/2026)"]

    snaps = s.exec(select(Snapshot)).all()
    assert all(x.is_manual for x in snaps)
    feb = {(x.account_id, x.isin): x.value for x in snaps if x.month == dt.date(2025, 2, 28)}
    assert feb[(bper_account.id, None)] == D("110")
    assert feb[(tr_account.id, VWCE)] == D("1700")
    # un valore 0 per un titolo è "nessuna posizione", non uno snapshot
    assert not any(isin and isin.startswith("FONDO-") for _, isin in feb)

    # i saldi del foglio fanno da ancora per la liquidità
    assert cash_balance(s, bper_account, dt.date(2026, 8, 31)) == D("120")
    prepagata = s.exec(select(Account).where(Account.name == "Prepagata BPER")).one()
    assert prepagata.kind == AccountKind.PREPAGATA
    assert cash_balance(s, prepagata, dt.date(2026, 8, 31)) == D("0")


def test_allocation_is_idempotent(s, tr_account, allocation_file):
    import_asset_allocation(s, allocation_file, tr_account)
    n = len(s.exec(select(Snapshot)).all())
    import_asset_allocation(s, allocation_file, tr_account)
    assert len(s.exec(select(Snapshot)).all()) == n


def test_initial_positions_from_last_month(s, tr_account, allocation_file):
    import_asset_allocation(s, allocation_file, tr_account)
    aug = dt.date(2026, 8, 31)
    set_price(s, VWCE, aug, D("160"))
    r = initial_positions_from_snapshots(s, tr_account, aug)
    assert r.missing_price == ["EIMI"]
    assert r.created == [("VWCE", D("11.25000000"), D("1800.00"))]
    t = s.exec(select(Trade).where(Trade.isin == VWCE)).one()
    assert (t.origin, t.date, t.price) == ("iniziale", aug, D("160"))


def test_history_and_xirr_start_at_initial_positions(s, tr_account, allocation_file, cashflow_file):
    import_cashflow(s, cashflow_file)
    import_asset_allocation(s, allocation_file, tr_account)
    aug, sept = dt.date(2026, 8, 31), dt.date(2026, 9, 30)
    set_price(s, VWCE, aug, D("160"))
    set_price(s, "IE00BKM4GZ66", aug, D("48"))
    initial_positions_from_snapshots(s, tr_account, aug)
    set_price(s, VWCE, sept, D("176"))
    set_price(s, "IE00BKM4GZ66", sept, D("48"))

    warnings = build_all_snapshots(s, sept)
    assert not any("Storico" in w for w in warnings)  # i conti storici non hanno saldo

    p = portfolio_summary(s, sept)
    assert "Storico Anna" not in p.cash
    assert p.xirr_since == aug  # non dal 2025: lo storico non dice quanto è stato versato
    assert p.period_return == pytest.approx((1800 * 1.1 + 480) / 2280 - 1, abs=1e-4)

    # lo storico resta: patrimonio di febbraio 2025 dai valori del foglio
    feb = s.exec(select(Snapshot).where(Snapshot.month == dt.date(2025, 2, 28))).all()
    assert sum(x.value for x in feb) == D("110") + D("25") + D("1100") + D("1700")


def test_xirr_without_initial_positions_uses_all_trades(s, tr_account):
    for i, (day, price) in enumerate([(dt.date(2025, 1, 2), 100), (dt.date(2025, 7, 1), 105)]):
        s.add(Trade(account_id=tr_account.id, isin=VWCE, date=day, shares=D("1"),
                    price=D(price), amount=D(-price), source_id=f"t{i}"))  # fmt: skip
    s.commit()
    set_price(s, VWCE, dt.date(2026, 1, 2), D("121"))
    p = portfolio_summary(s, dt.date(2026, 1, 2))
    assert p.xirr_since == dt.date(2025, 1, 2)
    assert p.annualized
    assert 0.15 < p.xirr < 0.25


def test_unknown_instrument_gets_internal_key(s, tr_account, allocation_file):
    import_asset_allocation(s, allocation_file, tr_account)
    inst = s.get(Instrument, "FONDO-FONDO-ARCA-OBBLIGAZIONI")
    assert inst is not None and inst.asset_class == "bond"
