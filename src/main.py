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

from pfb_app.views import AddView, DashboardView, ImportView, PortfolioView, ReviewView
from pfb_core.db import get_engine, init_db, session
from pfb_core.seed import seed_all


def _base_dir() -> Path:
    """Cartella di data/pfb.sqlite e config.toml.

    Dai sorgenti è la radice del progetto: `flet run` sposta la cartella di lavoro in
    src/.flet/storage/data, e con percorsi relativi l'app aprirebbe un database vuoto.
    Nell'app installata (senza pyproject.toml accanto) è la cartella dati dell'app.
    """
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").exists():
        return root
    return Path(os.environ.get("FLET_APP_STORAGE_DATA", "."))


DB_PATH = os.environ.get("PFB_DB", str(_base_dir() / "data" / "pfb.sqlite"))
CONFIG_PATH = Path(os.environ.get("PFB_CONFIG", _base_dir() / "config.toml"))


def main(page: ft.Page) -> None:
    page.title = "Personal Finance Bro"
    engine = get_engine(DB_PATH)
    init_db(engine)
    with session(engine) as s:
        seed_all(s, CONFIG_PATH if CONFIG_PATH.exists() else None)

    dashboard = DashboardView(engine)
    portfolio = PortfolioView(engine)
    review = ReviewView(engine)
    imports = ImportView(
        engine, on_imported=lambda: (review.refresh(), dashboard.refresh(), portfolio.refresh())
    )
    add = AddView(engine, CONFIG_PATH, on_added=dashboard.refresh)
    views: list[ft.Control] = [dashboard, portfolio, imports, add, review]
    body = ft.Container(content=views[0], expand=True, padding=16)

    def on_nav(e: ft.Event[ft.NavigationBar]) -> None:
        idx = e.control.selected_index
        body.content = views[idx]
        if idx == 0:
            dashboard.refresh()
        elif idx == 1:
            portfolio.refresh()
        elif idx == 4:
            review.refresh()
        page.update()

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
    page.add(body)


if __name__ == "__main__":
    ft.run(main)
