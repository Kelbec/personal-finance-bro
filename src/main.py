"""Interfaccia Flet di Personal Finance Bro.

Avvio:
    uv run --extra app flet run src/main.py          # desktop
    uv run --extra app flet run --web src/main.py    # web (Raspberry Pi)
Build Android:
    uv run --extra app flet build apk

Variabili d'ambiente: PFB_DB (percorso del database), PFB_CONFIG (config.toml).
"""

from __future__ import annotations

import os
from pathlib import Path

import flet as ft

from pfb_app.views import AddView, DashboardView, DataView, ImportView, PortfolioView, ReviewView
from pfb_core.backup import replace_db
from pfb_core.db import get_engine, init_db, session
from pfb_core.seed import seed_all


def _base_dir() -> Path:
    """Cartella di data/pfb.sqlite e config.toml.

    Dai sorgenti è la radice del progetto: `flet run` sposta la cartella di lavoro in
    src/.flet/storage/data, e con percorsi relativi l'app aprirebbe un database vuoto.
    Nell'app installata (APK, senza pyproject.toml accanto) è la cartella dati dell'app,
    che resta tra un aggiornamento e l'altro.
    """
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").exists():
        return root
    return Path(os.environ.get("FLET_APP_STORAGE_DATA", "."))


DB_PATH = Path(os.environ.get("PFB_DB", _base_dir() / "data" / "pfb.sqlite"))
CONFIG_PATH = Path(os.environ.get("PFB_CONFIG", _base_dir() / "config.toml"))


def main(page: ft.Page) -> None:
    page.title = "Personal Finance Bro"
    state: dict = {}

    def build() -> None:
        engine = get_engine(DB_PATH)
        init_db(engine)
        with session(engine) as s:
            seed_all(s, CONFIG_PATH if CONFIG_PATH.exists() else None)
        state["engine"] = engine

        dashboard = DashboardView(engine)
        portfolio = PortfolioView(engine)
        review = ReviewView(engine)
        imports = ImportView(
            engine, on_imported=lambda: (review.refresh(), dashboard.refresh(), portfolio.refresh())
        )
        add = AddView(engine, CONFIG_PATH, on_added=dashboard.refresh)
        data = DataView(DB_PATH, on_replace=replace)
        views: list[ft.Control] = [dashboard, portfolio, imports, add, review]
        body = ft.Container(content=views[0], expand=True, padding=16)

        def on_nav(e: ft.Event[ft.NavigationBar]) -> None:
            idx = e.control.selected_index
            body.content = views[idx]
            if idx == 0:
                dashboard.refresh(update=False)
            elif idx == 1:
                portfolio.refresh(update=False)
            elif idx == 4:
                review.refresh(update=False)
            page.update()

        def show_data(_) -> None:
            data.refresh(update=False)
            body.content = data
            page.update()

        page.appbar = ft.AppBar(
            title=ft.Text("Finance Bro"),
            actions=[ft.IconButton(ft.Icons.STORAGE, tooltip="Dati", on_click=show_data)],
        )
        page.navigation_bar = ft.NavigationBar(
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.INSIGHTS, label="Cashflow"),
                ft.NavigationBarDestination(icon=ft.Icons.ACCOUNT_BALANCE, label="Patrimonio"),
                ft.NavigationBarDestination(icon=ft.Icons.UPLOAD_FILE, label="Import"),
                ft.NavigationBarDestination(icon=ft.Icons.ADD_CARD, label="Aggiungi"),
                ft.NavigationBarDestination(icon=ft.Icons.RULE, label="Da rivedere"),
            ],
            on_change=on_nav,
        )
        page.controls.clear()
        page.add(body)

    def replace(new_db: Path) -> None:
        """Sostituisce il database (importato dal PC) e ricostruisce le schermate."""
        state["engine"].dispose()  # chiude le connessioni al file prima di sovrascriverlo
        try:
            backup = replace_db(new_db, DB_PATH)
        except Exception as exc:  # noqa: BLE001
            build()
            page.show_dialog(ft.SnackBar(ft.Text(f"Import non riuscito: {exc}")))
            return
        build()
        page.show_dialog(
            ft.SnackBar(ft.Text(f"Database importato; copia del vecchio: {backup.name}"))
        )

    build()


if __name__ == "__main__":
    ft.run(main)
