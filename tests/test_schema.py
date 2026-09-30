from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from pfb_core.db import get_engine, init_db, session
from pfb_core.models import (
    Account,
    AccountKind,
    AccountSource,
    Category,
    CategoryGroup,
    Instrument,
    Owner,
    Transaction,
)
from pfb_core.seed import CATEGORIES, seed_all


@pytest.fixture
def s():
    engine = get_engine(":memory:")
    init_db(engine)
    with session(engine) as sess:
        yield sess


@pytest.fixture
def account(s):
    owner = Owner(name="Test")
    s.add(owner)
    s.flush()
    acc = Account(
        owner_id=owner.id, name="TR", kind=AccountKind.BROKER, source=AccountSource.CSV_TR
    )
    s.add(acc)
    s.commit()
    return acc


def test_seed_is_idempotent(s):
    first = seed_all(s)
    second = seed_all(s)
    assert first["categories"] == len(CATEGORIES)
    assert first["instruments"] == 7
    assert first["rules"] > 0
    assert second == {"categories": 0, "instruments": 0, "rules": 0}


def test_every_group_has_categories(s):
    seed_all(s)
    groups = {c.group for c in s.exec(select(Category)).all()}
    assert groups == set(CategoryGroup)


def test_category_label(s):
    seed_all(s)
    casa = s.exec(select(Category).where(Category.name == "Casa")).one()
    assert casa.label == "🏠 Casa"


def test_instruments_loaded(s):
    seed_all(s)
    vwce = s.get(Instrument, "IE00BK5BQT80")
    assert vwce.ticker == "VWCE"


def test_transaction_dedup_on_source_id(s, account):
    def tx():
        return Transaction(
            account_id=account.id,
            date=date(2026, 9, 1),
            amount=Decimal("-6.00"),
            type="CARD_TRANSACTION",
            source_id="01a05c9a-46ed-755e-be16-f1fefe940b9c",
        )

    s.add(tx())
    s.commit()
    s.add(tx())
    with pytest.raises(IntegrityError):
        s.commit()


def test_amount_keeps_decimal_precision(s, account):
    s.add(
        Transaction(
            account_id=account.id,
            date=date(2026, 9, 2),
            amount=Decimal("-634.40"),
            type="TRANSFER_INSTANT_OUTBOUND",
            source_id="x",
        )
    )
    s.commit()
    stored = s.exec(select(Transaction)).one()
    assert stored.amount == Decimal("-634.40")


def test_seed_accounts_from_config(s, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[owner]\nname = "Marco"\n'
        '[[accounts]]\nname = "BPER"\nkind = "conto"\nsource = "xls_bper"\niban = "IT00X0"\n',
        encoding="utf-8",
    )
    counts = seed_all(s, cfg)
    assert counts["accounts"] == 1
    bper = s.exec(select(Account).where(Account.name == "BPER")).one()
    assert bper.source == AccountSource.XLS_BPER
