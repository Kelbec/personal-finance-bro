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
