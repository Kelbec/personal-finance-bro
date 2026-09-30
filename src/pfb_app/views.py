"""Schermate Flet. Tutta la logica sta in pfb_core; qui solo interfaccia."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import flet as ft
from sqlalchemy import Engine
from sqlmodel import select

from pfb_core.db import session
from pfb_core.importer import ImportFailed, import_file
from pfb_core.models import Account, AccountSource, Category, CategoryGroup, Transaction
from pfb_core.rules import mark_internal, pending_review, set_category

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
            return
        name, source = self.accounts[self.account.value]
        files = await ft.FilePicker().pick_files(
            dialog_title=f"Export {name}",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=EXTENSIONS[source],
            with_data=True,  # necessario su web e Android, dove non c'è un percorso locale
        )
        if not files:
            return
        f = files[0]
        if f.path:
            path = Path(f.path)
        else:
            tmp = Path(tempfile.mkdtemp()) / f.name
            tmp.write_bytes(f.bytes or b"")
            path = tmp
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
        with session(self.engine) as s:
            cats = s.exec(select(Category)).all()
        opts = []
        for group in CategoryGroup:
            opts += [ft.dropdown.Option(key=str(c.id), text=c.label) for c in cats
                     if c.group == group]  # fmt: skip
        return opts

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
