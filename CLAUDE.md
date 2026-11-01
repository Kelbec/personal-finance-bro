# Personal Finance Bro — note per Claude Code

App personale (Python) per cashflow e investimenti da export Trade Republic (CSV) e BPER (XLS).
Dati solo in locale: SQLite, niente cloud. Unica eccezione, opzionale e su richiesta: la
sincronizzazione verso il foglio Google condiviso (`sheets_sync`, sezione `[google_sheet]`). Lingua del progetto: italiano (UI, commit, commenti).

## Comandi
- `uv sync --group dev --extra app` — installa tutto
- `uv run pytest -q` · `uv run ruff check .` · `uv run ruff format .`
- `uv run python -m pfb_core init|import <conto> <file>|review`
- `uv run --extra app flet run src/main.py` — app desktop; `flet build apk` per Android

## Struttura
- `src/pfb_core/` logica pura, senza dipendenze da Flet (models, importers/, rules, importer, seed,
  metrics, prices, portfolio, history, sheets_sync, backup)
- `src/pfb_app/` schermate Flet; `src/main.py` entry point (`[tool.flet.app] path = "src"`)
- `tests/` pytest; `tests/conftest.py` genera un .xls BPER finto con xlwt

## Regole
- Importi sempre `Decimal`, mai float (eccezione: XIRR e rapporti per la UI).
- `Snapshot.is_manual=True` = valore fornito (a mano o da export): ancora, mai ricalcolato.
- Trade `iniziale` alla data D = posizione completa a D: i trade precedenti dello stesso
  strumento/conto ci sono già dentro (`_trades_until` li esclude). Costo con commissioni.
- Mai committare dati personali: export, `*.sqlite`, `config.toml`, IBAN, nomi, codice fiscale.
  Le fixture in `tests/fixtures/` vanno anonimizzate.
- Un branch per fase (`fase-N-...`) con PR verso `main`; CI = ruff + pytest.
- Flet ≥ 1.0: `ft.run(main)`, `page.show_dialog(ft.SnackBar(...))`. `FilePicker` è un servizio:
  `page.services.append(picker)` + `page.update()` prima di `await picker.pick_files(...)`,
  altrimenti il pulsante non fa nulla (helper `_file_picker` / `_pick_one` in views.py).
- APK: le dipendenze sono `[project].dependencies` + `[tool.flet.android].dependencies` (flet e
  certifi vanno dichiarati lì: `flet build` aggiunge flet solo se il progetto non ha dipendenze, e
  lo script di avvio importa certifi). Devono avere wheel Android o essere pure Python. yfinance/gspread restano extra: sul telefono i prezzi usano
  `chart_provider` (solo stdlib); gspread invece è nelle dipendenze Android, con config.toml e chiave importati dalla schermata Dati (`setup_files`). Su Android il filtro per estensione del picker è disattivato.
  `[tool.flet.flutter.pubspec.dependency_overrides] jni_flutter = "1.0.1"` serve finché il template
  di Flet fissa jni 1.0.0. Gli avvisi Kotlin "Could not close incremental caches" non bloccano.

## Roadmap
0 setup ✅ · 1 import e regole ✅ · 2 dashboard cashflow ✅ · 3 patrimonio ✅ (trade, prezzi
yfinance, snapshot, XIRR) · 4 storico dai fogli ✅ (branch `fase-4-storico`) + Raspberry · 5 APK Android ✅ (branch `fase-5-apk`, schermata Dati per copiare il DB) ·
6 assistente con Ollama locale (notebook MSI, RTX 2060 6 GB → modelli 7–8B a 4 bit).
