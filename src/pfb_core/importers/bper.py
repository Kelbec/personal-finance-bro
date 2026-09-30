"""Parser dell'export BPER "Lista Movimenti" (.xls, Excel 97).

Struttura del file:
- intestazione con luogo e data ("Modena,30 settembre 2026"), IBAN e saldi;
- tabella con header "Data operazione | Data valuta | Descrizione | Entrate | Uscite |
  Note | Categoria | Stato", date in italiano ("28 settembre 2026");
- riga finale "Totale".
Non c'è un ID univoco per movimento: si usa un hash di data, importo e descrizione,
più un contatore per righe identiche nello stesso file.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import xlrd

from pfb_core.importers.base import ParsedBalance, ParsedTransaction, ParseResult

_NOMI_MESI = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]  # fmt: skip
MESI = {m: i for i, m in enumerate(_NOMI_MESI, start=1)}

HEADER = "Data operazione"
_CODICE_FISCALE = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b")
_SPACES = re.compile(r"\s{2,}")
_COUNTERPARTY = [
    re.compile(r"a favore di (.+?)(?:\s+(?:Num\.|EUR\b)|$)"),
    re.compile(r"^(?:ADDEBITO SDD|COMMISSIONI)\s+(.+?)(?:\s{2,}|\s+N:|$)"),
]


class FormatError(ValueError):
    pass


def parse_data_it(text: str) -> dt.date:
    """'28 settembre 2026' → date(2026, 9, 28)."""
    try:
        g, m, a = text.strip().split()
        return dt.date(int(a), MESI[m.lower()], int(g))
    except (ValueError, KeyError) as exc:
        raise FormatError(f"data non riconosciuta: {text!r}") from exc


def parse_importo_it(value: object) -> Decimal | None:
    """Accetta float di Excel o testo italiano ('4.306,88', '86,01 €')."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(round(float(value), 2)))
    text = str(value).replace("€", "").replace(".", "").replace(",", ".").strip()
    return Decimal(text) if text else None


def clean_description(text: str) -> str:
    text = _CODICE_FISCALE.sub("", text)
    return _SPACES.sub(" ", text).strip()


def extract_counterparty(description: str) -> str | None:
    for pattern in _COUNTERPARTY:
        m = pattern.search(description)
        if m:
            return m.group(1).strip()
    return None


def _row_hash(*parts: object) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def parse_bper_xls(path: str | Path) -> ParseResult:
    sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
    rows = [
        [str(c.value).strip() if c.ctype == 1 else c.value for c in sheet.row(r)]
        for r in range(sheet.nrows)
    ]

    header_idx = next((i for i, r in enumerate(rows) if HEADER in r), None)
    if header_idx is None:
        raise FormatError(f"intestazione '{HEADER}' non trovata: non è un export BPER?")
    cols = {name: j for j, name in enumerate(rows[header_idx]) if name}

    result = ParseResult()
    as_of: dt.date | None = None
    closing: Decimal | None = None
    for r in rows[:header_idx]:
        for j, cell in enumerate(r):
            if isinstance(cell, str) and cell == "IBAN" and j + 1 < len(r):
                result.iban = str(r[j + 1]).strip() or None
            if isinstance(cell, str) and cell == "Saldo finale" and j + 1 < len(r):
                closing = parse_importo_it(r[j + 1])
            if isinstance(cell, str) and "," in cell and cell.split(",", 1)[1][:1].isdigit():
                with contextlib.suppress(FormatError):
                    as_of = parse_data_it(cell.split(",", 1)[1])
    if as_of is not None and closing is not None:
        result.balance = ParsedBalance(as_of=as_of, value=closing)

    seen: Counter[str] = Counter()
    for r in rows[header_idx + 1 :]:
        raw_date = r[cols["Data operazione"]]
        description = r[cols["Descrizione"]]
        if not raw_date or not isinstance(raw_date, str):
            continue
        try:
            day = parse_data_it(raw_date)
        except FormatError:  # righe "Totale", "Dati aggiornati al ..." e simili
            continue
        if r[cols["Stato"]] != "Contabilizzato":
            result.skipped.append(f"{raw_date}: stato {r[cols['Stato']]!r}")
            continue
        amount = parse_importo_it(r[cols["Entrate"]])
        if amount is None:
            amount = parse_importo_it(r[cols["Uscite"]])
        if amount is None:
            result.skipped.append(f"{raw_date}: importo vuoto")
            continue
        descr = clean_description(str(description))
        base = _row_hash(day, amount, descr)
        seen[base] += 1
        bank_cat = str(r[cols["Categoria"]]).strip() or None
        result.transactions.append(
            ParsedTransaction(
                source_id=f"{base}-{seen[base]}",
                date=day,
                amount=amount,
                type=bank_cat or "ALTRO",
                description=descr,
                counterparty=extract_counterparty(descr),
                bank_category=bank_cat,
            )
        )
    return result
