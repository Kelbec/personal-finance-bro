"""Smoke test delle schermate Flet senza client grafico (saltato se flet non è installato)."""

from types import SimpleNamespace

import pytest
from sqlmodel import select

ft = pytest.importorskip("flet")

from pfb_app.views import ImportView, ReviewView  # noqa: E402
from pfb_core.models import Category, Transaction  # noqa: E402


@pytest.fixture
def engine(s):
    return s.get_bind()


def test_import_view_lists_importable_accounts(engine):
    view = ImportView(engine)
    names = sorted(o.text for o in view.account.options)
    assert names == ["BPER", "Trade Republic"]


def test_import_then_review_flow(engine, s, tr_account, tr_csv):
    review = ReviewView(engine)
    assert review.header.value == "Da rivedere (0)"

    imp = ImportView(engine, on_imported=review.refresh)
    imp._run_import(tr_account.id, tr_csv)
    assert any("Da rivedere" in c.value for c in imp.result.controls)
    pending = int(review.header.value.split("(")[1].rstrip(")"))
    assert pending > 0

    # scelta di una categoria dal menu a tendina di una riga
    tx = s.exec(select(Transaction).where(Transaction.counterparty == "Mario Rossi")).one()
    gift = s.exec(select(Category).where(Category.name == "Regali/Beneficenza")).one()
    dropdown = SimpleNamespace(data=tx.id, value=str(gift.id))
    review._on_category(SimpleNamespace(control=dropdown))
    assert review.header.value == f"Da rivedere ({pending - 1})"


def test_mark_internal_from_review(engine, s, tr_account, tr_csv):
    review = ReviewView(engine)
    ImportView(engine, on_imported=review.refresh)._run_import(tr_account.id, tr_csv)
    before = review.header.value
    tx = s.exec(select(Transaction).where(Transaction.counterparty == "Mario Rossi")).one()
    review._on_internal(SimpleNamespace(control=SimpleNamespace(data=tx.id)))
    assert review.header.value != before


def test_dashboard_reflects_import(engine, s, tr_account, tr_csv):
    from pfb_app.views import DashboardView

    dash = DashboardView(engine)
    assert dash.month_picker.value is None
    ImportView(engine)._run_import(tr_account.id, tr_csv)
    dash.refresh()
    assert dash.month_picker.value is not None
    assert len(dash.kpis.controls) >= 5


def test_portfolio_view_renders_and_saves_balance(engine, s, tr_account):
    from pfb_app.views import PortfolioView
    from pfb_core.portfolio import add_initial_position

    view = PortfolioView(engine)
    assert any("Nessun dato" in getattr(c, "value", "") for c in view.allocation.controls)

    import datetime as dt
    from decimal import Decimal

    add_initial_position(s, tr_account, "IE00BK5BQT80", Decimal(10), Decimal(100),
                         dt.date(2026, 8, 31))  # fmt: skip
    view.balance_account.value = str(tr_account.id)
    view.balance_date.value = "2026-08-31"
    view.balance_value.value = "1.234,50".replace(".", "")
    view._on_balance(None)
    assert view.positions.controls  # VWCE + liquidità Trade Republic
    assert any(getattr(t, "value", "").startswith("Patrimonio") for t in
               (c.content.content.controls[0] for c in view.kpis.controls))  # fmt: skip
