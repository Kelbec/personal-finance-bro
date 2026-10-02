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

from pfb_app.views import DashboardView, ImportView, ReviewView
from pfb_core.db import get_engine, init_db, session
from pfb_core.seed import seed_all

DB_PATH = os.environ.get("PFB_DB", "data/pfb.sqlite")
CONFIG_PATH = Path(os.environ.get("PFB_CONFIG", "config.toml"))


def main(page: ft.Page) -> None:
    page.title = "Personal Finance Bro"
    engine = get_engine(DB_PATH)
    init_db(engine)
    with session(engine) as s:
        seed_all(s, CONFIG_PATH if CONFIG_PATH.exists() else None)

    dashboard = DashboardView(engine)
    review = ReviewView(engine)
    imports = ImportView(engine, on_imported=lambda: (review.refresh(), dashboard.refresh()))
    views: list[ft.Control] = [dashboard, imports, review]
    body = ft.Container(content=views[0], expand=True, padding=16)

    def on_nav(e: ft.Event[ft.NavigationBar]) -> None:
        idx = e.control.selected_index
        body.content = views[idx]
        if idx == 0:
            dashboard.refresh()
        elif idx == 2:
            review.refresh()
        page.update()

    page.navigation_bar = ft.NavigationBar(
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.INSIGHTS, label="Cashflow"),
            ft.NavigationBarDestination(icon=ft.Icons.UPLOAD_FILE, label="Import"),
            ft.NavigationBarDestination(icon=ft.Icons.RULE, label="Da rivedere"),
        ],
        on_change=on_nav,
    )
    page.add(body)


if __name__ == "__main__":
    ft.run(main)
