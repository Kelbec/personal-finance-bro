"""Sincronizzazione verso il foglio Google con un client finto: nessuna rete."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlmodel import select

from pfb_core.models import Account, AccountKind, AccountSource, Category, SheetSync, Transaction
from pfb_core.sheets_sync import (
    SheetConfig,
    SheetLayoutError,
    add_manual_transaction,
    detect_layout,
    load_sheet_config,
    manual_accounts,
    plan_sync,
    run_sync,
)

D = Decimal
HEADER = ["Causale", "Categoria", "Importo", "Data", "Mese", "Intestatario"]
FORM_HEADER = ["Informazioni cronologiche", "Causale", "Categoria", "Importo (€)", "Data"]
TAB = "CashFlowTest"
NOW = dt.datetime(2026, 10, 3, 15, 40, 0)


class FakeSheet:
    """Una scheda: `rows` come valori grezzi, `formulas` come le restituisce Sheets."""

    def __init__(self, rows=None, header=HEADER, formulas=None, tab=TAB):
        self.tab = tab
        self.data = {tab: [header, *(rows or [])]}
        self.formulas = formulas
        self.appended = []

    def rows(self, tab, formulas=False):
        if formulas and self.formulas is not None:
            return self.formulas
        return self.data[tab]

    def append(self, tab, rows, header_row):
        self.appended.append((tab, rows, header_row))
        self.data[tab].extend(rows)


@pytest.fixture
def cfg(tmp_path):
    return SheetConfig(spreadsheet="id", credentials=tmp_path / "k.json",
                       since=dt.date(2026, 9, 1), tabs={"Test": TAB})  # fmt: skip


def _cat(s, name):
    return s.exec(select(Category).where(Category.name == name)).one()


def _tx(s, account, day, amount, cat=None, internal=False, sid="x"):
    tx = Transaction(account_id=account.id, date=day, amount=D(amount), type="CARD",
                     counterparty="Bar  Centrale", category_id=cat.id if cat else None,
                     is_internal=internal, source_id=sid)  # fmt: skip
    s.add(tx)
    s.commit()
    return tx


# --- configurazione ----------------------------------------------------------
def test_load_config(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[google_sheet]\nfoglio = "https://docs.google.com/spreadsheets/d/abc"\n'
        'dal = "2026-05-01"\n[google_sheet.schede]\nTest = "CashFlowTest"\n',
        encoding="utf-8",
    )
    c = load_sheet_config(cfg_file)
    assert c.since == dt.date(2026, 5, 1)
    assert c.tabs == {"Test": "CashFlowTest"}
    assert c.credentials == tmp_path / "google-credentials.json"  # relativa a config.toml


def test_no_section_means_sync_off(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[owner]\nname = "Test"\n', encoding="utf-8")
    assert load_sheet_config(f) is None
    assert load_sheet_config(tmp_path / "manca.toml") is None


# --- struttura della scheda --------------------------------------------------
def test_layout_of_form_responses():
    rows = [FORM_HEADER, [46007.24, "Interessi", "📊 Rendita finanziaria", "7.78", 45992]]
    layout = detect_layout(rows)
    assert layout.header_row == 1
    assert layout.columns == {"timestamp": 0, "causale": 1, "categoria": 2, "importo": 3,
                              "data": 4}  # fmt: skip
    assert layout.amount_as_text and layout.decimal_sep == "."


def test_layout_with_title_row_and_numbers():
    rows = [["LISTA MOVIMENTI"], ["Causale", "Categoria", "Importo", "Data", "Mese", "Anno"],
            ["Canone", "🏦 Costi bancari", 1, 45685, 1, 2025]]  # fmt: skip
    layout = detect_layout(rows)
    assert layout.header_row == 2
    assert not layout.amount_as_text
    assert layout.columns["anno"] == 5


def test_layout_without_required_columns():
    with pytest.raises(SheetLayoutError):
        detect_layout([["Data", "Importo"], [45685, 1]])


# --- cosa inviare ------------------------------------------------------------
def test_plan_filters_and_formats(s, bper_account, cfg):
    bar = _cat(s, "Ristorante/Asporto")
    salary = _cat(s, "Stipendio/Pensione")
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", bar, sid="a")
    _tx(s, bper_account, dt.date(2026, 9, 4), "2.00", bar, sid="b")  # rimborso
    _tx(s, bper_account, dt.date(2026, 9, 27), "2000", salary, sid="c")
    _tx(s, bper_account, dt.date(2026, 9, 5), "-10", None, sid="d")  # da rivedere
    _tx(s, bper_account, dt.date(2026, 9, 6), "-500", bar, internal=True, sid="e")  # giroconto
    _tx(s, bper_account, dt.date(2026, 8, 31), "-3", bar, sid="f")  # prima di `dal`

    plan = plan_sync(s, cfg)
    layout = detect_layout([HEADER])
    assert [r.values(layout, NOW) for r in plan.rows] == [
        ["Bar Centrale", "🍔 Ristorante/Asporto", 4.5, "2026-09-03", 9, "Test"],
        ["Bar Centrale", "🍔 Ristorante/Asporto", -2.0, "2026-09-04", 9, "Test"],
        ["Bar Centrale", "🤑 Stipendio/Pensione", 2000.0, "2026-09-27", 9, "Test"],
    ]
    assert (plan.first, plan.last) == (dt.date(2026, 9, 3), dt.date(2026, 9, 27))


def test_history_accounts_are_never_synced(s, cfg):
    owner_id = s.exec(select(Account)).first().owner_id
    storico = Account(owner_id=owner_id, name="Storico Test", kind=AccountKind.STORICO,
                      source=AccountSource.MANUALE)  # fmt: skip
    s.add(storico)
    s.commit()
    _tx(s, storico, dt.date(2026, 9, 3), "-4.50", _cat(s, "Casa"))
    assert plan_sync(s, cfg).rows == []


def test_owner_without_tab_is_reported(s, bper_account, cfg):
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", _cat(s, "Casa"))
    cfg.tabs = {}
    plan = plan_sync(s, cfg)
    assert plan.rows == [] and plan.no_tab == {"Test": 1}


# --- scrittura ---------------------------------------------------------------
def test_sync_writes_once_and_skips_rows_already_in_sheet(s, bper_account, cfg):
    bar = _cat(s, "Ristorante/Asporto")
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", bar, sid="a")
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", bar, sid="b")  # stesso caffè, due volte
    _tx(s, bper_account, dt.date(2026, 9, 10), "-30", _cat(s, "Casa"), sid="c")
    # uno era già stato scritto a mano (data seriale e virgola come in Sheets)
    sheet = FakeSheet([["Caffè", "🍔 Ristorante/Asporto", "4,5", 46268, 9, "Test"]])

    r = run_sync(s, plan_sync(s, cfg), sheet, NOW)
    assert r.already_there == 1
    assert r.written == {TAB: 2}
    tab, rows, header_row = sheet.appended[0]
    assert header_row == 1
    assert [row[3] for row in rows] == ["2026-09-03", "2026-09-10"]
    assert len(s.exec(select(SheetSync)).all()) == 3

    again = plan_sync(s, cfg)
    assert again.rows == []  # niente da reinviare
    assert run_sync(s, again, sheet, NOW).written == {}


def test_sync_to_form_responses_writes_like_a_form_submission(s, bper_account, cfg):
    _tx(s, bper_account, dt.date(2026, 9, 3), "-12.40", _cat(s, "Ristorante/Asporto"))
    sheet = FakeSheet([[46007.24, "Interessi", "📊 Rendita finanziaria", "7.78", 45992]],
                      header=FORM_HEADER)  # fmt: skip
    run_sync(s, plan_sync(s, cfg), sheet, NOW)
    assert sheet.appended[0][1] == [
        ["2026-10-03 15:40:00", "Bar Centrale", "🍔 Ristorante/Asporto", "'12.40", "2026-09-03"]
    ]


def test_derived_tab_is_refused_and_nothing_written(s, bper_account, cfg):
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", _cat(s, "Casa"))
    header = ["Causale", "Categoria", "Importo", "Data", "Mese", "Anno"]
    rows = [["Canone", "🏦 Costi bancari", 1, 45685, 1, 2025]]
    spill = "=ARRAYFORMULA('Risposte del modulo 1'!B2:B)"
    formulas = [["LISTA MOVIMENTI"], header,
                ["Canone", "🏦 Costi bancari", "1", "45685", "=MONTH(D3)", "=YEAR(D3)"],
                [spill, "", "", "", "=MONTH(D4)", ""]]  # fmt: skip
    sheet = FakeSheet(rows, header=header, formulas=formulas)
    sheet.data[TAB].insert(0, ["LISTA MOVIMENTI"])
    with pytest.raises(SheetLayoutError, match="formule"):
        run_sync(s, plan_sync(s, cfg), sheet, NOW)
    assert sheet.appended == []
    assert s.exec(select(SheetSync)).all() == []


def test_formulas_only_in_month_year_are_fine(s, bper_account, cfg):
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", _cat(s, "Casa"))
    header = ["Causale", "Categoria", "Importo", "Data", "Mese", "Anno"]
    formulas = [header, ["Canone", "🏦 Costi bancari", "1", "45685", "=MONTH(D2)", "=YEAR(D2)"]]
    sheet = FakeSheet([["Canone", "🏦 Costi bancari", 1, 45685, 1, 2025]], header=header,
                      formulas=formulas)  # fmt: skip
    assert run_sync(s, plan_sync(s, cfg), sheet, NOW).written == {TAB: 1}
    assert sheet.appended[0][1][0][4:] == [9, 2026]


def test_dry_run_reads_but_does_not_write(s, bper_account, cfg):
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", _cat(s, "Casa"))
    sheet = FakeSheet()
    r = run_sync(s, plan_sync(s, cfg), sheet, NOW, write=False)
    assert r.written == {TAB: 1}
    assert r.preview[TAB][0][:4] == ["Bar Centrale", "🏠 Casa", 4.5, "2026-09-03"]
    assert sheet.appended == []
    assert s.exec(select(SheetSync)).all() == []


def test_failed_append_marks_nothing(s, bper_account, cfg):
    _tx(s, bper_account, dt.date(2026, 9, 3), "-4.50", _cat(s, "Casa"))

    class Broken(FakeSheet):
        def append(self, tab, rows, header_row):
            raise ConnectionError("offline")

    with pytest.raises(ConnectionError):
        run_sync(s, plan_sync(s, cfg), Broken(), NOW)
    s.rollback()
    assert s.exec(select(SheetSync)).all() == []
    assert len(plan_sync(s, cfg).rows) == 1  # al prossimo tentativo riparte


# --- inserimento manuale -----------------------------------------------------
def test_manual_transaction_sign_and_accounts(s, bper_account, cfg):
    accounts = manual_accounts(s)
    assert bper_account not in accounts  # le spese bancarie arrivano dall'export
    cash = Account(owner_id=bper_account.owner_id, name="Contanti", kind=AccountKind.WALLET,
                   source=AccountSource.MANUALE)  # fmt: skip
    s.add(cash)
    s.commit()
    assert [a.name for a in manual_accounts(s)] == ["Contanti"]

    tx = add_manual_transaction(s, cash, dt.date(2026, 9, 12), D("12.40"),
                                _cat(s, "Ristorante/Asporto"), " Pizza ")  # fmt: skip
    assert tx.amount == D("-12.40") and tx.description == "Pizza"
    gift = add_manual_transaction(s, cash, dt.date(2026, 9, 12), D("50"),
                                  _cat(s, "Altre entrate"), "Regalo")  # fmt: skip
    assert gift.amount == D("50")
    with pytest.raises(ValueError):
        add_manual_transaction(s, cash, dt.date(2026, 9, 12), D("0"), _cat(s, "Casa"), "")

    layout = detect_layout([HEADER])
    values = [r.values(layout, NOW) for r in plan_sync(s, cfg).rows]
    assert ["Pizza", "🍔 Ristorante/Asporto", 12.4, "2026-09-12", 9, "Test"] in values


def test_causale_drops_transaction_codes(s, tr_account, cfg):
    tx = Transaction(account_id=tr_account.id, date=dt.date(2026, 9, 3), amount=D("9.15"),
                     type="BENEFITS_SAVEBACK", category_id=_cat(s, "Rendita finanziaria").id,
                     description="Saveback cash reward ad35e643-e16d-421b-ba88-10b66a565523 "
                                 "for reservation: 520dd", source_id="sb")  # fmt: skip
    s.add(tx)
    s.commit()
    cfg.tabs = {"Test": TAB}
    assert plan_sync(s, cfg).rows[0].causale == "Saveback cash reward"
