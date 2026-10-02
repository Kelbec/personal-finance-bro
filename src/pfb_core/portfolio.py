"""Patrimonio: posizioni, liquidità, snapshot di fine mese, rendimento (XIRR).

- Quote: somma dei trade fino alla data (le posizioni di partenza sono trade `iniziale`).
- Costo di carico: metodo del costo medio; le vendite riducono il costo in proporzione.
- Liquidità di un conto: ultimo saldo "ancora" (inserito a mano o letto dall'export BPER)
  più i movimenti successivi. Senza ancora il saldo non è calcolabile.
- XIRR: dal primo mese con snapshot, usando il valore di mercato iniziale come primo flusso;
  così non serve conoscere il prezzo d'acquisto storico di tutto il portafoglio.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session, select

from pfb_core.models import (
    Account,
    AssetClass,
    Instrument,
    Snapshot,
    Trade,
    Transaction,
)
from pfb_core.prices import price_on

ZERO = Decimal(0)
CENT = Decimal("0.01")
CASH = "cash"  # chiave dell'allocazione per la liquidità


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def months_between(first: dt.date, last: dt.date) -> list[dt.date]:
    out, cur = [], month_end(first)
    while cur <= month_end(last):
        out.append(cur)
        cur = month_end(cur + dt.timedelta(days=1))
    return out


# --- posizioni ---------------------------------------------------------------
@dataclass
class Position:
    instrument: Instrument
    shares: Decimal
    cost: Decimal  # costo di carico residuo, positivo
    price: Decimal | None
    price_date: dt.date | None

    @property
    def value(self) -> Decimal | None:
        return None if self.price is None else (self.shares * self.price).quantize(CENT)

    @property
    def pnl(self) -> Decimal | None:
        return None if self.value is None else self.value - self.cost.quantize(CENT)

    @property
    def pnl_pct(self) -> Decimal | None:
        return None if self.pnl is None or not self.cost else self.pnl / self.cost


def _trades_until(s: Session, day: dt.date) -> list[Trade]:
    return list(s.exec(select(Trade).where(Trade.date <= day).order_by(Trade.date, Trade.id)))


def positions(s: Session, day: dt.date | None = None) -> list[Position]:
    day = day or dt.date.today()
    shares: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cost: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in _trades_until(s, day):
        if t.shares >= 0:
            shares[t.isin] += t.shares
            cost[t.isin] += -t.amount if t.amount < 0 else t.shares * t.price
        else:
            held = shares[t.isin]
            if held > 0:
                cost[t.isin] -= cost[t.isin] * min(-t.shares / held, Decimal(1))
            shares[t.isin] += t.shares
    out = []
    for isin, qty in shares.items():
        if qty <= Decimal("0.00000001"):
            continue
        quote = price_on(s, isin, day)
        out.append(
            Position(
                instrument=s.get(Instrument, isin),
                shares=qty,
                cost=cost[isin],
                price=quote[0] if quote else None,
                price_date=quote[1] if quote else None,
            )
        )
    return sorted(out, key=lambda p: p.value or ZERO, reverse=True)


def add_initial_position(
    s: Session, account: Account, isin: str, shares: Decimal, avg_cost: Decimal, day: dt.date
) -> Trade:
    """Posizione di partenza (quote già possedute prima del primo export importato)."""
    if s.get(Instrument, isin) is None:
        raise ValueError(f"strumento sconosciuto: {isin} (aggiungilo in config.toml)")
    source_id = f"iniziale:{isin}:{day.isoformat()}"
    t = s.exec(
        select(Trade).where(Trade.account_id == account.id, Trade.source_id == source_id)
    ).first() or Trade(account_id=account.id, isin=isin, date=day, source_id=source_id,
                       shares=ZERO, price=ZERO, amount=ZERO)  # fmt: skip
    t.shares, t.price = shares, avg_cost
    t.amount = -(shares * avg_cost).quantize(CENT)
    t.origin = "iniziale"
    s.add(t)
    s.commit()
    return t


# --- liquidità ---------------------------------------------------------------
def set_balance(s: Session, account: Account, day: dt.date, value: Decimal) -> Snapshot:
    """Saldo di un conto a fine mese, inserito a mano: fa da ancora per i mesi successivi."""
    month = month_end(day)
    snap = s.exec(
        select(Snapshot).where(
            Snapshot.month == month,
            Snapshot.account_id == account.id,
            Snapshot.isin == None,  # noqa: E711
        )
    ).first() or Snapshot(month=month, account_id=account.id, value=value)
    snap.value, snap.is_manual = value, True
    s.add(snap)
    s.commit()
    return snap


def cash_balance(s: Session, account: Account, day: dt.date) -> Decimal | None:
    snaps = s.exec(
        select(Snapshot)
        .where(
            Snapshot.account_id == account.id,
            Snapshot.isin == None,  # noqa: E711
            Snapshot.month <= month_end(day),
        )
        .order_by(Snapshot.month.desc())
    ).all()
    anchor = next((x for x in snaps if x.is_manual), None)
    if anchor is None:
        return None
    if anchor.month >= day:
        return anchor.value
    moves = s.exec(
        select(Transaction.amount).where(
            Transaction.account_id == account.id,
            Transaction.date > anchor.month,
            Transaction.date <= day,
        )
    ).all()
    return (anchor.value + sum(moves, ZERO)).quantize(CENT)


# --- snapshot mensili --------------------------------------------------------
def _upsert(s: Session, month: dt.date, account_id: int | None, isin: str | None, **values):
    snap = s.exec(
        select(Snapshot).where(
            Snapshot.month == month,
            Snapshot.account_id == account_id,
            Snapshot.isin == isin if isin else Snapshot.isin == None,  # noqa: E711
        )
    ).first()
    if snap is not None and snap.is_manual:
        return snap  # un valore inserito a mano non viene sovrascritto
    snap = snap or Snapshot(month=month, account_id=account_id, isin=isin, value=ZERO)
    for k, v in values.items():
        setattr(snap, k, v)
    s.add(snap)
    return snap


def build_snapshots(s: Session, month: dt.date) -> list[str]:
    """Calcola gli snapshot di fine mese; restituisce gli avvisi (prezzi o saldi mancanti)."""
    month = month_end(month)
    warnings = []
    # i valori calcolati si rifanno da zero; quelli manuali/da export restano
    for old in s.exec(
        select(Snapshot).where(Snapshot.month == month, Snapshot.is_manual == False)  # noqa: E712
    ).all():
        s.delete(old)
    s.flush()
    by_account: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
    for t in _trades_until(s, month):
        by_account[(t.account_id, t.isin)] += t.shares
    for (account_id, isin), qty in by_account.items():
        if qty <= 0:
            continue
        quote = price_on(s, isin, month)
        if quote is None:
            warnings.append(f"{s.get(Instrument, isin).ticker}: nessun prezzo al {month}")
            continue
        _upsert(s, month, account_id, isin, shares=qty, price=quote[0],
                value=(qty * quote[0]).quantize(CENT))  # fmt: skip
        if (month - quote[1]).days > 7:
            warnings.append(f"{s.get(Instrument, isin).ticker}: prezzo vecchio ({quote[1]})")
    for account in s.exec(select(Account)).all():
        value = cash_balance(s, account, month)
        if value is None:
            warnings.append(f"{account.name}: manca un saldo di partenza")
            continue
        _upsert(s, month, account.id, None, value=value)
    s.commit()
    return warnings


def first_month(s: Session) -> dt.date | None:
    dates = [
        s.exec(select(Trade.date).order_by(Trade.date)).first(),
        s.exec(select(Snapshot.month).order_by(Snapshot.month)).first(),
    ]
    dates = [d for d in dates if d]
    return month_end(min(dates)) if dates else None


def build_all_snapshots(s: Session, until: dt.date | None = None) -> list[str]:
    start = first_month(s)
    if start is None:
        return ["nessun trade o saldo: niente da calcolare"]
    until = until or dt.date.today()
    warnings: list[str] = []
    for m in months_between(start, until):
        warnings += [f"{m:%m/%Y} {w}" for w in build_snapshots(s, min(m, until))]
    return warnings


# --- serie storica e riepilogo -----------------------------------------------
@dataclass
class MonthWorth:
    month: dt.date
    by_class: dict[str, Decimal] = field(default_factory=dict)  # stock, bond, commodity, cash

    @property
    def total(self) -> Decimal:
        return sum(self.by_class.values(), ZERO)


def net_worth_series(s: Session) -> list[MonthWorth]:
    classes = {i.isin: i.asset_class.value for i in s.exec(select(Instrument)).all()}
    months: dict[dt.date, MonthWorth] = {}
    for snap in s.exec(select(Snapshot).order_by(Snapshot.month)).all():
        mw = months.setdefault(snap.month, MonthWorth(snap.month))
        key = classes.get(snap.isin, AssetClass.STOCK.value) if snap.isin else CASH
        mw.by_class[key] = mw.by_class.get(key, ZERO) + snap.value
    return list(months.values())


def xirr(flows: list[tuple[dt.date, float]]) -> float | None:
    """Tasso annuo che azzera il valore attuale dei flussi (negativi = versamenti)."""
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** ((d - t0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


@dataclass
class PortfolioSummary:
    day: dt.date
    positions: list[Position]
    cash: dict[str, Decimal]  # conto -> saldo
    invested: Decimal  # costo di carico totale delle posizioni aperte
    xirr: float | None
    xirr_since: dt.date | None

    @property
    def period_return(self) -> float | None:
        """Rendimento cumulato dal `xirr_since`, non annualizzato.

        Su periodi brevi l'XIRR annualizzato amplifica le oscillazioni (un +3% in un mese
        diventa +40% annuo): la UI mostra questo valore finché non è passato un anno.
        """
        if self.xirr is None or self.xirr_since is None:
            return None
        return (1 + self.xirr) ** ((self.day - self.xirr_since).days / 365.0) - 1

    @property
    def annualized(self) -> bool:
        return self.xirr_since is not None and (self.day - self.xirr_since).days >= 365

    @property
    def securities(self) -> Decimal:
        return sum((p.value or ZERO for p in self.positions), ZERO)

    @property
    def total_cash(self) -> Decimal:
        return sum(self.cash.values(), ZERO)

    @property
    def net_worth(self) -> Decimal:
        return self.securities + self.total_cash

    @property
    def pnl(self) -> Decimal:
        return sum((p.pnl or ZERO for p in self.positions), ZERO)

    @property
    def allocation(self) -> dict[str, Decimal]:
        """Quota di ogni asset class sul patrimonio (liquidità inclusa)."""
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for p in self.positions:
            totals[p.instrument.asset_class.value] += p.value or ZERO
        totals[CASH] += self.total_cash
        nw = self.net_worth
        return {k: v / nw for k, v in totals.items() if v} if nw else {}


def portfolio_xirr(s: Session, day: dt.date) -> tuple[float | None, dt.date | None]:
    series = [m for m in net_worth_series(s) if m.month <= day]
    start = next((m for m in series if any(k != CASH for k in m.by_class)), None)
    if start is None:
        return None, None
    securities_start = sum((v for k, v in start.by_class.items() if k != CASH), ZERO)
    flows = [(start.month, -float(securities_start))]
    for t in s.exec(
        select(Trade).where(Trade.date > start.month, Trade.date <= day, Trade.origin != "iniziale")
    ).all():
        flows.append((t.date, float(t.amount)))  # acquisti negativi, vendite positive
    current = sum((p.value or ZERO for p in positions(s, day)), ZERO)
    flows.append((day, float(current)))
    return xirr(flows), start.month


def portfolio_summary(s: Session, day: dt.date | None = None) -> PortfolioSummary:
    day = day or dt.date.today()
    pos = positions(s, day)
    cash = {}
    for account in s.exec(select(Account)).all():
        value = cash_balance(s, account, day)
        if value is not None:
            cash[account.name] = value
    rate, since = portfolio_xirr(s, day)
    return PortfolioSummary(
        day=day,
        positions=pos,
        cash=cash,
        invested=sum((p.cost for p in pos), ZERO).quantize(CENT),
        xirr=rate,
        xirr_since=since,
    )
