import datetime as dt
import io
import json
from decimal import Decimal

import pytest
from sqlmodel import select

from pfb_core import prices
from pfb_core.backup import InvalidDatabase, inspect, replace_db, snapshot_bytes
from pfb_core.db import get_engine, init_db, session
from pfb_core.importer import import_file
from pfb_core.models import Account, AccountSource
from pfb_core.seed import seed_all
from tests.conftest import CONFIG


def make_db(path):
    engine = get_engine(path)
    init_db(engine)
    with session(engine) as s:
        seed_all(s)
    engine.dispose()
    return path


def test_snapshot_and_inspect_count_transactions(tmp_path, tr_csv):
    db = make_db(tmp_path / "pfb.sqlite")
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    engine = get_engine(db)
    with session(engine) as s:
        seed_all(s, cfg)
        account = s.exec(select(Account).where(Account.source == AccountSource.CSV_TR)).one()
        import_file(s, account, tr_csv)
    out = tmp_path / "copia.sqlite"
    out.write_bytes(snapshot_bytes(db))  # con l'engine ancora aperto, come nell'app
    engine.dispose()
    info = inspect(out)
    assert info.transactions > 0
    assert info.accounts >= 1
    assert info.last_date is not None


def test_inspect_rejects_foreign_files(tmp_path):
    text = tmp_path / "export.csv"
    text.write_text("date,amount\n2026-01-01,1\n")
    with pytest.raises(InvalidDatabase, match="SQLite"):
        inspect(text)
    other = tmp_path / "altro.sqlite"
    import sqlite3

    with sqlite3.connect(other) as con:
        con.execute("CREATE TABLE t (x)")
    with pytest.raises(InvalidDatabase, match="mancano"):
        inspect(other)


def test_replace_db_keeps_a_backup(tmp_path):
    current = make_db(tmp_path / "data" / "pfb.sqlite")
    new = make_db(tmp_path / "dal-pc.sqlite")
    before = current.read_bytes()
    backup = replace_db(new, current, now=dt.datetime(2026, 10, 14, 9, 30))
    assert backup.name == "pfb.backup-20261014-093000.sqlite"
    assert backup.read_bytes() == before
    assert current.read_bytes() == new.read_bytes()


def test_replace_db_refuses_invalid_file(tmp_path):
    current = make_db(tmp_path / "pfb.sqlite")
    bad = tmp_path / "rotto.sqlite"
    bad.write_bytes(b"non sono un database")
    before = current.read_bytes()
    with pytest.raises(InvalidDatabase):
        replace_db(bad, current)
    assert current.read_bytes() == before


def test_chart_provider_parses_yahoo_response(monkeypatch):
    payload = {"chart": {"error": None, "result": [{
        "meta": {"gmtoffset": 7200},
        "timestamp": [1791183600, 1791270000, 1791356400],  # 07:00 ora di Milano
        "indicators": {"quote": [{"close": [54.37, None, 53.18]}]},
    }]}}  # fmt: skip
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    rows = prices.chart_provider("WSLV.MI", dt.date(2026, 10, 5), dt.date(2026, 10, 7))
    assert "WSLV.MI" in seen["url"]
    assert rows == [
        (dt.date(2026, 10, 5), Decimal("54.37")),
        (dt.date(2026, 10, 7), Decimal("53.18")),
    ]


def test_chart_provider_reports_yahoo_errors(monkeypatch):
    payload = {"chart": {"error": {"description": "No data found"}, "result": None}}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: io.BytesIO(json.dumps(payload).encode()))  # fmt: skip
    with pytest.raises(RuntimeError, match="No data"):
        prices.chart_provider("XXX", dt.date(2026, 1, 1), dt.date(2026, 1, 2))
