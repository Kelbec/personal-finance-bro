"""Storico dai fogli Google (esportati in .xlsx): movimenti di cashflow e patrimonio mensile.

- Cashflow: fogli con colonne Causale | Categoria | Importo | Data | Mese | Intestatario.
  Gli importi sono positivi: il segno viene dal gruppo della categoria. I movimenti vanno
  su un conto "storico" per intestatario, escluso dal calcolo della liquidità.
  Lo stesso movimento può comparire in più fogli (es. il foglio totale e quello personale):
  si prende l'unione, contando i movimenti identici al massimo quante volte compaiono
  in un singolo foglio.
- Patrimonio: un foglio per anno ("2024", "2025", ...) con i mesi in colonna e le sezioni
  CASH / BONDS / STOCKS in riga. Ogni valore diventa uno snapshot manuale di fine mese:
  i saldi dei conti fanno da ancora per la liquidità.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import openpyxl
from sqlmodel import Session, select

from pfb_core.models import (
    Account,
    AccountKind,
    AccountSource,
    AssetClass,
    Category,
    CategoryGroup,
    Instrument,
    Owner,
    Snapshot,
    Transaction,
)
from pfb_core.portfolio import CENT, add_initial_position, month_end
from pfb_core.prices import price_on

CASHFLOW_HEADER = ["causale", "categoria", "importo", "data"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
          "settembre", "ottobre", "novembre", "dicembre"]  # fmt: skip
SECTIONS = {"CASH": None, "BONDS": AssetClass.BOND, "STOCKS": AssetClass.STOCK,
            "COMMODITIES": AssetClass.COMMODITY}  # fmt: skip
STOP_LABELS = {"AGGREGATED", "% STOCKS/BONDS"}
SHARES_Q = Decimal("0.00000001")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def category_key(label: str) -> str:
    """Nome della categoria senza emoji: '🎮 Hobbie/Gadget' → 'hobbie/gadget'."""
    label = label.strip()
    parts = label.split(" ", 1)
    name = parts[1] if len(parts) == 2 and not parts[0].isalnum() else label
    return name.strip().lower()


# --- cashflow ----------------------------------------------------------------
@dataclass
class CashflowReport:
    new: int = 0
    duplicates: int = 0
    by_owner: Counter = field(default_factory=Counter)
    unknown_categories: Counter = field(default_factory=Counter)
    sheets: list[str] = field(default_factory=list)


def _cashflow_sheets(wb) -> list:
    out = []
    for ws in wb.worksheets:
        header = [str(c.value or "").strip().lower() for c in ws[1]]
        if header[:4] == CASHFLOW_HEADER and "intestatario" in header:
            out.append(ws)
    return out


def _history_account(s: Session, owner_name: str) -> Account:
    owner = s.exec(select(Owner).where(Owner.name == owner_name)).first()
    if owner is None:
        owner = Owner(name=owner_name)
        s.add(owner)
        s.flush()
    name = f"Storico {owner_name}"
    account = s.exec(select(Account).where(Account.name == name)).first()
    if account is None:
        account = Account(owner_id=owner.id, name=name, kind=AccountKind.STORICO,
                          source=AccountSource.MANUALE)  # fmt: skip
        s.add(account)
        s.flush()
    return account


def category_sign(cat: Category) -> int:
    if cat.group == CategoryGroup.ENTRATE:
        return 1
    if cat.group == CategoryGroup.EXTRA and cat.name.lower().startswith("entrata"):
        return 1
    return -1


def import_cashflow(
    s: Session, path: str | Path, sheets: list[str] | None = None
) -> CashflowReport:
    """Importa i movimenti dei fogli cashflow; rieseguirlo non crea duplicati."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    selected = [wb[n] for n in sheets] if sheets else _cashflow_sheets(wb)
    report = CashflowReport(sheets=[ws.title for ws in selected])
    categories = {category_key(f"{c.emoji} {c.name}"): c for c in s.exec(select(Category)).all()}

    # chiave del movimento -> numero massimo di occorrenze in un singolo foglio
    occurrences: dict[tuple, int] = {}
    for ws in selected:
        header = [str(c.value or "").strip().lower() for c in ws[1]]
        col = {name: header.index(name) for name in CASHFLOW_HEADER + ["intestatario"]}
        counts: Counter = Counter()
        for r in ws.iter_rows(min_row=2, values_only=True):
            amount, day = r[col["importo"]], r[col["data"]]
            if not isinstance(amount, (int, float)) or not isinstance(day, dt.datetime):
                continue
            owner = str(r[col["intestatario"]] or "").strip() or "Sconosciuto"
            label = str(r[col["categoria"]] or "").strip()
            causale = str(r[col["causale"]] or "").strip()
            key = (owner, day.date(), Decimal(str(amount)).quantize(CENT), label, causale)
            counts[key] += 1
        for key, n in counts.items():
            occurrences[key] = max(occurrences.get(key, 0), n)

    existing: dict[int, set[str]] = defaultdict(set)
    for key, n in occurrences.items():
        owner, day, amount, label, causale = key
        cat = categories.get(category_key(label))
        if cat is None:
            report.unknown_categories[label] += n
            continue
        account = _history_account(s, owner)
        if account.id not in existing:
            existing[account.id] = set(
                s.exec(select(Transaction.source_id).where(Transaction.account_id == account.id))
            )
        digest = hashlib.sha1("|".join(map(str, key)).encode()).hexdigest()[:16]
        for i in range(n):
            source_id = f"storico:{digest}:{i}"
            if source_id in existing[account.id]:
                report.duplicates += 1
                continue
            s.add(Transaction(account_id=account.id, date=day, amount=category_sign(cat) * amount,
                              type="STORICO", description=causale, category_id=cat.id,
                              source_id=source_id))  # fmt: skip
            existing[account.id].add(source_id)
            report.new += 1
            report.by_owner[owner] += 1
    s.commit()
    return report


# --- patrimonio --------------------------------------------------------------
@dataclass
class AllocationReport:
    snapshots: int = 0
    months: list[dt.date] = field(default_factory=list)
    created_accounts: list[str] = field(default_factory=list)
    created_instruments: list[str] = field(default_factory=list)
    closed_accounts: list[str] = field(default_factory=list)  # azzerati: spariti dal foglio

    @property
    def last_month(self) -> dt.date | None:
        return max(self.months) if self.months else None


def _find_account(s: Session, label: str, report: AllocationReport) -> Account:
    wanted = _norm(label)
    accounts = s.exec(select(Account)).all()
    for a in accounts:
        if a.kind == AccountKind.STORICO:
            continue
        n = _norm(a.name)
        # "Conto BPER" → BPER; "Trade Repubblic" (refuso nel foglio) → Trade Republic
        if wanted in (n, f"conto{n}") or wanted.replace("repubblic", "republic") == n:
            return a
    owner = s.exec(select(Owner).order_by(Owner.id)).first()
    if owner is None:
        owner = Owner(name="Io")
        s.add(owner)
        s.flush()
    kind = AccountKind.PREPAGATA if "prepagata" in wanted else AccountKind.CONTO
    account = Account(owner_id=owner.id, name=label, kind=kind, source=AccountSource.MANUALE)
    s.add(account)
    s.flush()
    report.created_accounts.append(label)
    return account


def _find_instrument(
    s: Session, label: str, asset_class: AssetClass, report: AllocationReport
) -> Instrument:
    wanted = _norm(label)
    for inst in s.exec(select(Instrument)).all():
        if wanted in (_norm(inst.ticker), _norm(inst.isin), _norm(inst.name)):
            return inst
    # fondo senza ISIN noto (es. i fondi Arca del 2024): chiave interna dal nome
    key = "FONDO-" + re.sub(r"[^A-Z0-9]+", "-", label.upper()).strip("-")
    inst = s.get(Instrument, key)
    if inst is None:
        inst = Instrument(isin=key, ticker=label, name=label, asset_class=asset_class)
        s.add(inst)
        s.flush()
        report.created_instruments.append(label)
    return inst


def _manual_snapshot(s: Session, month: dt.date, account_id: int, isin: str | None,
                     value: Decimal) -> None:  # fmt: skip
    snap = s.exec(
        select(Snapshot).where(
            Snapshot.month == month,
            Snapshot.account_id == account_id,
            Snapshot.isin == isin if isin else Snapshot.isin == None,  # noqa: E711
        )
    ).first() or Snapshot(month=month, account_id=account_id, isin=isin, value=value)
    snap.value, snap.is_manual = value, True
    s.add(snap)


def import_asset_allocation(s: Session, path: str | Path, broker: Account) -> AllocationReport:
    """Snapshot manuali di fine mese dai fogli anno; i titoli vanno sul conto `broker`."""
    wb = openpyxl.load_workbook(path, data_only=True)
    report = AllocationReport()
    seen_cash: dict[int, Account] = {}
    months: set[dt.date] = set()
    for ws in sorted((w for w in wb.worksheets if re.fullmatch(r"\d{4}", w.title)),
                     key=lambda w: w.title):  # fmt: skip
        year = int(ws.title)
        header = [str(c.value or "").strip().lower() for c in ws[1]]
        month_cols = {j: MONTHS.index(h) + 1 for j, h in enumerate(header) if h in MONTHS}
        section = None
        in_sheet: set[int] = set()
        sheet_months: set[dt.date] = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            label = str(row[0] or "").strip()
            if not label:
                continue
            if label.upper() in STOP_LABELS:
                break
            if label.upper() in SECTIONS:
                section = label.upper()
                continue
            if section is None or label.lower() == "total":
                continue
            if section == "CASH":
                account, isin = _find_account(s, label, report), None
                seen_cash[account.id] = account
                in_sheet.add(account.id)
            else:
                isin = _find_instrument(s, label, SECTIONS[section], report).isin
                account = broker
            for j, m in month_cols.items():
                v = row[j] if j < len(row) else None
                if not isinstance(v, (int, float)) or (isin and v == 0):
                    continue
                month = month_end(dt.date(year, m, 1))
                _manual_snapshot(s, month, account.id, isin, Decimal(str(v)).quantize(CENT))
                report.snapshots += 1
                sheet_months.add(month)
        # un conto presente negli anni precedenti ma non in questo foglio è stato chiuso
        if sheet_months:
            first = min(sheet_months)
            for acc_id, acc in seen_cash.items():
                if acc_id not in in_sheet:
                    _manual_snapshot(s, first, acc_id, None, Decimal("0.00"))
                    report.closed_accounts.append(f"{acc.name} ({first:%m/%Y})")
            seen_cash = {k: v for k, v in seen_cash.items() if k in in_sheet}
        months |= sheet_months
    report.months = sorted(months)
    s.commit()
    return report


@dataclass
class InitialPositionsReport:
    created: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)  # ticker, quote, €
    missing_price: list[str] = field(default_factory=list)


def initial_positions_from_snapshots(
    s: Session, broker: Account, month: dt.date
) -> InitialPositionsReport:
    """Posizioni di partenza dai valori di fine mese: quote = valore ÷ chiusura del giorno.

    Il costo di carico è il prezzo di quel giorno: il P&L parte da lì. Servono i prezzi
    del mese (fetch_prices) già in cache.
    """
    month = month_end(month)
    report = InitialPositionsReport()
    snaps = s.exec(
        select(Snapshot).where(
            Snapshot.month == month,
            Snapshot.account_id == broker.id,
            Snapshot.isin != None,  # noqa: E711
            Snapshot.is_manual == True,  # noqa: E712
        )
    ).all()
    for snap in snaps:
        inst = s.get(Instrument, snap.isin)
        quote = price_on(s, snap.isin, month)
        if quote is None:
            report.missing_price.append(inst.ticker)
            continue
        price = quote[0]
        shares = (snap.value / price).quantize(SHARES_Q)
        add_initial_position(s, broker, snap.isin, shares, price, month)
        report.created.append((inst.ticker, shares, snap.value))
    return report


def instruments_at(s: Session, broker: Account, month: dt.date) -> list[str]:
    """ISIN con un valore manuale nel mese: quelli per cui servono i prezzi di partenza."""
    return list(
        s.exec(
            select(Snapshot.isin).where(
                Snapshot.month == month_end(month),
                Snapshot.account_id == broker.id,
                Snapshot.isin != None,  # noqa: E711
            )
        )
    )
