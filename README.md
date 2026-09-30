# Personal Finance Bro

App personale per cashflow e investimenti, alimentata dagli export di **Trade Republic** (CSV) e **BPER** (XLS).
Tutti i dati restano in locale: un file SQLite sul Raspberry Pi o sul telefono, niente cloud.

## Struttura

```
src/pfb_core/   logica pura (schema, parser, regole, import) — nessuna dipendenza dalla UI
src/pfb_app/    schermate Flet
src/main.py     punto d'ingresso Flet (web sul Raspberry, desktop, APK Android)
tests/          test pytest; fixture solo anonimizzate
data/           database e export reali — ignorata da git
```

## Avvio rapido

```bash
uv sync --group dev
cp config.example.toml config.toml     # inserisci i tuoi IBAN
uv run python -m pfb_core init          # crea data/pfb.sqlite con categorie, regole, strumenti e conti
uv run pytest

# import da riga di comando
uv run python -m pfb_core import "Trade Republic" ~/Downloads/Esportazione_operazioni.csv
uv run python -m pfb_core import BPER ~/Downloads/Lista_Movimenti.xls
uv run python -m pfb_core review        # movimenti ancora da categorizzare

# interfaccia
uv run --extra app flet run src/main.py            # desktop
uv run --extra web flet run --web src/main.py      # web, es. sul Raspberry Pi
uv run --extra app flet build apk                  # APK Android
```

## Categorizzazione

Ogni movimento importato passa per un motore di regole (`src/pfb_core/rules.py`), valutate in ordine
di priorità: IBAN dei propri conti (giroconti) → correzioni fatte in app → tipo operazione →
regole personali di `config.toml` → categoria della banca → esercenti noti → codice MCC.
Quello che resta finisce nella schermata **Da rivedere**: scegliendo una categoria l'app crea
una regola sulla controparte e la applica subito agli altri movimenti uguali.

## Privacy

Export bancari, database e `config.toml` sono esclusi da git (vedi `.gitignore`).
Per i test si usano solo file anonimizzati in `tests/fixtures/`.

## Roadmap

| Fase | Contenuto |
| --- | --- |
| 0 · Setup | Schema SQLite, tassonomia categorie, prova build APK Flet |
| 1 · Import e regole | Parser Trade Republic e BPER, dedup, motore di regole, coda di revisione |
| 2 · Dashboard cashflow | Entrate/uscite, savings rate, budget, categorie |
| 3 · Patrimonio | Trade dal CSV, prezzi yfinance, snapshot mensili, XIRR |
| 4 · Storico e Raspberry | Migrazione dai fogli Google, web app sul Raspberry Pi |
| 5 · App nativa | APK Android con database sul telefono |
| 6 · Assistente | Chat con LLM locale (Ollama) sui propri dati |
