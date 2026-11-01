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

## Patrimonio

```bash
uv sync --extra prices                                   # yfinance, per i prezzi
# una tantum: quote già possedute prima del primo export, e saldi di partenza
uv run python -m pfb_core position "Trade Republic" IE00BK5BQT80 120.5 98.40 --data 2026-08-31
uv run python -m pfb_core balance "Trade Republic" 2026-08-31 1250.00
uv run python -m pfb_core balance PayPal 2026-09-30 12.00
# ogni mese, dopo l'import
uv run python -m pfb_core prices        # chiusure giornaliere da Yahoo Finance
uv run python -m pfb_core snapshot      # valori di fine mese
uv run python -m pfb_core portfolio     # patrimonio, P&L, rendimento, allocazione
```

Quote = trade importati + posizioni di partenza; valore = quote × ultima chiusura.
Una posizione di partenza alla data D è tutto ciò che si possedeva quel giorno: i trade
precedenti dello stesso strumento sono già compresi e non si sommano. Il prezzo medio è il
costo medio di tutti gli acquisti, PAC e commissioni inclusi; le vendite non lo cambiano.

Per avere il prezzo medio vero invece di quello stimato, importa da Trade Republic la storia
completa (tutte le operazioni dall'apertura del conto), poi:

```bash
uv run python -m pfb_core riconcilia --prova   # confronta posizioni stimate e trade
uv run python -m pfb_core riconcilia           # toglie le stime che i trade ricostruiscono
```
La liquidità di ogni conto parte dall'ultimo saldo noto (inserito a mano o letto dall'export
BPER) più i movimenti successivi. Il rendimento è un XIRR calcolato dal giorno delle posizioni
di partenza (o dal primo acquisto, se non ce ne sono); sotto l'anno viene mostrato cumulato,
non annualizzato. I simboli Yahoo si correggono in `config.toml` (`[[instruments]]`).

## Storico dai fogli Google

Una tantum, con i fogli esportati in `.xlsx` (File → Scarica → Microsoft Excel):

```bash
# movimenti: fogli con colonne Causale | Categoria | Importo | Data | Mese | Intestatario
uv run python -m pfb_core storico-cashflow CashFlow.xlsx
# patrimonio: fogli anno ("2024", "2025", ...) con i mesi in colonna e le sezioni CASH/BONDS/STOCKS
uv run python -m pfb_core storico-patrimonio Asset_allocation.xlsx
```

- I movimenti finiscono su un conto "Storico <intestatario>" (fuori dal calcolo della
  liquidità), già categorizzati: il segno viene dal gruppo della categoria. Se lo stesso
  movimento è in più fogli (es. totale e foglio personale) viene importato una volta sola.
- Ogni valore mensile del patrimonio diventa uno snapshot di fine mese; i saldi dei conti
  fanno da punto di partenza per la liquidità. Un conto che sparisce dal foglio di un anno
  viene azzerato da quell'anno.
- Dall'ultimo mese del foglio nascono le posizioni di partenza: quote = valore ÷ chiusura
  Yahoo del giorno, costo di carico = quella chiusura (il P&L parte da lì).
- Da lì in poi si aggiorna mese per mese con gli export di Trade Republic e BPER.

## Foglio Google condiviso

Facoltativo: l'app può aggiungere i movimenti al foglio Google che si usa già in coppia
(solo app → foglio). Si attiva con la sezione `[google_sheet]` di `config.toml`; senza,
nessun dato lascia il computer.

1. [Google Cloud Console](https://console.cloud.google.com/): nuovo progetto → abilita
   **Google Sheets API** → *Credenziali* → *Account di servizio* → *Chiavi* → *JSON*.
2. Salva la chiave come `google-credentials.json` nella cartella del progetto (è in `.gitignore`).
3. Condividi il foglio con l'email dell'account di servizio (`...@...iam.gserviceaccount.com`)
   come **Editor**.
4. Compila `[google_sheet]` in `config.toml` (vedi `config.example.toml`) e installa
   `uv sync --extra sheets`.

```bash
uv run python -m pfb_core sync-sheet --prova   # mostra cosa verrebbe scritto
uv run python -m pfb_core sync-sheet           # scrive
```

Nell'app: schermata **Aggiungi** → *Sincronizza Google Sheet*, con conferma prima dell'invio.
Vanno sul foglio i movimenti categorizzati dalla data `dal` in poi (giroconti e storico
esclusi), ognuno una volta sola, nel formato Causale | Categoria | Importo | Data | Mese |
Intestatario. Un movimento già presente nella scheda (stessa data, importo e categoria),
ad esempio perché scritto anche a mano, non viene riscritto. Una categoria cambiata dopo
l'invio non viene aggiornata sul foglio.

Nella stessa schermata si inseriscono a mano le spese dei conti senza export (contanti,
PayPal, Satispay); quelle bancarie arrivano dall'import, per non contarle due volte.

## Categorizzazione

Ogni movimento importato passa per un motore di regole (`src/pfb_core/rules.py`), valutate in ordine
di priorità: IBAN dei propri conti (giroconti) → correzioni fatte in app → tipo operazione →
regole personali di `config.toml` → categoria della banca → esercenti noti → codice MCC.
Quello che resta finisce nella schermata **Da rivedere**: scegliendo una categoria l'app crea
una regola sulla controparte e la applica subito agli altri movimenti uguali.

## Privacy

## Sul telefono (APK Android)

```bash
uv run --extra app flet build apk     # la prima volta scarica Flutter, JDK e Android SDK
# risultato: build/apk/ — installa sul telefono l'APK arm64-v8a
```

Il telefono tiene un suo database. Dalla schermata **Dati** (icona in alto a destra):

- **Importa database**: scegli il `data/pfb.sqlite` copiato dal PC; quello del telefono viene
  salvato come copia accanto prima di essere sostituito.
- **Esporta database**: dopo un import fatto sul telefono, salva il file e mettilo al posto di
  `data/pfb.sqlite` sul PC (con l'app del PC chiusa).

Sul telefono funzionano import degli export, revisione, dashboard, patrimonio e prezzi
(senza yfinance: l'app usa direttamente l'endpoint pubblico di Yahoo). La sincronizzazione
col foglio Google resta sul PC, perché richiede `config.toml` e la chiave dell'account di servizio.

Export bancari, database e `config.toml` sono esclusi da git (vedi `.gitignore`).
Per i test si usano solo file anonimizzati in `tests/fixtures/`.

### Release automatica

Un tag `vX.Y.Z` fa partire il workflow `Release APK`: controlla che il tag coincida con la
versione in `pyproject.toml` e `src/pfb_core/__init__.py`, esegue ruff e pytest, compila gli APK
e li allega a una release GitHub (da scaricare sul telefono).

```bash
# alza la versione in pyproject.toml e src/pfb_core/__init__.py, poi:
git commit -am "Versione 0.3.0" && git push
git tag v0.3.0 && git push origin v0.3.0
```

Per installare ogni APK sopra il precedente, senza disinstallare, serve una chiave di firma
fissa. Si crea una volta sola e si salva nei secret del repository:

```bash
keytool -genkeypair -v -keystore release.jks -alias pfb -keyalg RSA -keysize 2048 -validity 10000
base64 -w0 release.jks     # → secret ANDROID_KEYSTORE_BASE64
# altri secret: ANDROID_KEYSTORE_PASSWORD (e, se l'alias non è "pfb", ANDROID_KEY_ALIAS)
```

Conserva `release.jks` fuori da git: se lo perdi, gli aggiornamenti richiedono di disinstallare l'app.

## Roadmap

| Fase | Contenuto |
| --- | --- |
| 0 · Setup | Schema SQLite, tassonomia categorie, prova build APK Flet |
| 1 · Import e regole | Parser Trade Republic e BPER, dedup, motore di regole, coda di revisione |
| 2 · Dashboard cashflow | Entrate/uscite, savings rate, budget, categorie |
| 3 · Patrimonio | Trade dal CSV, prezzi yfinance, snapshot mensili, XIRR |
| 4 · Storico e Raspberry | Migrazione dai fogli Google ✅, web app sul Raspberry Pi |
| 5 · App nativa | APK Android con database sul telefono, copia da/verso il PC ✅ |
| 6 · Assistente | Chat con LLM locale (Ollama) sui propri dati |
