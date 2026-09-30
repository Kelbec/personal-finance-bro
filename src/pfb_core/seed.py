"""Dati iniziali: tassonomia delle categorie (da SpeseWhiteList), strumenti, conti.

Idempotente: rieseguirlo non crea duplicati.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from sqlmodel import Session, select

from pfb_core.models import (
    Account,
    AccountKind,
    AccountSource,
    AssetClass,
    Category,
    CategoryGroup,
    Instrument,
    Owner,
)

G = CategoryGroup

# (emoji, nome, gruppo) — stessa tassonomia del foglio CashFlow → SpeseWhiteList
CATEGORIES: list[tuple[str, str, CategoryGroup]] = [
    ("🤑", "Stipendio/Pensione", G.ENTRATE),
    ("🔑", "Rendita da Affitto", G.ENTRATE),
    ("🇮🇹", "Bonus statali", G.ENTRATE),
    ("📊", "Rendita finanziaria", G.ENTRATE),
    ("🪙", "Altre entrate", G.ENTRATE),
    ("🏠", "Casa", G.PRIMARIE),
    ("🍲", "Alimentari/Igiene", G.PRIMARIE),
    ("👶🏻", "Figli", G.PRIMARIE),
    ("🚌", "Auto/Trasporti", G.PRIMARIE),
    ("🔒", "Sicurezza/Protezione", G.PRIMARIE),
    ("🚑", "Salute Personale", G.PRIMARIE),
    ("🏋️", "Fitness/Bellezza", G.SECONDARIE),
    ("✈️", "Viaggi/Vacanze", G.SECONDARIE),
    ("👕", "Vestiario", G.SECONDARIE),
    ("🎮", "Hobbie/Gadget", G.SECONDARIE),
    ("🎬", "Intrattenimento/Socialità", G.SECONDARIE),
    ("🍔", "Ristorante/Asporto", G.SECONDARIE),
    ("🐶", "Animale", G.SECONDARIE),
    ("🎁", "Regali/Beneficenza", G.SECONDARIE),
    ("📚", "Formazione", G.SECONDARIE),
    ("🎫", "Abbonamenti", G.SECONDARIE),
    ("🏦", "Costi bancari", G.SECONDARIE),
    ("🧳", "Costi attività lavorativa", G.SECONDARIE),
    ("💸", "Tassazione non lavorativa", G.SECONDARIE),
    ("🐷", "Cuscinetto Imprevisti", G.RISPARMIO),
    ("🎯", "Cuscinetto Progetti Breve", G.RISPARMIO),
    ("📈", "Investimento", G.RISPARMIO),
    ("👴🏻", "Fondo Pensione", G.RISPARMIO),
    ("🟢", "Entrata extra fuori budget", G.EXTRA),
    ("🔴", "Uscita extra fuori budget", G.EXTRA),
]

# (isin, ticker, nome, asset class) — dal foglio Asset allocation → ISIN_mapping
INSTRUMENTS: list[tuple[str, str, str, AssetClass]] = [
    ("IE00B4WXJJ64", "EUNH", "iShares Core Euro Government Bond", AssetClass.BOND),
    ("IE00BK5BQT80", "VWCE", "Vanguard FTSE All-World (Acc)", AssetClass.STOCK),
    ("IE00BKM4GZ66", "EIMI", "iShares Core MSCI EM IMI (Acc)", AssetClass.STOCK),
    ("LU0908500753", "MEUD", "Amundi Core Stoxx Europe 600", AssetClass.STOCK),
    ("IE00B5BMR087", "CSSPX", "iShares Core S&P 500 (Acc)", AssetClass.STOCK),
    ("US0231351067", "AMZ", "Amazon.com", AssetClass.STOCK),
    ("JE00BQRFDY49", "WSLV", "WisdomTree Silver 3x Daily Leveraged", AssetClass.COMMODITY),
]


def seed_categories(s: Session) -> int:
    existing = set(s.exec(select(Category.name)).all())
    new = [Category(emoji=e, name=n, group=g) for e, n, g in CATEGORIES if n not in existing]
    s.add_all(new)
    return len(new)


def seed_instruments(s: Session) -> int:
    existing = set(s.exec(select(Instrument.isin)).all())
    new = [
        Instrument(isin=i, ticker=t, name=n, asset_class=c)
        for i, t, n, c in INSTRUMENTS
        if i not in existing
    ]
    s.add_all(new)
    return len(new)


def seed_from_config(s: Session, config_path: str | Path) -> int:
    """Crea owner e conti leggendo config.toml (che contiene gli IBAN, fuori da git)."""
    cfg = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    name = cfg["owner"]["name"]
    owner = s.exec(select(Owner).where(Owner.name == name)).first()
    if owner is None:
        owner = Owner(name=name)
        s.add(owner)
        s.flush()

    existing = set(s.exec(select(Account.name)).all())
    new = [
        Account(
            owner_id=owner.id,
            name=a["name"],
            kind=AccountKind(a["kind"]),
            source=AccountSource(a["source"]),
            iban=a.get("iban"),
        )
        for a in cfg.get("accounts", [])
        if a["name"] not in existing
    ]
    s.add_all(new)
    return len(new)


def seed_all(s: Session, config_path: str | Path | None = None) -> dict[str, int]:
    counts = {"categories": seed_categories(s), "instruments": seed_instruments(s)}
    if config_path is not None:
        counts["accounts"] = seed_from_config(s, config_path)
    s.commit()
    return counts
