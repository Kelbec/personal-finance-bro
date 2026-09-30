import datetime as dt
from decimal import Decimal

import pytest

from pfb_core.importers import parse_bper_xls, parse_tr_csv
from pfb_core.importers.bper import (
    clean_description,
    extract_counterparty,
    parse_data_it,
    parse_importo_it,
)
from pfb_core.importers.trade_republic import FormatError


# --- Trade Republic ---------------------------------------------------------
def test_tr_counts(tr_csv):
    r = parse_tr_csv(tr_csv)
    # 23 righe + 1 ritenuta sugli interessi
    assert len(r.transactions) == 24
    assert len(r.trades) == 4
    assert r.skipped == []


def test_tr_amounts_are_decimal(tr_csv):
    r = parse_tr_csv(tr_csv)
    first = r.transactions[0]
    assert first.amount == Decimal("-6")
    assert isinstance(first.amount, Decimal)
    assert first.mcc == "5814"


def test_tr_direct_debit_real_payee(tr_csv):
    debits = [t for t in parse_tr_csv(tr_csv).transactions
              if t.type == "TRANSFER_DIRECT_DEBIT_INBOUND"]  # fmt: skip
    payees = {t.counterparty for t in debits}
    assert payees == {"ASSICURAZIONE SPA", "Vita Sicura", "Satispay Europe S.A."}
    assert all(t.iban and not t.iban.startswith("IT00A000000000000000000000") for t in debits
               if t.counterparty != "ASSICURAZIONE SPA")  # fmt: skip


def test_tr_uses_local_date_not_utc(tr_csv):
    # datetime 2026-09-21T07:06 UTC, date 2026-09-20
    debit = next(t for t in parse_tr_csv(tr_csv).transactions if t.amount == Decimal("-35.59"))
    assert debit.date == dt.date(2026, 9, 20)


def test_tr_interest_tax_split(tr_csv):
    r = parse_tr_csv(tr_csv)
    tax = [t for t in r.transactions if t.type == "TAX"]
    assert len(tax) == 1
    assert tax[0].amount == Decimal("-10.35")
    assert tax[0].source_id.endswith(":tax")


def test_tr_trades(tr_csv):
    trade = parse_tr_csv(tr_csv).trades[0]
    assert trade.isin == "IE00BKM4GZ66"
    assert trade.shares == Decimal("10.5325240000")
    assert trade.origin == "piano"


def test_tr_missing_columns(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("date,amount\n2026-09-01,1\n", encoding="utf-8")
    with pytest.raises(FormatError):
        parse_tr_csv(bad)


# --- BPER --------------------------------------------------------------------
def test_parse_data_it():
    assert parse_data_it("28 settembre 2026") == dt.date(2026, 9, 28)
    assert parse_data_it("01 Febbraio 2026") == dt.date(2026, 2, 1)


@pytest.mark.parametrize(
    "value,expected",
    [(-937.14, Decimal("-937.14")), ("4.306,88", Decimal("4306.88")),
     ("86,01 €", Decimal("86.01")), (None, None), ("", None)],
)  # fmt: skip
def test_parse_importo_it(value, expected):
    assert parse_importo_it(value) == expected


def test_clean_description_removes_codice_fiscale():
    assert "RSSMRA80A01H501U" not in clean_description("F24 RSSMRA80A01H501U 2026")


def test_extract_counterparty():
    assert extract_counterparty("... a favore di VERDI GIULIO Num. Bon.Sepa 1") == "VERDI GIULIO"
    assert extract_counterparty("ADDEBITO SDD TELECOMITALIA SPA N: 1") == "TELECOMITALIA SPA"
    assert extract_counterparty("CANONE SERVIZIO") is None


def test_bper_parse(bper_xls):
    r = parse_bper_xls(bper_xls)
    assert len(r.transactions) == 7  # Totale e "Dati aggiornati" ignorati
    assert r.iban == "IT00A0000000000000000000005"
    assert r.balance.value == Decimal("86.01")
    assert r.balance.as_of == dt.date(2026, 9, 30)
    assert r.transactions[0].bank_category == "CANONE"
    assert r.transactions[1].amount == Decimal("-937.14")


def test_bper_duplicate_rows_get_distinct_ids(bper_xls):
    ids = [t.source_id for t in parse_bper_xls(bper_xls).transactions]
    assert len(ids) == len(set(ids))


def test_bper_ids_stable_across_parses(bper_xls):
    a = [t.source_id for t in parse_bper_xls(bper_xls).transactions]
    b = [t.source_id for t in parse_bper_xls(bper_xls).transactions]
    assert a == b
