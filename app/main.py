"""Interfaccia Flet — per ora uno scheletro che mostra le categorie dal database.

Avvio:
    uv run --extra app flet run app/main.py          # desktop
    uv run --extra app flet run --web app/main.py    # web (Raspberry Pi)
Build Android (prova della fase 0):
    uv run --extra app flet build apk
"""

from __future__ import annotations

import os

import flet as ft
from sqlmodel import select

from pfb_core.db import get_engine, init_db, session
from pfb_core.models import Category, CategoryGroup
from pfb_core.seed import seed_all

DB_PATH = os.environ.get("PFB_DB", "data/pfb.sqlite")


def main(page: ft.Page) -> None:
    page.title = "Personal Finance Bro"
    engine = get_engine(DB_PATH)
    init_db(engine)
    with session(engine) as s:
        seed_all(s)
        categories = s.exec(select(Category)).all()

    title = ft.Text("Personal Finance Bro", size=24, weight=ft.FontWeight.BOLD)
    controls: list[ft.Control] = [title]
    for group in CategoryGroup:
        controls.append(ft.Text(group.value, size=16, weight=ft.FontWeight.W_600))
        controls.extend(ft.Text(c.label) for c in categories if c.group == group)

    page.add(ft.ListView(controls=controls, expand=True, spacing=4, padding=16))


if __name__ == "__main__":
    ft.run(main)
