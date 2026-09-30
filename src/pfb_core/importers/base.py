"""Formato normalizzato prodotto dai parser, indipendente dalla banca."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class ParsedTransaction:
    source_id: str
    date: dt.date
    amount: Decimal
    type: str
    description: str = ""
    counterparty: str | None = None
    iban: str | None = None
    mcc: str | None = None
    bank_category: str | None = None


@dataclass(frozen=True)
class ParsedTrade:
    source_id: str
    date: dt.date
    isin: str
    name: str
    shares: Decimal  # negativo per le vendite
    price: Decimal
    amount: Decimal
    fee: Decimal = Decimal(0)
    origin: str = "manuale"


@dataclass(frozen=True)
class ParsedBalance:
    """Saldo del conto riportato nell'export (es. intestazione BPER)."""

    as_of: dt.date
    value: Decimal


@dataclass
class ParseResult:
    transactions: list[ParsedTransaction] = field(default_factory=list)
    trades: list[ParsedTrade] = field(default_factory=list)
    balance: ParsedBalance | None = None
    iban: str | None = None  # IBAN del conto esportato, se presente nel file
    skipped: list[str] = field(default_factory=list)  # righe non gestite, per diagnostica
