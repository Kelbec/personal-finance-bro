# Personal Finance Bro

App personale per cashflow e investimenti, alimentata dagli export di **Trade Republic** (CSV) e **BPER** (XLS).
Tutti i dati restano in locale: un file SQLite sul Raspberry Pi o sul telefono, niente cloud.

## Struttura

```
src/pfb_core/   logica pura (schema, import, regole, metriche) — nessuna dipendenza dalla UI
app/            interfaccia Flet (web sul Raspberry, poi APK Android)
tests/          test pytest; fixture solo anonimizzate
data/           database e export reali — ignorata da git
```

## Avvio rapido

```bash
uv sync --group dev
cp config.example.toml config.toml     # inserisci i tuoi IBAN
uv run python -m pfb_core init          # crea data/pfb.sqlite con categorie, strumenti e conti
uv run pytest
uv run --extra app flet run app/main.py
```

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
