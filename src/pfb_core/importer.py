"""Import di un file bancario nel database: parsing, dedup, trade, regole, saldo."""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from pfb_core.importers import ParseResult, parse_bper_xls, parse_tr_csv
from pfb_core.models import (
    Account,
    AccountSource,
    AssetClass,
    ImportBatch,
    Instrument,
    Snapshot,
    Trade,
    Transaction,
)
from pfb_core.rules import categorize, load_rules

PARSERS = {
    AccountSource.CSV_TR: parse_tr_csv,
    AccountSource.XLS_BPER: parse_bper_xls,
}


class ImportFailed(ValueError):
    pass


@dataclass
class ImportReport:
    batch: ImportBatch
    new: int
    duplicates: int
    trades_new: int
    categorized: int
    internal: int
    to_review: int
    balance_saved: bool
    skipped: list[str]

    @property
    def coverage(self) -> float:
        """Quota dei nuovi movimenti risolti in automatico (categoria o giroconto)."""
        return (self.categorized + self.internal) / self.new if self.new else 1.0


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_instrument(s: Session, isin: str, name: str) -> None:
    if s.get(Instrument, isin) is None:
        s.add(Instrument(isin=isin, ticker=isin, name=name, asset_class=AssetClass.STOCK))
        s.flush()


def _save_balance(s: Session, account: Account, parsed: ParseResult) -> bool:
    if parsed.balance is None:
        return False
    month = month_end(parsed.balance.as_of)
    snap = s.exec(
        select(Snapshot).where(
            Snapshot.month == month,
            Snapshot.account_id == account.id,
            Snapshot.isin == None,  # noqa: E711
        )
    ).first()
    if snap is None:
        snap = Snapshot(month=month, account_id=account.id, value=parsed.balance.value)
    snap.value = parsed.balance.value
    snap.is_manual = False
    s.add(snap)
    return True


def import_file(s: Session, account: Account, path: str | Path) -> ImportReport:
    path = Path(path)
    parser = PARSERS.get(account.source)
    if parser is None:
        raise ImportFailed(f"il conto {account.name!r} non ha un import automatico")
    parsed = parser(path)

    file_iban = (parsed.iban or "").replace(" ", "")
    if file_iban and account.iban and file_iban != account.iban.replace(" ", ""):
        raise ImportFailed(f"il file è del conto {parsed.iban}, non di {account.name!r}")

    batch = ImportBatch(account_id=account.id, filename=path.name, file_hash=_file_hash(path))
    s.add(batch)
    s.flush()

    known_tx = set(
        s.exec(select(Transaction.source_id).where(Transaction.account_id == account.id)).all()
    )
    known_trades = set(s.exec(select(Trade.source_id).where(Trade.account_id == account.id)).all())

    rules = load_rules(s)
    new = dup = categorized = internal = 0
    for p in parsed.transactions:
        if p.source_id in known_tx:
            dup += 1
            continue
        known_tx.add(p.source_id)
        tx = Transaction(
            account_id=account.id,
            date=p.date,
            amount=p.amount,
            type=p.type,
            counterparty=p.counterparty,
            iban=p.iban,
            mcc=p.mcc,
            bank_category=p.bank_category,
            description=p.description,
            source_id=p.source_id,
            import_batch_id=batch.id,
        )
        if categorize(tx, rules):
            if tx.is_internal:
                internal += 1
            else:
                categorized += 1
        s.add(tx)
        new += 1

    trades_new = 0
    for t in parsed.trades:
        if t.source_id in known_trades:
            continue
        _ensure_instrument(s, t.isin, t.name)
        s.add(
            Trade(
                account_id=account.id,
                isin=t.isin,
                date=t.date,
                shares=t.shares,
                price=t.price,
                amount=t.amount,
                fee=t.fee,
                origin=t.origin,
                source_id=t.source_id,
            )
        )
        trades_new += 1

    balance_saved = _save_balance(s, account, parsed)
    batch.rows_new, batch.rows_duplicate = new, dup
    s.add(batch)
    s.commit()
    s.refresh(batch)
    return ImportReport(
        batch=batch,
        new=new,
        duplicates=dup,
        trades_new=trades_new,
        categorized=categorized,
        internal=internal,
        to_review=new - categorized - internal,
        balance_saved=balance_saved,
        skipped=parsed.skipped,
    )
