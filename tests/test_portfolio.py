import datetime as dt
from decimal import Decimal

import pytest
from sqlmodel import select

from pfb_core.importer import import_file
from pfb_core.models import Instrument, Price, Snapshot
from pfb_core.portfolio import (
    CASH,
    add_initial_position,
    build_all_snapshots,
    build_snapshots,
    cash_balance,
    net_worth_series,
    portfolio_summary,
    positions,
    set_balance,
    xirr,
)
from pfb_core.prices import fetch_prices, price_on, set_price

VWCE, EIMI = "IE00BK5BQT80", "IE00BKM4GZ66"
D = Decimal


def fake_provider(prices: dict[str, list[tuple[dt.date, float]]]):
    calls = []

    def provider(symbol, start, end):
        calls.append((symbol, start, end))
        return [(d, c) for d, c in prices.get(symbol, []) if start <= d <= end]

    provider.calls = calls
    return provider


# --- XIRR --------------------------------------------------------------------
def test_xirr_one_year_ten_percent():
    r = xirr([(dt.date(2025, 1, 1), -1000.0), (dt.date(2026, 1, 1), 1100.0)])
    assert r == pytest.approx(0.10, abs=1e-6)


def test_xirr_needs_both_signs():
    assert xirr([(dt.date(2025, 1, 1), -1000.0)]) is None
    assert xirr([(dt.date(2025, 1, 1), 10.0), (dt.date(2026, 1, 1), 5.0)]) is None


# --- posizioni ---------------------------------------------------------------
def test_initial_position_plus_imported_trades(s, tr_account, tr_csv):
    add_initial_position(s, tr_account, VWCE, D("100"), D("120"), dt.date(2026, 8, 31))
    import_file(s, tr_account, tr_csv)
    pos = {p.instrument.ticker: p for p in positions(s, dt.date(2026, 9, 30))}
    # 100 iniziali + 4.817535 dal piano di accumulo del 16/09
    assert pos["VWCE"].shares == D("104.8175350000")
    assert pos["VWCE"].cost == D("12800.00")
    # prezzo di fallback: ultimo trade importato (non il costo medio iniziale)
    assert pos["VWCE"].price == D("166.06")
    assert pos["EIMI"].shares > 0


def test_initial_position_is_idempotent(s, tr_account):
    add_initial_position(s, tr_account, VWCE, D("100"), D("120"), dt.date(2026, 8, 31))
    add_initial_position(s, tr_account, VWCE, D("90"), D("120"), dt.date(2026, 8, 31))
    assert positions(s, dt.date(2026, 9, 1))[0].shares == D("90")


def test_unknown_instrument_rejected(s, tr_account):
    with pytest.raises(ValueError):
        add_initial_position(s, tr_account, "XX0000000000", D(1), D(1), dt.date(2026, 1, 1))


def test_sell_reduces_cost_proportionally(s, tr_account):
    from pfb_core.models import Trade

    add_initial_position(s, tr_account, VWCE, D("10"), D("100"), dt.date(2026, 1, 31))
    s.add(Trade(account_id=tr_account.id, isin=VWCE, date=dt.date(2026, 2, 10), shares=D("-4"),
                price=D("150"), amount=D("600"), source_id="sell1"))  # fmt: skip
    s.commit()
    p = positions(s, dt.date(2026, 2, 28))[0]
    assert p.shares == D("6")
    assert p.cost == D("600")
    assert p.pnl == D("300.00")  # 6 × 150 − 600


# --- prezzi ------------------------------------------------------------------
def test_fetch_prices_stores_and_is_incremental(s, tr_account):
    add_initial_position(s, tr_account, VWCE, D("10"), D("100"), dt.date(2026, 8, 31))
    provider = fake_provider({"VWCE.DE": [(dt.date(2026, 8, 31), 160.0),
                                          (dt.date(2026, 9, 30), 170.5)]})  # fmt: skip
    r = fetch_prices(s, provider=provider, end=dt.date(2026, 9, 30))
    assert r.saved == {"VWCE": 2}
    r2 = fetch_prices(s, provider=provider, end=dt.date(2026, 9, 30))
    assert r2.saved == {}  # già aggiornato: nessuna richiesta
    assert price_on(s, VWCE, dt.date(2026, 9, 15)) == (D("160.0"), dt.date(2026, 8, 31))


def test_fetch_prices_reports_missing_symbol_and_errors(s, tr_account):
    wslv = "JE00BQRFDY49"
    add_initial_position(s, tr_account, wslv, D("1"), D("10"), dt.date(2026, 8, 31))
    add_initial_position(s, tr_account, VWCE, D("1"), D("10"), dt.date(2026, 8, 31))

    def broken(symbol, start, end):
        raise ConnectionError("offline")

    r = fetch_prices(s, provider=broken)
    assert r.missing_symbol == ["WSLV"]
    assert "offline" in r.errors["VWCE"]


def test_manual_price(s, tr_account):
    set_price(s, VWCE, dt.date(2026, 9, 30), D("171"))
    assert s.exec(select(Price)).one().source == "manuale"


def test_config_sets_yahoo_symbol(s, tmp_path):
    from pfb_core.seed import seed_from_config

    cfg = tmp_path / "c.toml"
    cfg.write_text('[owner]\nname = "Test"\n[[instruments]]\nisin = "JE00BQRFDY49"\n'
                   'yahoo_symbol = "XYZ.DE"\n', encoding="utf-8")  # fmt: skip
    seed_from_config(s, cfg)
    assert s.get(Instrument, "JE00BQRFDY49").yahoo_symbol == "XYZ.DE"


# --- liquidità e snapshot ----------------------------------------------------
def test_cash_balance_from_anchor_plus_movements(s, tr_account, tr_csv):
    assert cash_balance(s, tr_account, dt.date(2026, 9, 30)) is None
    set_balance(s, tr_account, dt.date(2026, 8, 31), D("1000"))
    r = import_file(s, tr_account, tr_csv)
    assert r.new > 0
    from pfb_core.models import Transaction

    moves = sum(t.amount for t in s.exec(select(Transaction)).all())
    assert cash_balance(s, tr_account, dt.date(2026, 9, 30)) == (D("1000") + moves).quantize(
        D("0.01")
    )


def test_bper_export_balance_is_anchor(s, bper_account, bper_xls):
    import_file(s, bper_account, bper_xls)
    assert cash_balance(s, bper_account, dt.date(2026, 9, 30)) == D("86.01")
    assert cash_balance(s, bper_account, dt.date(2026, 10, 15)) == D("86.01")


def test_build_snapshots_and_series(s, tr_account, bper_account, tr_csv, bper_xls):
    add_initial_position(s, tr_account, VWCE, D("100"), D("120"), dt.date(2026, 8, 31))
    set_balance(s, tr_account, dt.date(2026, 8, 31), D("500"))
    import_file(s, tr_account, tr_csv)
    import_file(s, bper_account, bper_xls)
    provider = fake_provider({"VWCE.DE": [(dt.date(2026, 8, 31), 160.0),
                                          (dt.date(2026, 9, 30), 170.0)],
                              "IS3N.DE": [(dt.date(2026, 9, 30), 48.0)]})  # fmt: skip
    fetch_prices(s, provider=provider, end=dt.date(2026, 9, 30))

    warnings = build_all_snapshots(s, dt.date(2026, 9, 30))
    assert not any("VWCE" in w for w in warnings)

    sept = s.exec(select(Snapshot).where(Snapshot.month == dt.date(2026, 9, 30),
                                         Snapshot.isin == VWCE)).one()  # fmt: skip
    assert sept.value == (D("104.817535") * D("170")).quantize(D("0.01"))

    series = net_worth_series(s)
    assert [m.month for m in series] == [dt.date(2026, 8, 31), dt.date(2026, 9, 30)]
    assert series[0].by_class["stock"] == D("16000.00")
    assert CASH in series[1].by_class

    # rieseguire non duplica e non tocca i saldi manuali
    build_snapshots(s, dt.date(2026, 9, 30))
    manual = s.exec(select(Snapshot).where(Snapshot.is_manual == True)).all()  # noqa: E712
    assert {m.value for m in manual} >= {D("500"), D("86.01")}


def test_portfolio_summary(s, tr_account, tr_csv):
    add_initial_position(s, tr_account, VWCE, D("100"), D("120"), dt.date(2026, 8, 31))
    set_balance(s, tr_account, dt.date(2026, 8, 31), D("500"))
    import_file(s, tr_account, tr_csv)
    provider = fake_provider({"VWCE.DE": [(dt.date(2026, 8, 31), 160.0),
                                          (dt.date(2026, 9, 30), 176.0)],
                              "IS3N.DE": [(dt.date(2026, 9, 30), 48.0)]})  # fmt: skip
    fetch_prices(s, provider=provider, end=dt.date(2026, 9, 30))
    build_all_snapshots(s, dt.date(2026, 9, 30))

    p = portfolio_summary(s, dt.date(2026, 9, 30))
    assert p.net_worth == p.securities + p.total_cash
    assert "Trade Republic" in p.cash
    assert p.pnl > 0
    assert sum(p.allocation.values()) == pytest.approx(1)
    assert p.xirr_since == dt.date(2026, 8, 31)
    assert p.xirr is not None and p.xirr > 0
    assert not p.annualized
    assert 0 < p.period_return < 0.15  # VWCE +10% in un mese, non annualizzato


def test_initial_position_price_is_not_a_market_price(s, tr_account):
    add_initial_position(s, tr_account, VWCE, D("10"), D("100"), dt.date(2026, 8, 31))
    assert price_on(s, VWCE, dt.date(2026, 9, 30)) is None


def test_rebuild_drops_stale_snapshots(s, tr_account):
    from pfb_core.models import Trade

    s.add(Trade(account_id=tr_account.id, isin=VWCE, date=dt.date(2026, 9, 2), shares=D("1"),
                price=D("100"), amount=D("-100"), source_id="b1"))  # fmt: skip
    s.commit()
    build_snapshots(s, dt.date(2026, 9, 30))
    assert s.exec(select(Snapshot).where(Snapshot.isin == VWCE)).one()
    trade = s.exec(select(Trade)).one()
    s.delete(trade)
    s.commit()
    build_snapshots(s, dt.date(2026, 9, 30))
    assert s.exec(select(Snapshot).where(Snapshot.isin == VWCE)).first() is None
