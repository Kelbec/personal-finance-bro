"""Smoke test delle schermate Flet senza client grafico (saltato se flet non è installato)."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlmodel import select

ft = pytest.importorskip("flet")

from pfb_app.views import ImportView, ReviewView  # noqa: E402
from pfb_core.models import Category, Transaction  # noqa: E402


@pytest.fixture
def engine(s):
    return s.get_bind()


def test_import_view_lists_importable_accounts(engine):
    view = ImportView(engine)
    names = sorted(o.text for o in view.account.options)
    assert names == ["BPER", "Trade Republic"]


def test_import_then_review_flow(engine, s, tr_account, tr_csv):
    review = ReviewView(engine)
    assert review.header.value == "Da rivedere (0)"

    imp = ImportView(engine, on_imported=review.refresh)
    imp._run_import(tr_account.id, tr_csv)
    assert any("Da rivedere" in c.value for c in imp.result.controls)
    pending = int(review.header.value.split("(")[1].rstrip(")"))
    assert pending > 0

    # scelta di una categoria dal menu a tendina di una riga
    tx = s.exec(select(Transaction).where(Transaction.counterparty == "Mario Rossi")).one()
    gift = s.exec(select(Category).where(Category.name == "Regali/Beneficenza")).one()
    dropdown = SimpleNamespace(data=tx.id, value=str(gift.id))
    review._on_category(SimpleNamespace(control=dropdown))
    assert review.header.value == f"Da rivedere ({pending - 1})"


def test_mark_internal_from_review(engine, s, tr_account, tr_csv):
    review = ReviewView(engine)
    ImportView(engine, on_imported=review.refresh)._run_import(tr_account.id, tr_csv)
    before = review.header.value
    tx = s.exec(select(Transaction).where(Transaction.counterparty == "Mario Rossi")).one()
    review._on_internal(SimpleNamespace(control=SimpleNamespace(data=tx.id)))
    assert review.header.value != before


def test_dashboard_reflects_import(engine, s, tr_account, tr_csv):
    from pfb_app.views import DashboardView

    dash = DashboardView(engine)
    assert dash.month_picker.value is None
    ImportView(engine)._run_import(tr_account.id, tr_csv)
    dash.refresh()
    assert dash.month_picker.value is not None
    assert len(dash.kpis.controls) >= 5


def test_portfolio_view_renders_and_saves_balance(engine, s, tr_account):
    from pfb_app.views import PortfolioView
    from pfb_core.portfolio import add_initial_position

    view = PortfolioView(engine)
    assert any("Nessun dato" in getattr(c, "value", "") for c in view.allocation.controls)

    import datetime as dt
    from decimal import Decimal

    add_initial_position(s, tr_account, "IE00BK5BQT80", Decimal(10), Decimal(100),
                         dt.date(2026, 8, 31))  # fmt: skip
    view.balance_account.value = str(tr_account.id)
    view.balance_date.value = "2026-08-31"
    view.balance_value.value = "1.234,50".replace(".", "")
    view._on_balance(None)
    assert view.positions.controls  # VWCE + liquidità Trade Republic
    assert any(getattr(t, "value", "").startswith("Patrimonio") for t in
               (c.content.content.controls[0] for c in view.kpis.controls))  # fmt: skip


def test_pick_registers_picker_on_page_and_imports(engine, tr_account, tr_csv, monkeypatch):
    from pfb_app import views

    updates = []
    page = SimpleNamespace(services=[], update=lambda: updates.append(1))
    monkeypatch.setattr(views, "_page", lambda control: page)
    monkeypatch.setattr(views, "_refresh", lambda control: None)

    async def fake_pick(self, **kwargs):
        assert kwargs["allowed_extensions"] == ["csv"]
        return [SimpleNamespace(path=str(tr_csv), name=tr_csv.name, bytes=None)]

    monkeypatch.setattr(ft.FilePicker, "pick_files", fake_pick)
    view = ImportView(engine)
    view.account.value = str(tr_account.id)
    asyncio.run(view._pick(None))

    assert len(page.services) == 1  # il FilePicker è registrato sulla pagina
    assert updates  # e la pagina è stata aggiornata, così il client lo conosce
    assert any("Nuovi" in c.value for c in view.result.controls)


def test_paths_point_to_project_even_with_flet_run_cwd(tmp_path, monkeypatch):
    """`flet run` lancia l'app da src/.flet/storage/data: i percorsi non dipendono dal cwd."""
    import importlib
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PFB_DB", raising=False)
    monkeypatch.delenv("PFB_CONFIG", raising=False)
    import main

    main = importlib.reload(main)
    root = Path(__file__).resolve().parent.parent
    assert Path(main.DB_PATH) == root / "data" / "pfb.sqlite"
    assert root / "config.toml" == main.CONFIG_PATH


def test_add_view_saves_and_syncs(engine, s, bper_account, tmp_path):
    from pfb_app.views import AddView
    from pfb_core.models import Account, AccountKind, AccountSource, SheetSync
    from tests.test_sheets_sync import TAB, FakeSheet

    s.add(Account(owner_id=bper_account.owner_id, name="Contanti", kind=AccountKind.WALLET,
                  source=AccountSource.MANUALE))  # fmt: skip
    s.commit()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[google_sheet]\nfoglio = "id"\ndal = "2026-01-01"\n'
                   f'[google_sheet.schede]\nTest = "{TAB}"\n', encoding="utf-8")  # fmt: skip

    view = AddView(engine, cfg)
    assert [o.text for o in view.account.options] == ["Contanti"]
    view.date.value = "2026-09-12"
    view.amount.value = "12,40"
    view.category.value = next(o.key for o in view.category.options if "Ristorante" in o.text)
    view.description.value = "Pizza"
    view._on_save(None)
    assert view.amount.value == ""

    view._on_sync(None)  # prepara il piano (il dialogo di conferma richiede una pagina)
    assert view._plan is not None and len(view._plan.rows) == 1
    sheet = FakeSheet()
    asyncio.run(view.sync_now(client=sheet))
    assert "Scritti 1" in view.sync_status.value
    assert sheet.appended[0][1][0][:4] == ["Pizza", "🍔 Ristorante/Asporto", 12.4, "2026-09-12"]
    assert len(s.exec(select(SheetSync)).all()) == 1


def test_sync_without_config_does_nothing(engine, tmp_path):
    from pfb_app.views import AddView

    view = AddView(engine, tmp_path / "config.toml")
    view._on_sync(None)
    assert view._plan is None
