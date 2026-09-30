from pathlib import Path

import pytest
import xlwt
from sqlmodel import select

from pfb_core.db import get_engine, init_db, session
from pfb_core.models import Account
from pfb_core.seed import seed_all

FIXTURES = Path(__file__).parent / "fixtures"
TR_IBAN = "IT00A0000000000000000000006"
BPER_IBAN = "IT00A0000000000000000000005"

CONFIG = f"""
[owner]
name = "Test"

[[accounts]]
name = "BPER"
kind = "conto"
source = "xls_bper"
iban = "{BPER_IBAN}"

[[accounts]]
name = "Trade Republic"
kind = "broker"
source = "csv_tr"
iban = "{TR_IBAN}"

[[rules]]
field = "counterparty"
pattern = "ACME"
category = "Stipendio/Pensione"

[[rules]]
field = "counterparty"
pattern = "Studio Contabile"
category = "Costi attività lavorativa"

[[rules]]
field = "bank_category"
pattern = "F24"
category = "Costi attività lavorativa"

[[rules]]
field = "description"
pattern = "ABI-CAB: 03674"
internal = true
"""

# (data operazione, descrizione, entrate, uscite, categoria banca)
BPER_ROWS = [
    ("28 settembre 2026", "CANONE SERVIZIO MULTICANALITA' HOME BANKING", None, -1.0, "CANONE"),
    ("16 settembre 2026", "PAGAM. DELEGA F24 AGENZIA ENTRATE RSSMRA80A01H501U 2026", None,
     -937.14, "F24"),
    ("14 settembre 2026", "BONIFICO ISTANTANEO o/c: Giulio Verdi  ABI-CAB: 03674-01600 a favore "
     "di VERDI GIULIO Num. Bon.Sepa 123", 1000.0, None, "BONIFICO"),
    ("08 settembre 2026", "COMMISSIONI TELECOMITALIA SPA         N: 123/58", None, -1.0,
     "COMMISSIONI"),
    ("08 settembre 2026", "ADDEBITO SDD TELECOMITALIA SPA         N: 123/58", None, -49.78,
     "ADDEBITO SDD/RID"),
    ("08 settembre 2026", "COMMISSIONI TELECOMITALIA SPA         N: 123/58", None, -1.0,
     "COMMISSIONI"),  # duplicato legittimo nello stesso giorno
    ("09 febbraio 2026", "DIVISA ACQUISTO DI USD 2.000,000", None, -1726.66, ""),
]  # fmt: skip


def write_bper_xls(path: Path, rows=BPER_ROWS, iban=BPER_IBAN, closing="86,01 €") -> Path:
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Movimenti Conto")
    ws.write(6, 1, "Modena,30 settembre 2026")
    ws.write(8, 1, "CONTO")
    ws.write(8, 4, "Saldo disponibile")
    ws.write(8, 5, closing)
    ws.write(10, 1, "IBAN")
    ws.write(10, 2, iban)
    ws.write(10, 4, "Saldo iniziale")
    ws.write(10, 5, "75,90 €")
    ws.write(11, 4, "Saldo finale")
    ws.write(11, 5, closing)
    header = ["Data operazione", "Data valuta", "Descrizione", "Entrate", "Uscite", "Note",
              "Categoria", "Stato"]  # fmt: skip
    for j, h in enumerate(header, start=1):
        ws.write(19, j, h)
    r = 20
    for day, descr, inc, out, cat in rows:
        values = [day, day, descr, inc, out, None, cat, "Contabilizzato"]
        for j, v in enumerate(values, start=1):
            if v is not None:
                ws.write(r, j, v)
        r += 1
    ws.write(r, 1, "Totale")
    ws.write(r + 2, 1, "Dati Aggiornati al 30 settembre 2026")
    wb.save(str(path))
    return path


@pytest.fixture
def tr_csv() -> Path:
    return FIXTURES / "tr_export.csv"


@pytest.fixture
def bper_xls(tmp_path) -> Path:
    return write_bper_xls(tmp_path / "Lista_Movimenti.xls")


@pytest.fixture
def s(tmp_path):
    engine = get_engine(":memory:")
    init_db(engine)
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    with session(engine) as sess:
        seed_all(sess, cfg)
        yield sess


@pytest.fixture
def tr_account(s) -> Account:
    return s.exec(select(Account).where(Account.name == "Trade Republic")).one()


@pytest.fixture
def bper_account(s) -> Account:
    return s.exec(select(Account).where(Account.name == "BPER")).one()
