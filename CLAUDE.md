# Personal Finance Bro — note per Claude Code

App personale (Python) per cashflow e investimenti da export Trade Republic (CSV) e BPER (XLS).
Dati solo in locale: SQLite, niente cloud. Lingua del progetto: italiano (UI, commit, commenti).

## Comandi
- `uv sync --group dev --extra app` — installa tutto
- `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format .`
- `uv run python -m pfb_core init|import <conto> <file>|review`
- `uv run --extra app flet run src/main.py` — app desktop; `flet build apk` per Android

## Struttura
- `src/pfb_core/` logica pura, senza dipendenze da Flet (models, importers/, rules, importer, seed,
  metrics, prices, portfolio)
- `src/pfb_app/` schermate Flet; `src/main.py` entry point (`[tool.flet.app] path = "src"`)
- `tests/` pytest; `tests/conftest.py` genera un .xls BPER finto con xlwt

## Regole
- Importi sempre `Decimal`, mai float (eccezione: XIRR e rapporti per la UI).
- `Snapshot.is_manual=True` = valore fornito (a mano o da export): ancora, mai ricalcolato.
- Mai committare dati personali: export, `*.sqlite`, `config.toml`, IBAN, nomi, codice fiscale.
  Le fixture in `tests/fixtures/` vanno anonimizzate.
- Un branch per fase (`fase-N-...`) con PR verso `main`; CI = ruff + pytest.
- Flet ≥ 1.0: `ft.run(main)`, `await ft.FilePicker().pick_files(...)`, `page.show_dialog(ft.SnackBar(...))`.

## Roadmap
0 setup ✅ · 1 import e regole ✅ · 2 dashboard cashflow ✅ · 3 patrimonio ✅ (branch `fase-3-patrimonio`:
trade, prezzi yfinance, snapshot, XIRR) · 4 storico dai fogli + Raspberry · 5 APK Android ·
6 assistente con Ollama locale (notebook MSI, RTX 2060 6 GB → modelli 7–8B a 4 bit).
