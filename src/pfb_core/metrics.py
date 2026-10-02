"""Metriche di cashflow: entrate, uscite, savings rate, spese per categoria e budget.

I giroconti (`is_internal`) sono sempre esclusi. I movimenti senza categoria non
entrano nei totali ma sono contati a parte (`uncategorized`), per non falsare le cifre.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func
from sqlmodel import Session, select

from pfb_core.models import Category, CategoryGroup, Transaction

ZERO = Decimal(0)
SPENDING_GROUPS = {CategoryGroup.PRIMARIE, CategoryGroup.SECONDARIE}


@dataclass
class CategoryTotal:
    category: Category
    spent: Decimal  # positivo = uscita netta (rimborsi già sottratti)
    budget: Decimal | None

    @property
    def over_budget(self) -> bool:
        return self.budget is not None and self.spent > self.budget


@dataclass
class MonthSummary:
    month: dt.date  # primo giorno del mese
    income: Decimal = ZERO
    expenses: Decimal = ZERO  # spese primarie + secondarie + extra in uscita
    saved: Decimal = ZERO  # uscite nette verso risparmio e investimenti
    uncategorized: int = 0
    by_category: list[CategoryTotal] = field(default_factory=list)

    @property
    def net(self) -> Decimal:
        return self.income - self.expenses

    @property
    def savings_rate(self) -> Decimal | None:
        """(entrate − spese) / entrate; None se nel mese non ci sono entrate."""
        return self.net / self.income if self.income > 0 else None


def month_bounds(month: dt.date) -> tuple[dt.date, dt.date]:
    """Primo giorno del mese e primo giorno del mese successivo."""
    start = month.replace(day=1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return start, end


def month_summary(s: Session, month: dt.date) -> MonthSummary:
    start, end = month_bounds(month)
    txs = s.exec(
        select(Transaction).where(
            Transaction.date >= start,
            Transaction.date < end,
            Transaction.is_internal == False,  # noqa: E712  (operatore SQL)
        )
    ).all()
    categories = {c.id: c for c in s.exec(select(Category)).all()}
    summary = MonthSummary(month=start)
    spent: dict[int, Decimal] = {}

    for tx in txs:
        cat = categories.get(tx.category_id)
        if cat is None:
            summary.uncategorized += 1
            continue
        if cat.group == CategoryGroup.ENTRATE:
            summary.income += tx.amount
        elif cat.group == CategoryGroup.RISPARMIO:
            summary.saved -= tx.amount
        elif cat.group in SPENDING_GROUPS:
            summary.expenses -= tx.amount
            spent[cat.id] = spent.get(cat.id, ZERO) - tx.amount
        elif tx.amount < 0:  # Extra: l'entrata extra non è reddito, l'uscita è una spesa
            summary.expenses -= tx.amount

    summary.by_category = sorted(
        (CategoryTotal(categories[cid], total, categories[cid].monthly_budget)
         for cid, total in spent.items() if total != ZERO),  # rimborso che azzera la spesa
        key=lambda c: c.spent,
        reverse=True,
    )  # fmt: skip
    return summary


def available_months(s: Session) -> list[dt.date]:
    """Mesi (primo giorno) con almeno un movimento, dal più recente."""
    month = func.strftime("%Y-%m-01", Transaction.date)
    rows = s.exec(select(month).distinct().order_by(month.desc())).all()
    return [dt.date.fromisoformat(r) for r in rows]


def monthly_trend(s: Session, months: int = 6, until: dt.date | None = None) -> list[MonthSummary]:
    """Riepiloghi degli ultimi `months` mesi fino a `until` incluso, in ordine cronologico."""
    cursor = (until or dt.date.today()).replace(day=1)
    result = []
    for _ in range(months):
        result.append(month_summary(s, cursor))
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)
    return result[::-1]
