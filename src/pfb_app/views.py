"""Schermate Flet. Tutta la logica sta in pfb_core; qui solo interfaccia."""

from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import flet as ft
from sqlalchemy import Engine
from sqlmodel import select

from pfb_core.backup import InvalidDatabase, inspect, snapshot_bytes
from pfb_core.db import session
from pfb_core.importer import ImportFailed, import_file
from pfb_core.metrics import MonthSummary, available_months, month_summary, monthly_trend
from pfb_core.models import Account, AccountSource, Category, CategoryGroup, Transaction
from pfb_core.portfolio import (
    MonthWorth,
    PortfolioSummary,
    build_all_snapshots,
    net_worth_series,
    portfolio_summary,
    set_balance,
)
from pfb_core.prices import fetch_prices
from pfb_core.rules import mark_internal, pending_review, set_category
from pfb_core.sheets_sync import (
    GspreadClient,
    SyncPlan,
    add_manual_transaction,
    load_sheet_config,
    manual_accounts,
    plan_sync,
    run_sync,
)

EXTENSIONS = {AccountSource.CSV_TR: ["csv"], AccountSource.XLS_BPER: ["xls"]}


def _page(control: ft.Control) -> ft.Page | None:
    """La pagina del controllo, oppure None se non è ancora montato (es. nei test)."""
    try:
        return control.page
    except RuntimeError:
        return None


def _refresh(control: ft.Control) -> None:
    if _page(control) is not None:
        control.update()


def _toast(control: ft.Control, text: str, error: bool = False) -> None:
    page = _page(control)
    if page is None:
        return
    page.show_dialog(
        ft.SnackBar(ft.Text(text), bgcolor=ft.Colors.ERROR_CONTAINER if error else None)
    )


def _file_picker(page: ft.Page) -> ft.FilePicker:
    """Il FilePicker della pagina. È un servizio: va registrato prima dell'uso, altrimenti
    il client non lo conosce e la chiamata resta muta."""
    for service in page.services:
        if isinstance(service, ft.FilePicker):
            return service
    picker = ft.FilePicker()
    page.services.append(picker)
    page.update()
    return picker


async def _pick_one(control: ft.Control, title: str, extensions: list[str] | None) -> Path | None:
    """Fa scegliere un file e ne ritorna un percorso locale (su Android/web arrivano i byte)."""
    page = _page(control)
    if page is None:
        return None
    if page.platform.is_mobile():  # Android filtra per tipo MIME: .csv/.xls spesso risultano grigi
        extensions = None
    try:
        files = await _file_picker(page).pick_files(
            dialog_title=title,
            file_type=ft.FilePickerFileType.CUSTOM if extensions else ft.FilePickerFileType.ANY,
            allowed_extensions=extensions,
            with_data=True,  # necessario su web e Android, dove non c'è un percorso locale
        )
    except Exception as exc:  # noqa: BLE001  (l'errore va mostrato, non perso nel log)
        _toast(control, f"Selezione file non riuscita: {exc}", error=True)
        return None
    if not files:
        return None
    f = files[0]
    if f.path and Path(f.path).is_file():
        return Path(f.path)
    tmp = Path(tempfile.mkdtemp()) / f.name
    tmp.write_bytes(f.bytes or b"")
    return tmp


def _category_options(engine: Engine) -> list[ft.dropdown.Option]:
    """Categorie nell'ordine dei gruppi: entrate, spese, risparmio, extra."""
    with session(engine) as s:
        cats = s.exec(select(Category)).all()
    opts = []
    for group in CategoryGroup:
        opts += [ft.dropdown.Option(key=str(c.id), text=c.label) for c in cats
                 if c.group == group]  # fmt: skip
    return opts


class ImportView(ft.Column):
    """Scelta del conto, selezione del file, riepilogo dell'import."""

    def __init__(self, engine: Engine, on_imported: Callable[[], None] | None = None):
        super().__init__(spacing=16, expand=True)
        self.engine = engine
        self.on_imported = on_imported
        with session(engine) as s:
            accounts = s.exec(select(Account).where(Account.source != AccountSource.MANUALE)).all()
            self.accounts = {str(a.id): (a.name, a.source) for a in accounts}

        self.account = ft.Dropdown(
            label="Conto",
            options=[ft.dropdown.Option(key=k, text=v[0]) for k, v in self.accounts.items()],
            value=next(iter(self.accounts), None),
            width=320,
        )
        self.result = ft.Column(spacing=4)
        self.controls = [
            ft.Text("Importa movimenti", size=22, weight=ft.FontWeight.BOLD),
            ft.Text('CSV di Trade Republic o XLS "Lista Movimenti" di BPER.'),
            self.account,
            ft.FilledButton("Scegli file", icon=ft.Icons.UPLOAD_FILE, on_click=self._pick),
            self.result,
        ]
        if not self.accounts:
            self.controls.insert(
                2, ft.Text("Nessun conto configurato: crea config.toml e riavvia l'app.")
            )

    async def _pick(self, e: ft.Event[ft.FilledButton]) -> None:
        if not self.account.value:
            _toast(self, "Scegli prima il conto", error=True)
            return
        name, source = self.accounts[self.account.value]
        path = await _pick_one(self, f"Export {name}", EXTENSIONS[source])
        if path is not None:
            self._run_import(int(self.account.value), path)

    def _run_import(self, account_id: int, path: Path) -> None:
        try:
            with session(self.engine) as s:
                account = s.get(Account, account_id)
                r = import_file(s, account, path)
        except (ImportFailed, ValueError) as exc:
            _toast(self, f"Import non riuscito: {exc}", error=True)
            return
        lines = [
            f"{path.name}",
            f"Nuovi: {r.new}  ·  già presenti: {r.duplicates}  ·  trade: {r.trades_new}",
            f"Categorizzati in automatico: {r.categorized}   ·   giroconti: {r.internal}",
            f"Da rivedere: {r.to_review}   ·   copertura {r.coverage:.0%}",
        ]
        if r.balance_saved:
            lines.append("Saldo di fine mese aggiornato")
        self.result.controls = [ft.Text(line) for line in lines]
        _refresh(self)
        if self.on_imported:
            self.on_imported()


class ReviewView(ft.Column):
    """Coda dei movimenti senza categoria: scegli la categoria, l'app crea la regola."""

    def __init__(self, engine: Engine):
        super().__init__(spacing=8, expand=True)
        self.engine = engine
        self.remember = ft.Checkbox(label="Ricorda per questo esercente/controparte", value=True)
        self.header = ft.Text(size=22, weight=ft.FontWeight.BOLD)
        self.list = ft.ListView(expand=True, spacing=8)
        self.controls = [self.header, self.remember, self.list]
        self._options = self._category_options()
        self.refresh(update=False)

    def _category_options(self) -> list[ft.dropdown.Option]:
        return _category_options(self.engine)

    def refresh(self, update: bool = True) -> None:
        with session(self.engine) as s:
            pending = pending_review(s)
            accounts = {a.id: a.name for a in s.exec(select(Account)).all()}
            self.list.controls = [self._row(tx, accounts.get(tx.account_id, "")) for tx in pending]
        self.header.value = f"Da rivedere ({len(pending)})"
        if not pending:
            self.list.controls = [ft.Text("Tutto categorizzato 🎉")]
        if update:
            _refresh(self)

    def _row(self, tx: Transaction, account: str) -> ft.Control:
        who = tx.counterparty or tx.description[:60]
        color = ft.Colors.GREEN if tx.amount > 0 else None
        return ft.Card(
            content=ft.Container(
                padding=12,
                content=ft.Column(
                    spacing=6,
                    controls=[
                        ft.Row(
                            [
                                ft.Text(who, weight=ft.FontWeight.W_600, expand=True),
                                ft.Text(f"{tx.amount:+.2f} €", color=color),
                            ]
                        ),
                        ft.Text(f"{tx.date:%d/%m/%Y} · {account} · {tx.type}", size=12),
                        ft.Row(
                            [
                                ft.Dropdown(
                                    label="Categoria",
                                    options=self._options,
                                    enable_filter=True,
                                    editable=True,
                                    expand=True,
                                    data=tx.id,
                                    on_select=self._on_category,
                                ),
                                ft.TextButton("Giroconto", data=tx.id, on_click=self._on_internal),
                            ]
                        ),
                    ],
                ),
            )
        )

    def _on_category(self, e: ft.Event[ft.Dropdown]) -> None:
        with session(self.engine) as s:
            tx = s.get(Transaction, e.control.data)
            res = set_category(s, tx, int(e.control.value), remember=bool(self.remember.value))
        msg = "Categoria salvata"
        if res.also_categorized:
            msg += f", aggiornati altri {res.also_categorized} movimenti"
        _toast(self, msg)
        self.refresh()

    def _on_internal(self, e: ft.Event[ft.TextButton]) -> None:
        with session(self.engine) as s:
            mark_internal(s, s.get(Transaction, e.control.data))
        _toast(self, "Segnato come giroconto")
        self.refresh()


def _eur(value) -> str:
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _month_label(month: dt.date) -> str:
    return f"{month:%m/%Y}"


class DashboardView(ft.Column):
    """Cashflow del mese: entrate, uscite, savings rate, categorie vs budget, ultimi mesi."""

    def __init__(self, engine: Engine):
        super().__init__(spacing=12, expand=True, scroll=ft.ScrollMode.AUTO)
        self.engine = engine
        self.month_picker = ft.Dropdown(label="Mese", width=160, on_select=self._on_month)
        self.kpis = ft.Row(wrap=True, spacing=12)
        self.categories = ft.Column(spacing=8)
        self.trend = ft.Column(spacing=2)
        self.controls = [
            ft.Text("Cashflow", size=22, weight=ft.FontWeight.BOLD),
            self.month_picker,
            self.kpis,
            ft.Text("Spese per categoria", size=16, weight=ft.FontWeight.W_600),
            self.categories,
            ft.Text("Ultimi mesi", size=16, weight=ft.FontWeight.W_600),
            self.trend,
        ]
        self.refresh(update=False)

    def refresh(self, update: bool = True) -> None:
        with session(self.engine) as s:
            months = available_months(s)
            selected = (
                dt.date.fromisoformat(self.month_picker.value)
                if self.month_picker.value
                else (months[0] if months else dt.date.today().replace(day=1))
            )
            self.month_picker.options = [
                ft.dropdown.Option(key=m.isoformat(), text=_month_label(m)) for m in months
            ]
            self.month_picker.value = selected.isoformat() if months else None
            summary = month_summary(s, selected)
            trend = monthly_trend(s, months=6, until=selected)
        self._show(summary, trend)
        if update:
            _refresh(self)

    def _on_month(self, e: ft.Event[ft.Dropdown]) -> None:
        self.refresh()

    def _show(self, m: MonthSummary, trend: list[MonthSummary]) -> None:
        rate = f"{m.savings_rate:.0%}" if m.savings_rate is not None else "—"
        tiles = [
            ("Entrate", _eur(m.income)),
            ("Uscite", _eur(m.expenses)),
            ("Saldo", _eur(m.net)),
            ("Savings rate", rate),
            ("Risparmiato/investito", _eur(m.saved)),
        ]
        self.kpis.controls = [
            ft.Card(
                content=ft.Container(
                    padding=12,
                    content=ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text(label, size=12),
                            ft.Text(value, size=18, weight=ft.FontWeight.BOLD),
                        ],
                    ),
                )
            )
            for label, value in tiles
        ]
        if m.uncategorized:
            self.kpis.controls.append(
                ft.Text(f"{m.uncategorized} movimenti da rivedere non sono nei totali", size=12)
            )
        self.categories.controls = [self._category_row(c) for c in m.by_category] or [
            ft.Text("Nessuna spesa categorizzata in questo mese.")
        ]
        self.trend.controls = [
            ft.Column(  # due righe: sta anche sullo schermo del telefono
                spacing=0,
                controls=[
                    ft.Row(
                        [
                            ft.Text(_month_label(t.month), weight=ft.FontWeight.W_600, expand=True),
                            ft.Text(
                                f"risparmio {t.savings_rate:.0%}"
                                if t.savings_rate is not None
                                else "—"
                            ),
                        ]
                    ),  # fmt: skip
                    ft.Text(f"entrate {_eur(t.income)}  ·  uscite {_eur(t.expenses)}", size=12),
                ],
            )
            for t in trend
        ]

    @staticmethod
    def _category_row(c) -> ft.Control:
        label = f"{c.category.label}  {_eur(c.spent)}"
        if c.budget is None:
            return ft.Text(label)
        ratio = float(c.spent / c.budget) if c.budget else 0.0
        return ft.Column(
            spacing=2,
            controls=[
                ft.Text(f"{label} / {_eur(c.budget)}"),
                ft.ProgressBar(
                    value=min(ratio, 1.0),
                    color=ft.Colors.ERROR if c.over_budget else None,
                ),
            ],
        )


ASSET_LABELS = {"stock": "Azioni", "bond": "Obbligazioni", "commodity": "Materie prime",
                "cash": "Liquidità"}  # fmt: skip


def _tile(label: str, value: str) -> ft.Control:
    return ft.Card(
        content=ft.Container(
            padding=12,
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Text(label, size=12),
                    ft.Text(value, size=18, weight=ft.FontWeight.BOLD),
                ],  # fmt: skip
            ),
        )
    )


class PortfolioView(ft.Column):
    """Patrimonio: valore, P&L, XIRR, allocazione, posizioni, storico, saldi manuali."""

    def __init__(self, engine: Engine):
        super().__init__(spacing=12, expand=True, scroll=ft.ScrollMode.AUTO)
        self.engine = engine
        self.kpis = ft.Row(wrap=True, spacing=12)
        self.allocation = ft.Column(spacing=6)
        self.positions = ft.Column(spacing=4)
        self.history = ft.Column(spacing=2)
        self.warnings = ft.Column(spacing=2)
        with session(engine) as s:
            accounts = s.exec(select(Account)).all()
        self.balance_account = ft.Dropdown(
            label="Conto",
            width=200,
            options=[ft.dropdown.Option(key=str(a.id), text=a.name) for a in accounts],
        )
        self.balance_date = ft.TextField(label="Fine mese (AAAA-MM-GG)", width=190,
                                         value=f"{dt.date.today():%Y-%m-%d}")  # fmt: skip
        self.balance_value = ft.TextField(label="Saldo €", width=120)
        self.controls = [
            ft.Row(
                [
                    ft.Text("Patrimonio", size=22, weight=ft.FontWeight.BOLD, expand=True),
                    ft.FilledButton(
                        "Aggiorna prezzi", icon=ft.Icons.REFRESH, on_click=self._on_update
                    ),  # fmt: skip
                ]
            ),
            self.kpis,
            self.warnings,
            ft.Text("Allocazione", size=16, weight=ft.FontWeight.W_600),
            self.allocation,
            ft.Text("Posizioni", size=16, weight=ft.FontWeight.W_600),
            self.positions,
            ft.Text("Andamento mensile", size=16, weight=ft.FontWeight.W_600),
            self.history,
            ft.Text(
                "Saldo manuale (PayPal, Satispay, saldo di partenza)",
                size=16,
                weight=ft.FontWeight.W_600,
            ),  # fmt: skip
            ft.Row(
                [
                    self.balance_account,
                    self.balance_date,
                    self.balance_value,
                    ft.OutlinedButton("Salva", on_click=self._on_balance),
                ],  # fmt: skip
                wrap=True,
            ),
        ]
        self.refresh(update=False)

    def refresh(self, update: bool = True) -> None:
        with session(self.engine) as s:
            p = portfolio_summary(s)
            series = net_worth_series(s)[-12:]
        self._show(p, series)
        if update:
            _refresh(self)

    def _show(self, p: PortfolioSummary, series: list[MonthWorth]) -> None:
        if p.xirr is not None and p.annualized:
            rate_label, rate = "Rendimento annuo (XIRR)", f"{p.xirr:.1%}"
        elif p.period_return is not None:
            rate_label = f"Rendimento dal {p.xirr_since:%m/%Y}"
            rate = f"{p.period_return:+.1%}"
        else:
            rate_label, rate = "Rendimento", "—"
        self.kpis.controls = [
            _tile("Patrimonio netto", _eur(p.net_worth)),
            _tile("Titoli", _eur(p.securities)),
            _tile("Liquidità", _eur(p.total_cash)),
            _tile("Costo di carico", _eur(p.invested)),
            _tile("Guadagno/perdita", _eur(p.pnl)),
            _tile(rate_label, rate),
        ]
        self.allocation.controls = [
            ft.Column(
                spacing=2,
                controls=[
                    ft.Text(f"{ASSET_LABELS.get(k, k)}  {v:.1%}"),
                    ft.ProgressBar(value=float(v)),
                ],  # fmt: skip
            )
            for k, v in sorted(p.allocation.items(), key=lambda kv: -kv[1])
        ] or [ft.Text("Nessun dato: inserisci le posizioni di partenza e un saldo.")]
        rows = []
        for pos in p.positions:
            pnl = f"{pos.pnl_pct:+.1%}" if pos.pnl_pct is not None else ""
            color = None
            if pos.pnl is not None:
                color = ft.Colors.GREEN if pos.pnl >= 0 else ft.Colors.ERROR
            value = _eur(pos.value) if pos.value is not None else "senza prezzo"
            rows.append(
                ft.Column(  # due righe: sta anche sullo schermo del telefono
                    spacing=0,
                    controls=[
                        ft.Row(
                            [
                                ft.Text(
                                    pos.instrument.ticker, weight=ft.FontWeight.W_600, expand=True
                                ),
                                ft.Text(value),
                                ft.Text(pnl, color=color, width=60, text_align=ft.TextAlign.RIGHT),
                            ]
                        ),
                        ft.Text(f"{pos.shares:.4f} q.  ·  medio {_eur(pos.avg_price)}", size=12),
                    ],
                )  # fmt: skip
            )
        for name, value in p.cash.items():
            rows.append(ft.Row([ft.Text(name, expand=True), ft.Text(_eur(value))]))
        self.positions.controls = rows
        self.history.controls = [
            ft.Row(
                [
                    ft.Text(f"{m.month:%m/%Y}", width=70),
                    ft.Text(_eur(m.total), expand=True),
                    ft.Text(f"liq. {_eur(m.by_class.get('cash', 0))}", size=12),
                ]
            )  # fmt: skip
            for m in reversed(series)
        ]

    async def _on_update(self, e) -> None:
        _toast(self, "Scarico i prezzi...")
        r, warnings = await asyncio.to_thread(self._update_blocking)  # rete: fuori dalla UI
        msgs = [f"Manca il simbolo Yahoo per {t}" for t in r.missing_symbol]
        msgs += [f"{t}: {err[:80]}" for t, err in r.errors.items()]
        msgs += warnings[-5:]
        self.warnings.controls = [ft.Text(m, size=12, color=ft.Colors.ERROR) for m in msgs]
        _toast(self, f"Prezzi nuovi: {sum(r.saved.values())}")
        self.refresh()

    def _update_blocking(self):
        with session(self.engine) as s:
            return fetch_prices(s), build_all_snapshots(s)

    def _on_balance(self, e) -> None:
        try:
            day = dt.date.fromisoformat(self.balance_date.value or "")
            value = Decimal((self.balance_value.value or "").replace(",", "."))
        except (ValueError, ArithmeticError):
            _toast(self, "Data o importo non validi", error=True)
            return
        if not self.balance_account.value:
            _toast(self, "Scegli un conto", error=True)
            return
        with session(self.engine) as s:
            set_balance(s, s.get(Account, int(self.balance_account.value)), day, value)
            build_all_snapshots(s)
        _toast(self, "Saldo salvato")
        self.refresh()


class AddView(ft.Column):
    """Spesa inserita a mano e sincronizzazione verso il foglio Google condiviso."""

    def __init__(
        self, engine: Engine, config_path: Path, on_added: Callable[[], None] | None = None
    ):
        super().__init__(spacing=12, expand=True, scroll=ft.ScrollMode.AUTO)
        self.engine = engine
        self.config_path = config_path
        self.on_added = on_added
        with session(engine) as s:
            accounts = manual_accounts(s)
        self.account = ft.Dropdown(
            label="Conto",
            options=[ft.dropdown.Option(key=str(a.id), text=a.name) for a in accounts],
            value=str(accounts[0].id) if accounts else None,
            width=320,
        )
        self.date = ft.TextField(
            label="Data (AAAA-MM-GG)", value=dt.date.today().isoformat(), width=200
        )
        self.amount = ft.TextField(
            label="Importo €", width=150, keyboard_type=ft.KeyboardType.NUMBER
        )
        self.category = ft.Dropdown(
            label="Categoria",
            options=_category_options(engine),
            enable_filter=True,
            editable=True,
            width=320,
        )
        self.description = ft.TextField(label="Descrizione", width=320)
        self.sync_status = ft.Text(size=12)
        self.sync_button = ft.FilledButton(
            "Sincronizza Google Sheet", icon=ft.Icons.SYNC, on_click=self._on_sync
        )
        self.controls = [
            ft.Text("Aggiungi movimento", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("Contanti, PayPal, Satispay: le spese bancarie arrivano dall'import.", size=12),
            self.account,
            ft.Row([self.date, self.amount], wrap=True),
            self.category,
            self.description,
            ft.FilledButton("Salva", icon=ft.Icons.ADD, on_click=self._on_save),
            ft.Divider(),
            ft.Text("Foglio Google condiviso", size=16, weight=ft.FontWeight.W_600),
            self.sync_button,
            self.sync_status,
        ]
        if not accounts:
            self.controls.insert(
                2,
                ft.Text(
                    'Nessun conto manuale: aggiungine uno in config.toml (source = "manuale").'
                ),
            )
        self._plan: SyncPlan | None = None

    def _on_save(self, e) -> None:
        try:
            day = dt.date.fromisoformat((self.date.value or "").strip())
            amount = Decimal((self.amount.value or "").strip().replace(",", "."))
        except (ValueError, ArithmeticError):
            _toast(self, "Data o importo non validi", error=True)
            return
        if not self.account.value or not self.category.value:
            _toast(self, "Scegli conto e categoria", error=True)
            return
        try:
            with session(self.engine) as s:
                add_manual_transaction(
                    s,
                    s.get(Account, int(self.account.value)),
                    day,
                    amount,
                    s.get(Category, int(self.category.value)),
                    self.description.value or "",
                )
        except ValueError as exc:
            _toast(self, str(exc), error=True)
            return
        self.amount.value = ""
        self.description.value = ""
        _toast(self, "Movimento salvato")
        _refresh(self)
        if self.on_added:
            self.on_added()

    def _on_sync(self, e) -> None:
        cfg = load_sheet_config(self.config_path)
        if cfg is None:
            _toast(
                self,
                "Sincronizzazione non configurata: aggiungi [google_sheet] a config.toml",
                error=True,
            )
            return
        with session(self.engine) as s:
            self._plan = plan_sync(s, cfg)
        plan = self._plan
        skipped = "".join(
            f"\n{owner or '?'}: {n} movimenti senza scheda configurata"
            for owner, n in plan.no_tab.items()
        )
        if not plan.rows:
            self.sync_status.value = "Niente da inviare: il foglio è aggiornato." + skipped
            _refresh(self)
            return
        tabs = ", ".join(f"{tab} ({len(rows)})" for tab, rows in plan.by_tab().items())
        text = (
            f"{len(plan.rows)} movimenti dal {plan.first:%d/%m/%Y} al {plan.last:%d/%m/%Y}\n"
            f"Schede: {tabs}\n"
            "Quelli già presenti nel foglio (stessa data, importo e categoria) vengono saltati."
            + skipped
        )
        page = _page(self)
        if page is None:
            return
        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Inviare al foglio Google?"),
                content=ft.Text(text),
                actions=[
                    ft.TextButton("Annulla", on_click=lambda _: page.pop_dialog()),
                    ft.FilledButton("Invia", on_click=self._on_confirm),
                ],
            )
        )

    async def _on_confirm(self, e) -> None:
        page = _page(self)
        if page is not None:
            page.pop_dialog()
        await self.sync_now()

    async def sync_now(self, client=None) -> None:
        """Esegue la sincronizzazione; la rete gira in un thread per non bloccare la UI."""
        if self._plan is None:
            return
        cfg = load_sheet_config(self.config_path)
        self.sync_button.disabled = True
        self.sync_status.value = "Invio in corso..."
        _refresh(self)
        try:
            result = await asyncio.to_thread(self._sync_blocking, cfg, client)
        except Exception as exc:  # noqa: BLE001  (rete, permessi, scheda inesistente: va mostrato)
            self.sync_status.value = f"Sincronizzazione non riuscita: {exc}"
            _toast(self, "Sincronizzazione non riuscita", error=True)
        else:
            written = sum(result.written.values())
            self.sync_status.value = (
                f"Scritti {written} movimenti; {result.already_there} erano già nel foglio."
            )
            _toast(self, "Foglio Google aggiornato")
        finally:
            self.sync_button.disabled = False
            self._plan = None
            _refresh(self)

    def _sync_blocking(self, cfg, client):
        client = client or GspreadClient(cfg)
        with session(self.engine) as s:
            return run_sync(s, plan_sync(s, cfg), client)


class DataView(ft.Column):
    """Database dell'app: copia da e verso il PC (il telefono tiene un suo SQLite)."""

    def __init__(self, db_path: Path, on_replace: Callable[[Path], None] | None = None):
        super().__init__(spacing=12, expand=True, scroll=ft.ScrollMode.AUTO)
        self.db_path = Path(db_path)
        self.on_replace = on_replace
        self.info = ft.Text()
        self.controls = [
            ft.Text("Dati", size=22, weight=ft.FontWeight.BOLD),
            self.info,
            ft.Text("Dal PC al telefono", size=16, weight=ft.FontWeight.W_600),
            ft.Text(
                "Copia data/pfb.sqlite dal PC al telefono (cavo, Drive, mail a te stesso) e "
                "sceglilo qui: sostituisce il database del telefono, di cui resta una copia.",
                size=12,
            ),
            ft.FilledButton("Importa database", icon=ft.Icons.DOWNLOAD, on_click=self._on_import),
            ft.Text("Dal telefono al PC", size=16, weight=ft.FontWeight.W_600),
            ft.Text(
                "Dopo un import fatto qui, salva il database e mettilo al posto di "
                "data/pfb.sqlite sul PC (con l'app del PC chiusa).",
                size=12,
            ),
            ft.OutlinedButton("Esporta database", icon=ft.Icons.UPLOAD, on_click=self._on_export),
        ]
        self.refresh(update=False)

    def refresh(self, update: bool = True) -> None:
        try:
            i = inspect(self.db_path)
            last = f"{i.last_date:%d/%m/%Y}" if i.last_date else "—"
            self.info.value = (f"{i.transactions} movimenti su {i.accounts} conti, "
                               f"ultimo del {last}\n{self.db_path}")  # fmt: skip
        except InvalidDatabase as exc:
            self.info.value = str(exc)
        if update:
            _refresh(self)

    async def _on_import(self, e) -> None:
        path = await _pick_one(self, "Database di Personal Finance Bro", ["sqlite", "db"])
        if path is None:
            return
        try:
            new = inspect(path)
        except InvalidDatabase as exc:
            _toast(self, str(exc), error=True)
            return
        page = _page(self)
        if page is None:
            return
        last = f"{new.last_date:%d/%m/%Y}" if new.last_date else "—"

        def confirm(_) -> None:
            page.pop_dialog()
            if self.on_replace:
                self.on_replace(path)

        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("Sostituire il database?"),
                content=ft.Text(
                    f"Il file scelto ha {new.transactions} movimenti (ultimo del {last}).\n"
                    "Quello attuale viene salvato come copia accanto al database."
                ),
                actions=[
                    ft.TextButton("Annulla", on_click=lambda _: page.pop_dialog()),
                    ft.FilledButton("Sostituisci", on_click=confirm),
                ],
            )
        )

    async def _on_export(self, e) -> None:
        page = _page(self)
        if page is None:
            return
        try:
            saved = await _file_picker(page).save_file(
                dialog_title="Salva il database",
                file_name=f"pfb-{dt.date.today():%Y-%m-%d}.sqlite",
                src_bytes=snapshot_bytes(self.db_path),
            )
        except Exception as exc:  # noqa: BLE001
            _toast(self, f"Esportazione non riuscita: {exc}", error=True)
            return
        if saved:
            _toast(self, "Database salvato")
