"""Requisiti dell'APK che si scoprirebbero solo aprendo l'app sul telefono."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

PYPROJECT = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))


def _names(deps: list[str]) -> set[str]:
    return {Requirement(d).name for d in deps}


def test_apk_ships_flet_and_certifi():
    # con dipendenze dichiarate, flet build non aggiunge flet da solo; e lo script di avvio
    # di Flet su Android fa `import certifi` prima di main.py
    android = _names(PYPROJECT["tool"]["flet"]["android"]["dependencies"])
    assert {"flet", "certifi"} <= android


def test_apk_dependencies_stay_light():
    # nell'APK finiscono solo le dipendenze base: niente librerie senza wheel Android
    base = _names(PYPROJECT["project"]["dependencies"])
    assert not base & {"pandas", "yfinance", "gspread", "numpy"}
