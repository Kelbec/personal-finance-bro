"""Sincronizzazione verso il foglio Google condiviso (solo app → foglio).

È l'unico punto in cui i movimenti escono dal computer, ed è opzionale: parte solo su
richiesta e solo se `config.toml` ha una sezione `[google_sheet]`. L'accesso usa un
account di servizio; la chiave JSON resta in locale, fuori da git.

Ogni movimento categorizzato (giroconti e storico esclusi) dalla data `dal` in poi viene
aggiunto in fondo alla scheda del suo intestatario, nello stesso formato delle righe
scritte a mano: Causale | Categoria | Importo | Data | Mese | Intestatario.
L'importo è positivo per spese ed entrate; un rimborso su una categoria di spesa è
negativo. Un movimento già presente nella scheda (stessa data, importo e categoria) non
viene riscritto: così non si duplicano le spese inserite a mano anche nel foglio.
"""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlmodel import Session, select

from pfb_core.history import category_key, category_sign
from pfb_core.models import (
    Account,
    AccountKind,
    AccountSource,
    Category,
    Owner,
    SheetSync,
    Transaction,
)

CENT = Decimal("0.01")
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


@dataclass
class SheetConfig:
    spreadsheet: str  # URL o ID del foglio
    credentials: Path  # chiave JSON dell'account di servizio
    since: dt.date  # movimenti da questa data in poi
    tabs: dict[str, str]  # intestatario -> scheda


def load_sheet_config(config_path: str | Path) -> SheetConfig | None:
    """Sezione [google_sheet] di config.toml; None se assente (sincronizzazione spenta)."""
    path = Path(config_path)
    if not path.exists():
        return None
    cfg = tomllib.loads(path.read_text(encoding="utf-8")).get("google_sheet")
    if not cfg:
        return None
    credentials = Path(cfg.get("credenziali", "google-credentials.json"))
    if not credentials.is_absolute():
        credentials = path.parent / credentials
    return SheetConfig(
        spreadsheet=cfg["foglio"],
        credentials=credentials,
        since=dt.date.fromisoformat(str(cfg["dal"])),
        tabs=dict(cfg.get("schede", {})),
    )


class SheetClient(Protocol):
    def rows(self, tab: str) -> list[list]: ...

    def append(self, tab: str, rows: list[list]) -> None: ...


class GspreadClient:
    """Client reale (gspread). Dipendenza opzionale: pip install gspread."""

    def __init__(self, cfg: SheetConfig):
        try:
            import gspread
        except ImportError as exc:
            raise RuntimeError("gspread non installato: uv sync --extra sheets") from exc
        if not cfg.credentials.exists():
            raise RuntimeError(f"chiave dell'account di servizio non trovata: {cfg.credentials}")
        from gspread.utils import ValueInputOption, ValueRenderOption

        self._render, self._input = ValueRenderOption.unformatted, ValueInputOption.user_entered
        # solo i fogli di calcolo, non tutto Google Drive
        client = gspread.service_account(filename=str(cfg.credentials), scopes=[SCOPE])
        key = cfg.spreadsheet
        self._book = client.open_by_url(key) if key.startswith("http") else client.open_by_key(key)

    def rows(self, tab: str) -> list[list]:
        # valori grezzi: date come numero seriale, importi come numero
        return self._book.worksheet(tab).get_all_values(value_render_option=self._render)

    def append(self, tab: str, rows: list[list]) -> None:
        # USER_ENTERED: "2026-09-12" diventa una data vera, come se fosse scritta a mano
        self._book.worksheet(tab).append_rows(rows, value_input_option=self._input,
                                              table_range="A1")  # fmt: skip


# --- cosa inviare ------------------------------------------------------------
@dataclass
class PendingRow:
    tx: Transaction
    tab: str
    values: list  # Causale, Categoria, Importo, Data, Mese, Intestatario

    @property
    def key(self) -> tuple:
        return _row_key(self.values[3], self.values[2], self.values[1])


@dataclass
class SyncPlan:
    rows: list[PendingRow] = field(default_factory=list)
    no_tab: Counter = field(default_factory=Counter)  # intestatario senza scheda -> movimenti

    @property
    def first(self) -> dt.date | None:
        return min((r.tx.date for r in self.rows), default=None)

    @property
    def last(self) -> dt.date | None:
        return max((r.tx.date for r in self.rows), default=None)

    def by_tab(self) -> dict[str, list[PendingRow]]:
        out: dict[str, list[PendingRow]] = {}
        for r in self.rows:
            out.setdefault(r.tab, []).append(r)
        return out


def _row_key(day, amount, category: str) -> tuple:
    """Chiave di confronto con le righe del foglio: data, importo al centesimo, categoria."""
    if isinstance(day, (int, float)):  # data seriale di Sheets (giorni dal 30/12/1899)
        day = dt.date(1899, 12, 30) + dt.timedelta(days=int(day))
    elif isinstance(day, str):
        day = _parse_date(day)
    elif isinstance(day, dt.datetime):
        day = day.date()
    try:
        amount = Decimal(str(amount).replace(",", ".")).quantize(CENT)
    except ArithmeticError:
        amount = None
    return day, amount, category_key(str(category))


def _parse_date(text: str) -> dt.date | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _causale(tx: Transaction) -> str:
    text = tx.counterparty or tx.description or tx.type
    return re.sub(r"\s+", " ", text).strip()[:80]


def plan_sync(s: Session, cfg: SheetConfig) -> SyncPlan:
    """Movimenti da inviare: categorizzati, non giroconti, non storici, non ancora inviati."""
    plan = SyncPlan()
    done = set(s.exec(select(SheetSync.transaction_id)).all())
    accounts = {a.id: a for a in s.exec(select(Account)).all()}
    owners = {o.id: o.name for o in s.exec(select(Owner)).all()}
    categories = {c.id: c for c in s.exec(select(Category)).all()}
    txs = s.exec(
        select(Transaction)
        .where(
            Transaction.date >= cfg.since,
            Transaction.is_internal == False,  # noqa: E712
            Transaction.category_id != None,  # noqa: E711
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()
    for tx in txs:
        account = accounts[tx.account_id]
        if tx.id in done or account.kind == AccountKind.STORICO:
            continue
        owner = owners.get(account.owner_id, "")
        tab = cfg.tabs.get(owner)
        if tab is None:
            plan.no_tab[owner] += 1
            continue
        cat = categories[tx.category_id]
        amount = (tx.amount * category_sign(cat)).quantize(CENT)  # positivo; negativo se rimborso
        values = [_causale(tx), f"{cat.emoji} {cat.name}", float(amount),
                  tx.date.isoformat(), tx.date.month, owner]  # fmt: skip
        plan.rows.append(PendingRow(tx, tab, values))
    return plan


@dataclass
class SyncResult:
    written: Counter = field(default_factory=Counter)  # scheda -> righe scritte
    already_there: int = 0


def run_sync(s: Session, plan: SyncPlan, client: SheetClient) -> SyncResult:
    """Scrive le righe del piano; registra ogni movimento inviato (o trovato già nel foglio)."""
    result = SyncResult()
    for tab, rows in plan.by_tab().items():
        existing = Counter(_row_key(r[3], r[2], r[1]) for r in _data_rows(client.rows(tab)))
        to_write = []
        for row in rows:
            if existing[row.key] > 0:  # già nel foglio: non riscrivere
                existing[row.key] -= 1
                s.add(SheetSync(transaction_id=row.tx.id, tab=tab, skipped=True))
                result.already_there += 1
            else:
                to_write.append(row)
        if to_write:
            client.append(tab, [r.values for r in to_write])  # se fallisce non segna nulla
            for row in to_write:
                s.add(SheetSync(transaction_id=row.tx.id, tab=tab))
            result.written[tab] += len(to_write)
        s.commit()
    return result


def _data_rows(rows: Sequence[Sequence]) -> list[Sequence]:
    """Righe con almeno data, importo e categoria; salta l'intestazione."""
    return [
        r for r in rows[1:] if len(r) >= 4 and r[1] not in ("", None) and r[2] not in ("", None)
    ]


# --- inserimento manuale -----------------------------------------------------
def add_manual_transaction(
    s: Session,
    account: Account,
    day: dt.date,
    amount: Decimal,
    category: Category,
    description: str,
) -> Transaction:
    """Spesa o entrata inserita a mano (contanti, PayPal, Satispay...).

    `amount` è positivo: il segno viene dalla categoria, come nel foglio.
    """
    if amount <= 0:
        raise ValueError("l'importo deve essere positivo")
    n = len(s.exec(select(Transaction.id).where(Transaction.account_id == account.id)).all())
    tx = Transaction(
        account_id=account.id,
        date=day,
        amount=amount.quantize(CENT) * category_sign(category),
        type="MANUALE",
        description=description.strip(),
        category_id=category.id,
        source_id=f"manuale:{dt.datetime.now(dt.UTC).isoformat()}:{n}",
    )
    s.add(tx)
    s.commit()
    s.refresh(tx)
    return tx


def manual_accounts(s: Session) -> list[Account]:
    """Conti senza export (contanti, PayPal, ...): le spese bancarie arrivano dall'import."""
    return [a for a in s.exec(select(Account)).all()
            if a.source == AccountSource.MANUALE and a.kind != AccountKind.STORICO]  # fmt: skip
