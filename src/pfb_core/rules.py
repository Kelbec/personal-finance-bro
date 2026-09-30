"""Motore di categorizzazione a regole.

Le regole sono valutate in ordine di `priority` (più bassa prima): la prima che fa match
decide categoria e/o giroconto. Quello che non fa match resta nella coda di revisione.

Priorità di default:
     1  IBAN dei propri conti → giroconto
     5  regole create dall'utente correggendo una categoria
    10  tipo operazione (BUY, INTEREST_PAYMENT, ...)
    20  regole personali da config.toml (datore di lavoro, commercialista, ...)
    30  categoria assegnata dalla banca (BPER: CANONE, COMMISSIONI, ...)
    40  esercenti noti (Iliad, Telecom, streaming, ...)
    60  codice MCC della carta
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from sqlmodel import Session, select

from pfb_core.models import Category, Rule, RuleField, RuleOrigin, Transaction

F = RuleField

PRIO_INTERNAL, PRIO_USER, PRIO_TYPE, PRIO_CONFIG, PRIO_BANK, PRIO_MERCHANT, PRIO_MCC = (
    1, 5, 10, 20, 30, 40, 60,
)  # fmt: skip

# (campo, pattern, categoria, priorità)
DEFAULT_RULES: list[tuple[RuleField, str, str, int]] = [
    # tipo operazione (Trade Republic)
    (F.TYPE, "BUY", "Investimento", PRIO_TYPE),
    (F.TYPE, "SELL", "Investimento", PRIO_TYPE),
    (F.TYPE, "INTEREST_PAYMENT", "Rendita finanziaria", PRIO_TYPE),
    (F.TYPE, "BENEFITS_SAVEBACK", "Rendita finanziaria", PRIO_TYPE),
    (F.TYPE, "DIVIDEND", "Rendita finanziaria", PRIO_TYPE),
    (F.TYPE, "TAX", "Tassazione non lavorativa", PRIO_TYPE),
    # categoria della banca (BPER)
    (F.BANK_CATEGORY, "CANONE", "Costi bancari", PRIO_BANK),
    (F.BANK_CATEGORY, "COMMISSIONI", "Costi bancari", PRIO_BANK),
    (F.BANK_CATEGORY, "COMMISSIONE", "Costi bancari", PRIO_BANK),
    (F.BANK_CATEGORY, "COMPETENZE", "Costi bancari", PRIO_BANK),
    # esercenti noti
    (F.COUNTERPARTY, r"\biliad\b", "Abbonamenti", PRIO_MERCHANT),
    (F.COUNTERPARTY, r"telecom ?italia|\btim\b", "Casa", PRIO_MERCHANT),
    (
        F.COUNTERPARTY,
        r"prime video|netflix|spotify|disney\+?|youtube premium",
        "Abbonamenti",
        PRIO_MERCHANT,
    ),  # fmt: skip
    # codici MCC
    (F.MCC, "5812", "Ristorante/Asporto", PRIO_MCC),
    (F.MCC, "5814", "Ristorante/Asporto", PRIO_MCC),
    (F.MCC, "5813", "Intrattenimento/Socialità", PRIO_MCC),
    (F.MCC, "5411", "Alimentari/Igiene", PRIO_MCC),
    (F.MCC, "5499", "Alimentari/Igiene", PRIO_MCC),
    (F.MCC, "5912", "Salute Personale", PRIO_MCC),
    (F.MCC, "8011-8099", "Salute Personale", PRIO_MCC),
    (F.MCC, "5541", "Auto/Trasporti", PRIO_MCC),
    (F.MCC, "5542", "Auto/Trasporti", PRIO_MCC),
    (F.MCC, "4784", "Auto/Trasporti", PRIO_MCC),
    (F.MCC, "7523", "Auto/Trasporti", PRIO_MCC),
    (F.MCC, "4121", "Auto/Trasporti", PRIO_MCC),
    (F.MCC, "4131", "Auto/Trasporti", PRIO_MCC),
    (F.MCC, "4112", "Viaggi/Vacanze", PRIO_MCC),
    (F.MCC, "4511", "Viaggi/Vacanze", PRIO_MCC),
    (F.MCC, "3000-3299", "Viaggi/Vacanze", PRIO_MCC),  # compagnie aeree
    (F.MCC, "3351-3441", "Viaggi/Vacanze", PRIO_MCC),  # noleggio auto
    (F.MCC, "3501-3999", "Viaggi/Vacanze", PRIO_MCC),  # hotel
    (F.MCC, "7011", "Viaggi/Vacanze", PRIO_MCC),
    (F.MCC, "4814", "Abbonamenti", PRIO_MCC),
    (F.MCC, "7230", "Fitness/Bellezza", PRIO_MCC),
    (F.MCC, "7997", "Fitness/Bellezza", PRIO_MCC),
    (F.MCC, "5651", "Vestiario", PRIO_MCC),
    (F.MCC, "5691", "Vestiario", PRIO_MCC),
    (F.MCC, "5732", "Hobbie/Gadget", PRIO_MCC),
    (F.MCC, "5942", "Formazione", PRIO_MCC),
]

_EXACT_FIELDS = {F.TYPE, F.IBAN, F.BANK_CATEGORY}
_REGEX_FIELDS = {F.COUNTERPARTY, F.DESCRIPTION}


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _value(tx: Transaction, field: RuleField) -> str | None:
    return {
        F.TYPE: tx.type,
        F.COUNTERPARTY: tx.counterparty,
        F.IBAN: tx.iban,
        F.MCC: tx.mcc,
        F.BANK_CATEGORY: tx.bank_category,
        F.DESCRIPTION: tx.description,
    }[field]


def matches(rule: Rule, tx: Transaction) -> bool:
    value = _value(tx, rule.field)
    if not value:
        return False
    if rule.field in _EXACT_FIELDS:
        return value.strip().casefold() == rule.pattern.strip().casefold()
    if rule.field in _REGEX_FIELDS:
        return _regex(rule.pattern).search(value) is not None
    # MCC: codice singolo o intervallo "a-b"
    if "-" in rule.pattern:
        lo, hi = (int(x) for x in rule.pattern.split("-", 1))
        return value.isdigit() and lo <= int(value) <= hi
    return value.strip() == rule.pattern.strip()


def load_rules(s: Session) -> list[Rule]:
    rules = s.exec(select(Rule).where(Rule.enabled == True)).all()  # noqa: E712
    return sorted(rules, key=lambda r: (r.priority, r.id or 0))


def categorize(tx: Transaction, rules: Sequence[Rule]) -> Rule | None:
    """Applica la prima regola che fa match. Restituisce la regola usata."""
    for rule in rules:
        if matches(rule, tx):
            tx.is_internal = rule.is_internal
            tx.category_id = None if rule.is_internal else rule.category_id
            return rule
    return None


def apply_rules(s: Session, txs: Iterable[Transaction] | None = None) -> int:
    """Categorizza i movimenti dati (default: tutti quelli ancora da rivedere)."""
    if txs is None:
        txs = pending_review(s)
    rules = load_rules(s)
    n = 0
    for tx in txs:
        if categorize(tx, rules):
            s.add(tx)
            n += 1
    return n


def pending_review(s: Session) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .where(Transaction.category_id == None, Transaction.is_internal == False)  # noqa: E711, E712
        .order_by(Transaction.date.desc())
    )
    return list(s.exec(stmt).all())


def _category_id(s: Session, name: str) -> int:
    cat = s.exec(select(Category).where(Category.name == name)).first()
    if cat is None:
        raise ValueError(f"categoria sconosciuta: {name!r}")
    return cat.id


def upsert_rule(
    s: Session,
    field: RuleField,
    pattern: str,
    *,
    category: str | int | None = None,
    is_internal: bool = False,
    priority: int = PRIO_CONFIG,
    origin: RuleOrigin = RuleOrigin.CONFIG,
) -> Rule:
    cat_id = _category_id(s, category) if isinstance(category, str) else category
    rule = s.exec(select(Rule).where(Rule.field == field, Rule.pattern == pattern)).first()
    if rule is None:
        rule = Rule(field=field, pattern=pattern)
    rule.category_id = cat_id
    rule.is_internal = is_internal
    rule.priority = priority
    rule.origin = origin
    s.add(rule)
    return rule


def seed_default_rules(s: Session) -> int:
    existing = {(r.field, r.pattern) for r in s.exec(select(Rule)).all()}
    n = 0
    for field, pattern, category, prio in DEFAULT_RULES:
        if (field, pattern) not in existing:
            upsert_rule(s, field, pattern, category=category, priority=prio,
                        origin=RuleOrigin.DEFAULT)  # fmt: skip
            n += 1
    return n


@dataclass
class Correction:
    transaction: Transaction
    rule: Rule | None
    also_categorized: int


def set_category(
    s: Session, tx: Transaction, category_id: int, *, remember: bool = True
) -> Correction:
    """Corregge la categoria di un movimento; con `remember` crea una regola sulla
    controparte e la applica agli altri movimenti in attesa di revisione."""
    tx.category_id = category_id
    tx.is_internal = False
    s.add(tx)
    rule = None
    others = 0
    if remember and tx.counterparty:
        pattern = rf"^{re.escape(tx.counterparty.strip())}$"
        rule = upsert_rule(
            s, F.COUNTERPARTY, pattern, category=category_id,
            priority=PRIO_USER, origin=RuleOrigin.USER,
        )  # fmt: skip
        s.flush()
        pending = [t for t in pending_review(s) if t.id != tx.id]
        others = apply_rules(s, (t for t in pending if matches(rule, t)))
    s.commit()
    return Correction(tx, rule, others)


def mark_internal(s: Session, tx: Transaction) -> None:
    tx.is_internal = True
    tx.category_id = None
    s.add(tx)
    s.commit()
