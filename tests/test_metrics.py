import datetime as dt
import itertools
from decimal import Decimal

from sqlmodel import select

from pfb_core.metrics import available_months, month_bounds, month_summary, monthly_trend
from pfb_core.models import Category, Transaction

_ids = itertools.count(1)


def _add(s, account, cat_name, amount, date, internal=False):
    cat = s.exec(select(Category).where(Category.name == cat_name)).one() if cat_name else None
    s.add(
        Transaction(
            account_id=account.id,
            date=date,
            amount=Decimal(amount),
            type="TEST",
            category_id=cat.id if cat else None,
            is_internal=internal,
            source_id=f"t{next(_ids)}",
        )
    )
    s.commit()


def test_month_bounds_handles_december():
    assert month_bounds(dt.date(2026, 12, 15)) == (dt.date(2026, 12, 1), dt.date(2027, 1, 1))


def test_summary_totals_and_savings_rate(s, bper_account):
    d = dt.date(2026, 9, 10)
    _add(s, bper_account, "Stipendio/Pensione", "2000", d)
    _add(s, bper_account, "Investimento", "-300", d)
    _add(s, bper_account, "Uscita extra fuori budget", "-100", d)
    _add(s, bper_account, "Entrata extra fuori budget", "50", d)  # non è reddito
    _add(s, bper_account, None, "-20", d)
    _add(s, bper_account, "Stipendio/Pensione", "999", d, internal=True)  # giroconto
    _add(s, bper_account, "Stipendio/Pensione", "500", dt.date(2026, 10, 1))  # altro mese

    m = month_summary(s, d)
    assert m.income == Decimal(2000)
    assert m.expenses == Decimal(100)
    assert m.saved == Decimal(300)
    assert m.uncategorized == 1
    assert m.savings_rate == Decimal("0.95")


def test_by_category_sorted_with_budget_and_refunds(s, bper_account):
    d = dt.date(2026, 9, 5)
    groceries = s.exec(select(Category).where(Category.group == "Spese primarie")).first()
    other = s.exec(select(Category).where(Category.group == "Spese secondarie")).first()
    groceries.monthly_budget = Decimal(100)
    s.add(groceries)
    _add(s, bper_account, groceries.name, "-150", d)
    _add(s, bper_account, groceries.name, "20", d)  # rimborso
    _add(s, bper_account, other.name, "-10", d)

    m = month_summary(s, d)
    assert [c.category.name for c in m.by_category] == [groceries.name, other.name]
    assert m.by_category[0].spent == Decimal(130)
    assert m.by_category[0].over_budget
    assert not m.by_category[1].over_budget


def test_no_income_means_no_savings_rate(s, bper_account):
    _add(s, bper_account, "Uscita extra fuori budget", "-10", dt.date(2026, 9, 1))
    assert month_summary(s, dt.date(2026, 9, 1)).savings_rate is None


def test_available_months_and_trend(s, bper_account):
    _add(s, bper_account, "Stipendio/Pensione", "100", dt.date(2026, 7, 3))
    _add(s, bper_account, "Stipendio/Pensione", "100", dt.date(2026, 9, 3))
    assert available_months(s) == [dt.date(2026, 9, 1), dt.date(2026, 7, 1)]

    trend = monthly_trend(s, months=3, until=dt.date(2026, 9, 30))
    assert [m.month for m in trend] == [dt.date(2026, m, 1) for m in (7, 8, 9)]
    assert [m.income for m in trend] == [Decimal(100), Decimal(0), Decimal(100)]
