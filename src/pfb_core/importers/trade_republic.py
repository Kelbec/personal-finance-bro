"""Parser dell'export CSV di Trade Republic ("Esportazione operazioni").

Note sul formato:
- `date` è la data contabile locale; `datetime` è in UTC e può cadere il giorno dopo.
- `TRANSFER_DIRECT_DEBIT_INBOUND` è un addebito SEPA in uscita: la controparte riportata
  è il titolare stesso, il beneficiario reale è nella descrizione ("... transfer to X (IBAN)").
- Le righe `TRADING` generano sia un trade sia un movimento di cassa (📈 Investimento).
- Gli interessi riportano la ritenuta nella colonna `tax`: diventa un movimento separato.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pfb_core.importers.base import ParsedTrade, ParsedTransaction, ParseResult

REQUIRED_COLUMNS = {
    "date",
    "category",
    "type",
    "name",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "description",
    "transaction_id",
    "counterparty_name",
    "counterparty_iban",
    "mcc_code",
}

TRADE_TYPES = {"BUY", "SELL"}
_DEBIT_PAYEE = re.compile(r"transfer to (.+?) \((\w+)\)")


class FormatError(ValueError):
    pass


def _dec(value: str) -> Decimal | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise FormatError(f"importo non valido: {value!r}") from exc


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_tr_csv(path: str | Path) -> ParseResult:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise FormatError(f"colonne mancanti nel CSV Trade Republic: {sorted(missing)}")
        rows = list(reader)

    result = ParseResult()
    for row in rows:
        tx_id = row["transaction_id"].strip()
        day = dt.date.fromisoformat(row["date"])
        amount = _dec(row["amount"])
        if amount is None:
            result.skipped.append(f"{tx_id}: importo vuoto")
            continue
        type_ = row["type"].strip()
        description = row["description"].strip()
        counterparty = _clean(row["counterparty_name"]) or _clean(row["name"])
        iban = _clean(row["counterparty_iban"])

        if type_ == "TRANSFER_DIRECT_DEBIT_INBOUND":
            m = _DEBIT_PAYEE.search(description)
            if m:
                counterparty, iban = m.group(1).strip(), m.group(2)

        if row["category"] == "TRADING" and type_ in TRADE_TYPES:
            shares = _dec(row["shares"]) or Decimal(0)
            if type_ == "SELL" and shares > 0:
                shares = -shares
            result.trades.append(
                ParsedTrade(
                    source_id=tx_id,
                    date=day,
                    isin=row["symbol"].strip(),
                    name=row["name"].strip(),
                    shares=shares,
                    price=_dec(row["price"]) or Decimal(0),
                    amount=amount,
                    fee=_dec(row["fee"]) or Decimal(0),
                    origin="piano" if description.startswith("Savings plan") else "manuale",
                )
            )
            counterparty = row["name"].strip()

        result.transactions.append(
            ParsedTransaction(
                source_id=tx_id,
                date=day,
                amount=amount,
                type=type_,
                description=description,
                counterparty=counterparty,
                iban=iban,
                mcc=_clean(row["mcc_code"]),
            )
        )

        tax = _dec(row["tax"])
        if tax:
            result.transactions.append(
                ParsedTransaction(
                    source_id=f"{tx_id}:tax",
                    date=day,
                    amount=tax,
                    type="TAX",
                    description=f"Ritenuta su {type_.lower()}",
                    counterparty="Erario",
                )
            )
    return result
