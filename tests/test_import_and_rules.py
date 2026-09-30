import datetime as dt
from decimal import Decimal

import pytest
from sqlmodel import select

from pfb_core.importer import ImportFailed, import_file
from pfb_core.models import Category, Rule, RuleField, RuleOrigin, Snapshot, Trade, Transaction
from pfb_core.rules import matches, pending_review, set_category
from tests.conftest import write_bper_xls


def _cat(s, tx: Transaction) -> str | None:
    return s.get(Category, tx.category_id).name if tx.category_id else None


def _by_counterparty(s, name: str) -> Transaction:
    return s.exec(select(Transaction).where(Transaction.counterparty == name)).first()


def test_import_tr(s, tr_account, tr_csv):
    r = import_file(s, tr_account, tr_csv)
    assert r.new == 24
    assert r.trades_new == 4
    assert r.coverage >= 0.8
    assert len(s.exec(select(Trade)).all()) == 4


def test_reimport_is_idempotent(s, tr_account, tr_csv):
    import_file(s, tr_account, tr_csv)
    r = import_file(s, tr_account, tr_csv)
    assert r.new == 0
    assert r.duplicates == 24
    assert r.trades_new == 0


def test_tr_categories(s, tr_account, tr_csv):
    import_file(s, tr_account, tr_csv)
    assert _cat(s, _by_counterparty(s, "PIADINERIA DALLA PATRIZIA")) == "Ristorante/Asporto"
    assert _cat(s, _by_counterparty(s, "ACME S.R.L.")) == "Stipendio/Pensione"
    assert _cat(s, _by_counterparty(s, "Prime Video   Canale")) == "Abbonamenti"
    buy = s.exec(select(Transaction).where(Transaction.type == "BUY")).first()
    assert _cat(s, buy) == "Investimento"
    tax = s.exec(select(Transaction).where(Transaction.type == "TAX")).one()
    assert _cat(s, tax) == "Tassazione non lavorativa"


def test_transfer_to_own_account_is_internal(s, tr_account, tr_csv):
    import_file(s, tr_account, tr_csv)
    tx = _by_counterparty(s, "VERDI GIULIO")
    assert tx.is_internal
    assert tx.category_id is None


def test_import_bper(s, bper_account, bper_xls):
    r = import_file(s, bper_account, bper_xls)
    assert r.new == 7
    assert r.internal == 1
    assert r.balance_saved
    snap = s.exec(select(Snapshot)).one()
    assert snap.month == dt.date(2026, 9, 30)
    assert snap.value == Decimal("86.01")
    f24 = s.exec(select(Transaction).where(Transaction.bank_category == "F24")).one()
    assert _cat(s, f24) == "Costi attività lavorativa"
    assert "RSSMRA80A01H501U" not in f24.description


def test_bper_bank_category_beats_merchant(s, bper_account, bper_xls):
    """COMMISSIONI TELECOM → Costi bancari, ADDEBITO TELECOM → Casa."""
    import_file(s, bper_account, bper_xls)
    txs = s.exec(select(Transaction).where(Transaction.counterparty == "TELECOMITALIA SPA")).all()
    cats = sorted(_cat(s, t) for t in txs)
    assert cats == ["Casa", "Costi bancari", "Costi bancari"]


def test_import_rejects_wrong_account(s, bper_account, tmp_path):
    other = write_bper_xls(tmp_path / "altro.xls", iban="IT99Z9999999999999999999999")
    with pytest.raises(ImportFailed):
        import_file(s, bper_account, other)


def test_bper_balance_updates_on_new_export(s, bper_account, tmp_path):
    import_file(s, bper_account, write_bper_xls(tmp_path / "a.xls"))
    import_file(s, bper_account, write_bper_xls(tmp_path / "b.xls", closing="120,00 €"))
    snap = s.exec(select(Snapshot)).one()
    assert snap.value == Decimal("120.00")


def test_mcc_range_rule():
    rule = Rule(field=RuleField.MCC, pattern="3501-3999")
    assert matches(rule, Transaction(mcc="3600", account_id=1, date=dt.date.today(),
                                     amount=Decimal(0), type="x", source_id="x"))  # fmt: skip
    assert not matches(rule, Transaction(mcc="5812", account_id=1, date=dt.date.today(),
                                         amount=Decimal(0), type="x", source_id="y"))  # fmt: skip


def test_correction_creates_rule_and_applies_it(s, tr_account, tr_csv):
    import_file(s, tr_account, tr_csv)
    satispay = s.exec(
        select(Transaction).where(Transaction.counterparty == "Satispay Europe S.A.")
    ).all()
    assert all(t.category_id is None for t in satispay)
    before = len(pending_review(s))

    svago = s.exec(select(Category).where(Category.name == "Intrattenimento/Socialità")).one()
    res = set_category(s, satispay[0], svago.id)

    assert res.rule.origin == RuleOrigin.USER
    assert res.rule.field == RuleField.COUNTERPARTY
    assert len(pending_review(s)) == before - len(satispay)
    # la regola vale anche per gli import futuri
    assert matches(res.rule, satispay[0])


def test_correction_without_rule(s, tr_account, tr_csv):
    import_file(s, tr_account, tr_csv)
    tx = _by_counterparty(s, "Mario Rossi")
    gift = s.exec(select(Category).where(Category.name == "Regali/Beneficenza")).one()
    res = set_category(s, tx, gift.id, remember=False)
    assert res.rule is None
    assert _cat(s, tx) == "Regali/Beneficenza"
