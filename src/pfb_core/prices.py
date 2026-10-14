"""Prezzi degli strumenti: download da Yahoo Finance (opzionale) e cache nella tabella Price.

Il download è l'unica chiamata di rete dell'app e non invia dati personali: chiede solo
lo storico pubblico di un simbolo. Il provider è iniettabile, così i test non usano la rete.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session, select

from pfb_core.models import Instrument, Price, Trade

# (simbolo, da, a) -> [(data, chiusura)]
PriceProvider = Callable[[str, dt.date, dt.date], Iterable[tuple[dt.date, float | Decimal]]]


CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={p1}&period2={p2}&interval=1d"


def chart_provider(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, Decimal]]:
    """Storico dall'endpoint pubblico di Yahoo, solo libreria standard (usato sul telefono)."""
    import json
    import urllib.parse
    import urllib.request

    p1 = int(dt.datetime.combine(start, dt.time(), dt.UTC).timestamp())
    p2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), dt.UTC).timestamp())
    url = CHART_URL.format(symbol=urllib.parse.quote(symbol), p1=p1, p2=p2)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)["chart"]
    if data.get("error"):
        raise RuntimeError(data["error"].get("description") or str(data["error"]))
    res = data["result"][0]
    offset = dt.timedelta(seconds=res["meta"].get("gmtoffset", 0))
    closes = res["indicators"]["quote"][0].get("close") or []
    return [
        ((dt.datetime.fromtimestamp(ts, dt.UTC) + offset).date(), Decimal(str(round(c, 6))))
        for ts, c in zip(res.get("timestamp") or [], closes, strict=False)
        if c is not None
    ]


def yahoo_provider(symbol: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, Decimal]]:
    try:
        import yfinance as yf
    except ImportError:  # extra opzionale assente (es. nell'APK): basta la libreria standard
        return chart_provider(symbol, start, end)
    hist = yf.Ticker(symbol).history(
        start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(), auto_adjust=False
    )
    return [
        (ts.date(), Decimal(str(round(float(close), 6))))
        for ts, close in hist["Close"].items()
        if close == close  # scarta NaN
    ]


@dataclass
class FetchReport:
    saved: dict[str, int] = field(default_factory=dict)  # ticker -> prezzi nuovi
    missing_symbol: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _first_trade_date(s: Session, isin: str) -> dt.date | None:
    return s.exec(select(Trade.date).where(Trade.isin == isin).order_by(Trade.date)).first()


def fetch_prices(
    s: Session,
    *,
    provider: PriceProvider = yahoo_provider,
    start: dt.date | None = None,
    end: dt.date | None = None,
    isins: Iterable[str] | None = None,
) -> FetchReport:
    """Scarica i prezzi mancanti per gli strumenti con almeno un trade (o quelli indicati)."""
    end = end or dt.date.today()
    report = FetchReport()
    if isins is None:
        isins = sorted(set(s.exec(select(Trade.isin)).all()))
    for isin in isins:
        inst = s.get(Instrument, isin)
        if inst is None:
            continue
        if not inst.yahoo_symbol:
            report.missing_symbol.append(inst.ticker)
            continue
        last = s.exec(
            select(Price.date).where(Price.isin == isin).order_by(Price.date.desc())
        ).first()
        since = start or (last + dt.timedelta(days=1) if last else _first_trade_date(s, isin))
        if since is None or since > end:
            continue
        try:
            rows = list(provider(inst.yahoo_symbol, since, end))
        except Exception as exc:  # rete assente, simbolo errato, ...
            report.errors[inst.ticker] = str(exc)
            continue
        known = set(s.exec(select(Price.date).where(Price.isin == isin)).all())
        new = [Price(isin=isin, date=d, close=Decimal(str(c))) for d, c in rows if d not in known]
        s.add_all(new)
        report.saved[inst.ticker] = len(new)
    s.commit()
    return report


def set_price(s: Session, isin: str, day: dt.date, close: Decimal) -> Price:
    """Prezzo inserito a mano (es. strumento senza simbolo Yahoo)."""
    p = s.exec(select(Price).where(Price.isin == isin, Price.date == day)).first()
    if p is None:
        p = Price(isin=isin, date=day, close=close, source="manuale")
    p.close, p.source = close, "manuale"
    s.add(p)
    s.commit()
    return p


def price_on(s: Session, isin: str, day: dt.date) -> tuple[Decimal, dt.date] | None:
    """Ultima chiusura disponibile alla data; in mancanza, il prezzo dell'ultimo trade.

    Le posizioni di partenza sono escluse: il loro prezzo è il costo medio, non il mercato.
    """
    p = s.exec(
        select(Price).where(Price.isin == isin, Price.date <= day).order_by(Price.date.desc())
    ).first()
    t = s.exec(
        select(Trade)
        .where(Trade.isin == isin, Trade.date <= day, Trade.price > 0, Trade.origin != "iniziale")
        .order_by(Trade.date.desc())
    ).first()
    if p and (t is None or p.date >= t.date):
        return p.close, p.date
    if t:
        return t.price, t.date
    return None
