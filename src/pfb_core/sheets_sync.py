"""Sincronizzazione verso il foglio Google condiviso (solo app → foglio).

È l'unico punto in cui i movimenti escono dal computer, ed è opzionale: parte solo su
richiesta e solo se `config.toml` ha una sezione `[google_sheet]`. L'accesso usa un
account di servizio; la chiave JSON resta in locale, fuori da git.

Ogni movimento categorizzato (giroconti e storico esclusi) dalla data `dal` in poi viene
aggiunto in fondo alla scheda del suo intestatario. Il formato si adatta alla scheda:
si cerca l'intestazione nelle prime righe (Causale, Categoria, Importo, Data e, se ci sono,
Informazioni cronologiche, Mese, Anno, Intestatario) e l'importo si scrive come testo se
la scheda lo tiene come testo (es. le risposte di un Google Form). Così l'app scrive come
un nuovo invio del modulo, e le schede che leggono le risposte con le formule si aggiornano.
Le schede generate da formule vengono rifiutate: scriverci romperebbe le formule.

L'importo è positivo per spese ed entrate; un rimborso su una categoria di spesa è
negativo. Un movimento già presente nella scheda (stessa data, importo e categoria) non
viene riscritto: così non si duplicano le spese inserite anche dal modulo.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import tomllib
import types
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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
    def rows(self, tab: str, formulas: bool = False) -> list[list]: ...

    def append(
        self, tab: str, rows: list[list], first_row: int, datetime_col: int | None = None
    ) -> None: ...


def _ensure_wsgiref() -> None:
    """Sul telefono (Python di serious_python) manca `wsgiref`, che google_auth_oauthlib
    importa e di cui eredita due classi al momento dell'import. Serve solo al login
    interattivo, che qui non si usa (account di servizio): basta un guscio vuoto. Dove
    `wsgiref` c'è davvero (PC) non si tocca nulla."""
    try:
        import wsgiref.simple_server  # noqa: F401
        import wsgiref.util  # noqa: F401
    except ImportError:

        def unavailable(*args, **kwargs):
            raise RuntimeError("wsgiref non è disponibile su questo dispositivo")

        pkg = types.ModuleType("wsgiref")
        pkg.__path__ = []
        server = types.ModuleType("wsgiref.simple_server")
        server.WSGIServer = type("WSGIServer", (), {})
        server.WSGIRequestHandler = type("WSGIRequestHandler", (), {})
        server.make_server = unavailable
        util = types.ModuleType("wsgiref.util")
        util.request_uri = unavailable
        pkg.simple_server, pkg.util = server, util
        sys.modules.update({"wsgiref": pkg, "wsgiref.simple_server": server, "wsgiref.util": util})


class GspreadClient:
    """Client reale (gspread). Dipendenza opzionale: pip install gspread."""

    def __init__(self, cfg: SheetConfig):
        _ensure_wsgiref()
        try:
            import gspread
        except ImportError as exc:
            if exc.name == "gspread":
                raise RuntimeError("gspread non installato: uv sync --extra sheets") from exc
            # gspread c'è, ma una sua dipendenza (google-auth, cryptography...) non si carica
            raise RuntimeError(
                f"gspread installato ma non si carica: manca o non parte {exc.name or '?'} ({exc})"
            ) from exc
        if not cfg.credentials.exists():
            raise RuntimeError(f"chiave dell'account di servizio non trovata: {cfg.credentials}")
        from gspread.utils import ValueInputOption, ValueRenderOption

        self._render = ValueRenderOption
        self._input = ValueInputOption.user_entered
        # solo i fogli di calcolo, non tutto Google Drive
        client = gspread.service_account(filename=str(cfg.credentials), scopes=[SCOPE])
        key = cfg.spreadsheet
        self._book = client.open_by_url(key) if key.startswith("http") else client.open_by_key(key)

    def rows(self, tab: str, formulas: bool = False) -> list[list]:
        # valori grezzi (date come numero seriale), oppure le formule così come sono scritte
        option = self._render.formula if formulas else self._render.unformatted
        return self._book.worksheet(tab).get_all_values(value_render_option=option)

    def append(
        self, tab: str, rows: list[list], first_row: int, datetime_col: int | None = None
    ) -> None:
        # posizione esplicita: "append" di Sheets riempirebbe la prima riga vuota in mezzo
        ws = self._book.worksheet(tab)
        needed = first_row + len(rows) - 1
        if ws.row_count < needed:
            ws.add_rows(needed - ws.row_count)
        # USER_ENTERED: "2026-09-12" diventa una data vera, "'7.78" resta testo
        ws.update(values=rows, range_name=f"A{first_row}", value_input_option=self._input)
        if datetime_col is not None:  # il seriale va mostrato come data e ora, come nel modulo
            col = chr(65 + datetime_col)
            ws.format(f"{col}{first_row}:{col}{needed}",
                      {"numberFormat": {"type": "DATE_TIME"}})  # fmt: skip


# --- struttura della scheda --------------------------------------------------
class SheetLayoutError(ValueError):
    pass


FIELDS = {  # campo -> intestazioni accettate (minuscole, senza spazi né simboli)
    "timestamp": ("informazionicronologiche", "timestamp", "datainvio"),
    "causale": ("causale", "descrizione"),
    "categoria": ("categoria",),
    "importo": ("importo", "importoeuro"),
    "data": ("data",),
    "mese": ("mese",),
    "anno": ("anno",),
    "intestatario": ("intestatario",),
}
REQUIRED = ("causale", "categoria", "importo", "data")
WRITTEN_AS_IS = ("causale", "categoria", "importo", "data")  # una formula qui = scheda derivata


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


@dataclass
class Layout:
    header_row: int  # 1-based, come in Sheets
    columns: dict[str, int]  # campo -> indice di colonna (0-based)
    amount_as_text: bool = False  # importo tenuto come testo (es. risposte di un modulo)
    decimal_sep: str = "."

    @property
    def width(self) -> int:
        return max(self.columns.values()) + 1

    def data_rows(self, rows: Sequence[Sequence]) -> list[Sequence]:
        cat, amount = self.columns["categoria"], self.columns["importo"]
        return [r for r in rows[self.header_row:]
                if len(r) > max(cat, amount) and r[cat] not in ("", None)
                and r[amount] not in ("", None)]  # fmt: skip

    def next_row(self, rows: Sequence[Sequence]) -> int:
        """Riga (1-based) dopo l'ultimo movimento: le formule di Mese/Anno più sotto non contano."""
        cols = [self.columns[name] for name in WRITTEN_AS_IS]
        last = self.header_row
        for i, r in enumerate(rows[self.header_row :], start=self.header_row + 1):
            if any(j < len(r) and r[j] not in ("", None) for j in cols):
                last = i
        return last + 1

    def key(self, row: Sequence) -> tuple:
        c = self.columns
        return _row_key(row[c["data"]] if len(row) > c["data"] else None,
                        row[c["importo"]], row[c["categoria"]])  # fmt: skip


def detect_layout(rows: Sequence[Sequence]) -> Layout:
    """Trova la riga di intestazione (entro le prime 5) e la posizione dei campi."""
    for i, row in enumerate(rows[:5]):
        names = [_norm(v) for v in row]
        columns = {}
        for name, aliases in FIELDS.items():
            idx = next((j for j, n in enumerate(names) if n in aliases), None)
            if idx is not None:
                columns[name] = idx
        if all(f in columns for f in REQUIRED):
            layout = Layout(header_row=i + 1, columns=columns)
            amounts = [r[columns["importo"]] for r in layout.data_rows(rows)]
            texts = [a for a in amounts if isinstance(a, str)]
            if texts and len(texts) * 2 >= len(amounts):
                layout.amount_as_text = True
                layout.decimal_sep = "," if any("," in t for t in texts) else "."
            return layout
    raise SheetLayoutError(
        "intestazione non trovata: servono le colonne Causale, Categoria, Importo e Data"
    )


def check_not_derived(layout: Layout, formulas: Sequence[Sequence], tab: str) -> None:
    """Rifiuta le schede i cui movimenti sono calcolati da formule (es. da un modulo)."""
    for row in formulas[layout.header_row :]:
        for name in WRITTEN_AS_IS:
            j = layout.columns[name]
            if j < len(row) and str(row[j]).startswith("="):
                raise SheetLayoutError(
                    f"la scheda {tab!r} è calcolata da formule: scrivendoci si romperebbero. "
                    "Indica la scheda da cui legge (es. 'Risposte del modulo 1')."
                )


# --- cosa inviare ------------------------------------------------------------
@dataclass
class PendingRow:
    tx: Transaction
    tab: str
    causale: str
    categoria: str  # con emoji, come nel foglio
    amount: Decimal  # positivo; negativo se rimborso
    owner: str

    @property
    def key(self) -> tuple:
        return self.tx.date, self.amount, category_key(self.categoria)

    def values(self, layout: Layout, now: dt.datetime) -> list:
        if layout.amount_as_text:  # "'" = testo in Sheets, come lo salva il modulo
            amount = "'" + f"{self.amount:.2f}".replace(".", layout.decimal_sep)
        else:
            amount = float(self.amount)
        fields = {
            "timestamp": _serial(now),  # data e ora di Sheets, come quelle del modulo
            "causale": self.causale,
            "categoria": self.categoria,
            "importo": amount,
            "data": self.tx.date.isoformat(),
            "mese": self.tx.date.month,
            "anno": self.tx.date.year,
            "intestatario": self.owner,
        }
        out = [""] * layout.width
        for name, j in layout.columns.items():
            out[j] = fields[name]
        return out


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


def _serial(moment: dt.datetime) -> float:
    """Data e ora come numero seriale di Sheets (giorni dal 30/12/1899)."""
    return round((moment - dt.datetime(1899, 12, 30)).total_seconds() / 86400, 8)


def _row_key(day, amount, category) -> tuple:
    """Chiave di confronto con le righe del foglio: data, importo al centesimo, categoria."""
    if isinstance(day, (int, float)) and not isinstance(day, bool):  # seriale di Sheets
        day = dt.date(1899, 12, 30) + dt.timedelta(days=int(day))
    elif isinstance(day, dt.datetime):
        day = day.date()
    elif isinstance(day, str):
        day = _parse_date(day)
    try:
        amount = Decimal(str(amount).strip().lstrip("'").replace(",", ".")).quantize(CENT)
    except (ArithmeticError, InvalidOperation):
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


UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)


def _causale(tx: Transaction) -> str:
    """Testo leggibile: senza codici di transazione (es. 'Saveback cash reward <uuid> ...')."""
    text = tx.counterparty or tx.description or tx.type
    text = UUID.split(text)[0] if UUID.search(text) else text  # dopo il codice: solo riferimenti
    return re.sub(r"\s+", " ", text).strip(" :-")[:80] or tx.type


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
        plan.rows.append(PendingRow(
            tx=tx, tab=tab, causale=_causale(tx), categoria=f"{cat.emoji} {cat.name}",
            amount=(tx.amount * category_sign(cat)).quantize(CENT), owner=owner,
        ))  # fmt: skip
    return plan


@dataclass
class SyncResult:
    written: Counter = field(default_factory=Counter)  # scheda -> righe scritte (o da scrivere)
    already_there: int = 0
    layouts: dict[str, Layout] = field(default_factory=dict)
    preview: dict[str, list[list]] = field(default_factory=dict)  # solo in prova


def run_sync(
    s: Session,
    plan: SyncPlan,
    client: SheetClient,
    now: dt.datetime | None = None,
    write: bool = True,
) -> SyncResult:
    """Scrive le righe del piano; registra ogni movimento inviato (o trovato già nel foglio).

    Prima di scrivere controlla tutte le schede: se una non va bene non si scrive nulla.
    Con `write=False` legge il foglio e prepara le righe, ma non scrive e non registra nulla.
    """
    now = now or dt.datetime.now()
    result = SyncResult()
    layouts = {}
    for tab in plan.by_tab():
        rows = client.rows(tab)
        layout = detect_layout(rows)
        check_not_derived(layout, client.rows(tab, formulas=True), tab)
        layouts[tab] = (layout, rows)
        result.layouts[tab] = layout
    for tab, pending in plan.by_tab().items():
        layout, rows = layouts[tab]
        existing = Counter(layout.key(r) for r in layout.data_rows(rows))
        to_write = []
        for row in pending:
            if existing[row.key] > 0:  # già nel foglio: non riscrivere
                existing[row.key] -= 1
                if write:
                    s.add(SheetSync(transaction_id=row.tx.id, tab=tab, skipped=True))
                result.already_there += 1
            else:
                to_write.append(row)
        if not write:
            result.preview[tab] = [r.values(layout, now) for r in to_write]
            result.written[tab] += len(to_write)
            continue
        if to_write:
            values = [r.values(layout, now) for r in to_write]
            client.append(tab, values, layout.next_row(rows), layout.columns.get("timestamp"))
            for row in to_write:  # solo dopo una scrittura riuscita
                s.add(SheetSync(transaction_id=row.tx.id, tab=tab))
            result.written[tab] += len(to_write)
        s.commit()
    return result


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
