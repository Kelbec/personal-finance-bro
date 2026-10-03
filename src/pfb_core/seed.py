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
    RuleField,
    RuleOrigin,
)
from pfb_core.rules import PRIO_CONFIG, PRIO_INTERNAL, seed_default_rules, upsert_rule

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

# (isin, ticker, nome, asset class, simbolo Yahoo) — dal foglio Asset allocation → ISIN_mapping.
# Simboli Yahoo in EUR; quelli dubbi restano None e si impostano da config.toml ([[instruments]]).
INSTRUMENTS: list[tuple[str, str, str, AssetClass, str | None]] = [
    ("IE00B4WXJJ64", "EUNH", "iShares Core Euro Government Bond", AssetClass.BOND, "EUNH.DE"),
    ("IE00BK5BQT80", "VWCE", "Vanguard FTSE All-World (Acc)", AssetClass.STOCK, "VWCE.DE"),
    ("IE00BKM4GZ66", "EIMI", "iShares Core MSCI EM IMI (Acc)", AssetClass.STOCK, "IS3N.DE"),
    ("LU0908500753", "MEUD", "Amundi Core Stoxx Europe 600", AssetClass.STOCK, "MEUD.PA"),
    ("IE00B5BMR087", "CSSPX", "iShares Core S&P 500 (Acc)", AssetClass.STOCK, "SXR8.DE"),
    ("US0231351067", "AMZ", "Amazon.com", AssetClass.STOCK, "AMZ.DE"),
    ("JE00BQRFDY49", "WSLV", "WisdomTree Silver 3x Daily Leveraged", AssetClass.COMMODITY, None),
]


def seed_categories(s: Session) -> int:
    existing = set(s.exec(select(Category.name)).all())
    new = [Category(emoji=e, name=n, group=g) for e, n, g in CATEGORIES if n not in existing]
    s.add_all(new)
    return len(new)


def seed_instruments(s: Session) -> int:
    existing = {i.isin: i for i in s.exec(select(Instrument)).all()}
    n = 0
    for isin, ticker, name, cls, yahoo in INSTRUMENTS:
        inst = existing.get(isin)
        if inst is None:
            s.add(Instrument(isin=isin, ticker=ticker, name=name, asset_class=cls,
                             yahoo_symbol=yahoo))  # fmt: skip
            n += 1
        elif inst.yahoo_symbol is None and yahoo:  # strumento creato dall'import, senza simbolo
            inst.yahoo_symbol = yahoo
            s.add(inst)
    return n


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
    s.flush()

    # IBAN dei propri conti → giroconto
    for a in cfg.get("accounts", []):
        if a.get("iban"):
            upsert_rule(s, RuleField.IBAN, a["iban"], is_internal=True,
                        priority=PRIO_INTERNAL, origin=RuleOrigin.CONFIG)  # fmt: skip

    # strumenti: simbolo Yahoo, asset class o strumenti nuovi
    for i in cfg.get("instruments", []):
        inst = s.get(Instrument, i["isin"])
        if inst is None:
            inst = Instrument(isin=i["isin"], ticker=i.get("ticker", i["isin"]),
                              name=i.get("name", i["isin"]),
                              asset_class=AssetClass(i.get("asset_class", "stock")))  # fmt: skip
        for key in ("ticker", "name", "yahoo_symbol"):
            if key in i:
                setattr(inst, key, i[key])
        if "asset_class" in i:
            inst.asset_class = AssetClass(i["asset_class"])
        s.add(inst)

    # regole personali
    for r in cfg.get("rules", []):
        upsert_rule(
            s,
            RuleField(r["field"]),
            r["pattern"],
            category=r.get("category"),
            is_internal=r.get("internal", False),
            priority=r.get("priority", PRIO_CONFIG),
            origin=RuleOrigin.CONFIG,
        )
    return len(new)


def seed_all(s: Session, config_path: str | Path | None = None) -> dict[str, int]:
    counts = {"categories": seed_categories(s), "instruments": seed_instruments(s)}
    s.flush()
    counts["rules"] = seed_default_rules(s)
    if config_path is not None:
        counts["accounts"] = seed_from_config(s, config_path)
    s.commit()
    return counts
