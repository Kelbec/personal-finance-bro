"""Schema del database (SQLModel).

I movimenti (`Transaction`, `Trade`) sono la fonte di verità; gli `Snapshot`
mensili sono derivati, tranne quelli dei conti senza export (`is_manual=True`).
Gli importi sono `Decimal`: mai float per il denaro.
"""

import datetime as dt
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class CategoryGroup(StrEnum):
    ENTRATE = "Entrate"
    PRIMARIE = "Spese primarie"
    SECONDARIE = "Spese secondarie"
    RISPARMIO = "Risparmio e investimenti"
    EXTRA = "Extra"


class AccountKind(StrEnum):
    CONTO = "conto"
    PREPAGATA = "prepagata"
    WALLET = "wallet"
    BROKER = "broker"


class AccountSource(StrEnum):
    CSV_TR = "csv_tr"
    XLS_BPER = "xls_bper"
    MANUALE = "manuale"


class AssetClass(StrEnum):
    STOCK = "stock"
    BOND = "bond"
    COMMODITY = "commodity"


class RuleField(StrEnum):
    """Campo del movimento su cui la regola fa match."""

    TYPE = "type"  # tipo operazione (es. BUY, INTEREST_PAYMENT)
    COUNTERPARTY = "counterparty"  # nome controparte, regex case-insensitive
    IBAN = "iban"  # IBAN controparte, match esatto
    MCC = "mcc"  # codice esercente carta
    BANK_CATEGORY = "bank_category"  # categoria assegnata dalla banca (BPER)
    DESCRIPTION = "description"  # descrizione, regex case-insensitive


class Owner(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)


class Account(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="owner.id")
    name: str = Field(unique=True)
    kind: AccountKind
    source: AccountSource
    iban: str | None = None


class Category(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    emoji: str
    group: CategoryGroup
    monthly_budget: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


class ImportBatch(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    filename: str
    file_hash: str = Field(index=True)
    imported_at: dt.datetime = Field(default_factory=dt.datetime.now)
    rows_new: int = 0
    rows_duplicate: int = 0


class Transaction(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("account_id", "source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    date: dt.date = Field(index=True)
    amount: Decimal = Field(max_digits=14, decimal_places=4)
    type: str  # tipo operazione della banca (CARD_TRANSACTION, BONIFICO, ...)
    counterparty: str | None = None
    iban: str | None = None
    mcc: str | None = None
    bank_category: str | None = None
    description: str = ""
    category_id: int | None = Field(default=None, foreign_key="category.id")
    is_internal: bool = False  # giroconto tra conti propri: escluso dal cashflow
    source_id: str  # transaction_id di TR, oppure hash per BPER
    import_batch_id: int | None = Field(default=None, foreign_key="importbatch.id")


class Rule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    priority: int = 100  # più basso = valutata prima
    field: RuleField
    pattern: str
    category_id: int | None = Field(default=None, foreign_key="category.id")
    is_internal: bool = False
    enabled: bool = True


class Instrument(SQLModel, table=True):
    isin: str = Field(primary_key=True)
    ticker: str
    name: str
    asset_class: AssetClass
    yahoo_symbol: str | None = None


class Trade(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("account_id", "source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    isin: str = Field(foreign_key="instrument.isin", index=True)
    date: dt.date
    shares: Decimal = Field(max_digits=18, decimal_places=8)  # negativo per le vendite
    price: Decimal = Field(max_digits=14, decimal_places=6)
    amount: Decimal = Field(max_digits=14, decimal_places=4)
    fee: Decimal = Field(default=Decimal(0), max_digits=10, decimal_places=4)
    origin: str = "manuale"  # piano, manuale, saveback
    source_id: str


class Snapshot(SQLModel, table=True):
    """Valore di fine mese di un conto (liquidità) o di uno strumento."""

    __table_args__ = (UniqueConstraint("month", "account_id", "isin"),)

    id: int | None = Field(default=None, primary_key=True)
    month: dt.date = Field(index=True)  # ultimo giorno del mese
    account_id: int | None = Field(default=None, foreign_key="account.id")
    isin: str | None = Field(default=None, foreign_key="instrument.isin")
    shares: Decimal | None = Field(default=None, max_digits=18, decimal_places=8)
    price: Decimal | None = Field(default=None, max_digits=14, decimal_places=6)
    value: Decimal = Field(max_digits=14, decimal_places=2)
    is_manual: bool = False
