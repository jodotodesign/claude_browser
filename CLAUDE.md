# CLAUDE.md — finanz-dashboard

Guidance für Claude Code beim Arbeiten in diesem Projekt.

## Was das ist

Virtuelles Papierdepot: virtuelle Ein-/Auszahlungen, virtuelle Käufe und Verkäufe von Aktien,
ETFs, Rohstoffen (Gold) und Krypto — bewertet mit **echten historischen Marktdaten**. Kein echtes
Geld, keine Brokeranbindung, keine Anlageberatung.

## Starten

```bash
python3 server.py        # http://localhost:8777, öffnet den Browser
python3 server.py 9000   # anderer Port
./start.sh               # dasselbe
```

`web_app.html` lässt sich auch direkt per `file://` öffnen — dann läuft die App im **Demo-Modus**
mit simulierten (aber reproduzierbaren) Kursen, weil Yahoo Finance keine CORS-Header sendet, und
**ohne** Speicherung in `depot.json`. `updateBanner()` blendet dafür oben eine Warnung ein; die
Unterscheidung „kein Server“ gegen „Server mit altem Code“ läuft über `Prices.live`, **nicht** über
den Statuscode von `/api/state` — ein beliebiger Webserver (auch `python3 -m http.server`)
antwortet dort ebenfalls mit 404.

## Architektur

Zwei Dateien, keine Abhängigkeiten außer der Python-Standardbibliothek.

### `server.py` — Server + CORS-Proxy

Grund für den Proxy: Yahoo Finance liefert **kein** `Access-Control-Allow-Origin`, ein direkter
`fetch()` aus dem Browser wird blockiert. Frankfurter (EZB-Wechselkurse) erlaubt CORS, wird aber
der Einheitlichkeit halber ebenfalls durchgereicht.

| Endpunkt | Zweck |
|---|---|
| `/api/status` | Erkennung Live- vs. Demo-Modus, Stand des Sitzungsspeichers |
| `/api/snapshot` | Fortschritt des Startabrufs; mit `?full=1` alle Kurse und Wechselkurse auf einmal |
| `/api/history?symbol=&range=&interval=` | Tagesschlusskurse (adjclose), Metadaten — `range`/`interval` werden entgegengenommen, aber nicht beachtet |
| `/api/quote?symbols=A,B,C` | aktueller Kurs + Vortagesschluss |
| `/api/search?q=` | Symbolsuche (Name, Ticker, ISIN) |
| `/api/fx?base=USD&start=` | Tagesreihe Fremdwährung → EUR (`start` wird nicht beachtet) |
| `GET/POST /api/state` | Depot lesen/schreiben (`depot.json` im Projektordner) |
| `POST /api/refresh` | Startabruf noch einmal ausführen (Knopf „Kurse aktualisieren“) |

**Sitzungsspeicher** (`_session`, `session_history()`, `session_fx()`): Kurse werden **einmal beim
Start** geholt — `warmup()` läuft als Hintergrundfaden über die Symbole des Depots (aus `depot.json`,
zuerst) und den Katalog der Web-App (`catalog_symbols()` liest den `CATALOG` direkt aus
`web_app.html`, damit es keine zweite Liste zu pflegen gibt). Danach beantworten `/api/history`,
`/api/quote` und `/api/fx` **ohne Netzabruf** aus diesem Speicher; die TTL des Plattencaches gilt
dort bewusst nicht. Grund: Yahoo drosselt die ganze IP, und wer jeden Kurs erst beim Bedarf holt,
verliert genau den Abruf, auf den es ankommt — das Auswählen eines Wertpapiers in der Ordermaske.
Ein Symbol, das im Startabruf nicht dabei war (frisch gesucht), wird beim ersten Zugriff einmal
geholt und dann ebenfalls festgehalten. Neue Kurse gibt es nur über `POST /api/refresh` oder einen
Neustart; nur dabei wird mit `refresh=True` auch der Plattencache übergangen.

**Quellenkette** (`PROVIDERS`): `_yahoo_history` → `_twelvedata_history` → `_coingecko_history`.
`yahoo_history()` (Name historisch) probiert sie der Reihe nach durch und liefert notfalls
veraltete Cache-Daten. Schlüssel kommen aus `config.json` oder `FD_TWELVEDATA_KEY` (siehe
`config()`), `config.example.json` ist die Vorlage.

Eigenheiten der Zweitquellen:
- **Twelve Data**: eigene Symbolschreibweise. `_td_symbol()` übersetzt `EUNL.DE` → `EUNL` @ `XETR`
  (Tabelle `TD_EXCHANGE`) und `BTC-EUR` → `BTC/EUR`. Futures (`=F`) und Indizes (`^`) kann die
  Quelle nicht, sie wirft dafür bewusst. Die **Symbolsuche** funktioniert dort ohne Schlüssel und
  dient als Ausweichquelle für `/api/search`.
- **CoinGecko**: nur die Kürzel aus `COINS`, und der kostenlose Zugang gibt höchstens **365 Tage**
  heraus (mehr → Fehler 10012).

Drei nicht offensichtliche Punkte:

1. **TLS-Kontext** (`ssl_context()`): System-Python auf macOS hat oft keinen Zertifikatsspeicher.
   Es werden mehrere CA-Bundles durchprobiert (Systemvorgabe, `certifi`, `/etc/ssl/cert.pem`, …).
   Wichtig: `urlopen` verpackt SSL-Fehler in `URLError`, deshalb packt `is_cert_error()` die
   `.reason`-Kette aus — ein `except ssl.SSLError` allein greift nie.
2. **Sperrpause und Host-Wechsel** (`fetch_yahoo()`): `query1` und `query2` werden von Yahoo **getrennt**
   gedrosselt. Bei `429` wird auf den anderen Host ausgewichen und der zuletzt erfolgreiche
   gemerkt. Nach einer Sperre pausiert Yahoo über `_yahoo_blocked_until` komplett für
   `YAHOO_COOLDOWN` Sekunden, damit nicht jeder Abruf zwei aussichtslose Anfragen verbrennt.
   `throttle()` entzerrt Aufrufe zusätzlich auf `MIN_GAP` Sekunden — ohne das löst die Ansicht
   „Märkte“ mit ihren ~30 Symbolen die Sperre selbst aus. Beim Entwickeln sparsam abfragen: die
   Sperre gilt für die ganze IP und hält Minuten bis Stunden.
3. **Cache** (`.cache/`, gitignoriert): Speicher **und** Platte, TTL je Präfix (`TTL`-Dict).
   Schlägt ein Abruf fehl, liefert `yahoo_history()` bewusst veraltete Daten aus dem Cache statt
   eines Fehlers — bei einer Ratenbegrenzung besser als ein leeres Depot.
4. **Depotdatei** (`depot.json`, gitignoriert): `write_state()` schreibt atomar über eine
   `.tmp`-Datei und dreht den vorherigen Stand nach `depot.backup.json`. `POST /api/state` prüft
   vorher Größe und Struktur (`transactions` muss eine Liste sein) — ungültige Rümpfe werden
   abgelehnt, **ohne** die vorhandene Datei anzufassen. Ist `depot.json` beschädigt, liest
   `read_state()` die Sicherung.

### `web_app.html` — Single-File-App (Vanilla JS, ~1900 Zeilen)

**Kernprinzip:** Einzige Wahrheit ist das Buchungsjournal `state.transactions`. Bestände und
Barmittel werden **nie gespeichert**, sondern per `replay(bisDatum)` nachgespielt. Dadurch sind
rückdatierte Buchungen und das Löschen einzelner Buchungen konsistent möglich.

| Baustein | Aufgabe |
|---|---|
| `Prices` (IIFE) | Kursbeschaffung, Demo-Generator, FX, Cache, `priceAt()`/`fxAt()` mit Vorwärtsfüllung |
| `Prices.preload()` | Startabruf des Servers abwarten (Fortschritt in der Statusanzeige) und alle Kurse in einem Zug übernehmen |
| `Prices.reload()` | `POST /api/refresh` und alles Gespeicherte verwerfen — der einzige Weg zu neuen Kursen innerhalb einer Sitzung |
| `replay(upToDate)` | Journal → Barmittel, Positionen, realisiertes Ergebnis zu einem Stichtag |
| `snapshot()` | Heutige Bewertung: Marktwert, G/V, Tagesveränderung je Position |
| `valueSeries()` | Tagesreihe Gesamtwert + Nettoeinzahlung ab der ersten Buchung |
| `validate(txns)` | Journal darf zu **keinem** Zeitpunkt negative Barmittel oder Bestände ergeben |
| `lineChart` / `donut` / `sparkline` | Handgezeichnetes SVG, keine externen Bibliotheken |

Weitere nicht offensichtliche Punkte:

- **Kostenbasis** ist der gleitende Durchschnittspreis inklusive Kaufgebühren (nicht FIFO). Beim
  Verkauf mindert der anteilige Durchschnittspreis die Kostenbasis, die Differenz wandert ins
  realisierte Ergebnis.
- **Farben in SVG**: `col("var(--x)")` löst CSS-Variablen über `getComputedStyle` in echte
  Farbwerte auf. `var()` als SVG-Präsentationsattribut ist in älteren Safari-Versionen unzuverlässig.
  `clearPalette()` muss bei jedem Themawechsel laufen.
- **Betragsmodus im Orderformular**: Die Gebühr hängt vom Volumen ab, das Volumen vom Betrag
  abzüglich Gebühr — `currentOrder()` iteriert das vier Mal, das konvergiert bei allen
  Gebührenmodellen.
- **Fremdwährung**: Bewertung immer in EUR. Für historische Punkte wird der EZB-Kurs **des
  jeweiligen Tages** benutzt (`fxAt`), für die heutige Bewertung `fxNow`.
- **Kurse stehen für die Sitzung fest**: `boot()` ruft `Prices.preload()` **vor** allem anderen auf;
  danach kommen Ordermaske, Depot und Marktansicht ohne einen einzigen Kursabruf aus. Nur ein neu
  gesuchtes Symbol löst noch `Prices.history()` mit Netzabruf aus (einmalig, danach hält es der
  Server fest). Die Statusanzeige nennt darum den Zeitpunkt des Abrufs (`statusText()`) statt
  „live“, und der Knopf **Kurse aktualisieren** im Depot ruft `Prices.reload()` + `boot(true)` auf.
  Antwortet der Server nicht auf `/api/snapshot` (alte Version), gibt `preload()` `false` zurück und
  alles läuft wie zuvor über Einzelabrufe.
- **Speicher, zwei Orte**: `localStorage` (`fd_state_v1` Journal, `fd_prices_v1` Kurscache mit max.
  24 Symbolen und 1 Stunde) **und** `depot.json` über `FileStore`. `saveState()` setzt
  `state.updatedAt` und stößt einen Sammel-Timer (600 ms) an, damit mehrere Buchungen kurz
  hintereinander einen Schreibvorgang ergeben; `beforeunload` schiebt Ausstehendes per `sendBeacon`
  nach. `syncStores()` läuft in `boot()` **vor** allem anderen und entscheidet über `updatedAt`,
  welcher Stand gewinnt — ersetzt die Datei vorhandene Buchungen, meldet ein Toast das.
  Ohne Server ist `FileStore.available` false und alles läuft wie zuvor über den `localStorage`.

## Symbole

Yahoo-Ticker. Nützliche Beispiele: `EUNL.DE` (MSCI World), `VWCE.DE` (FTSE All-World),
`SXR8.DE` (S&P 500), `4GLD.DE` (Xetra-Gold), `GC=F` (Gold-Future), `SI=F` (Silber), `BTC-EUR`,
`^GDAXI` (DAX), `AAPL`, `SAP.DE`. Die Suche akzeptiert auch ISINs (`IE00B4L5Y983`).

Der Katalog in `CATALOG` (web_app.html) steuert nur die Ansicht „Märkte“ und die Vorschläge —
gekauft werden kann jedes Symbol, das Yahoo kennt.

## Prüfliste nach Änderungen

1. `python3 server.py` starten, `http://localhost:8777` öffnen → Statusanzeige zählt „lade Kurse (n/28)“
   hoch und steht danach auf „Kurse vom … · Sitzung“
2. Einstellungen → „Beispiel-Depot laden“ → Übersicht zeigt Kurve über drei Jahre mit echten Kursen
3. Kasse: Einzahlung buchen → Barmittel steigen; Auszahlung über den Bestand hinaus wird abgelehnt
4. Handeln: Symbol suchen, Datum in der Vergangenheit setzen → Kurs füllt sich automatisch
5. Order über die verfügbaren Barmittel hinaus → Vorschau warnt, Buchung wird abgelehnt
6. Depot: Position verkaufen → realisiertes Ergebnis erscheint in der Übersicht
7. Verlauf: Buchung löschen, die eine spätere unmöglich machen würde → wird abgelehnt
8. Märkte: alle Zeilen bekommen Kurs, Veränderungen und Sparkline
9. CSV- und JSON-Export öffnen/wieder einspielen
9b. Speicherung: Buchung anlegen → `depot.json` enthält sie; `localStorage.clear()` + Neuladen →
   Depot kommt aus der Datei zurück; Datei mit höherem `updatedAt` schreiben → Toast meldet die
   Übernahme; ohne Server geöffnet → Hinweis nennt nur den `localStorage`
9c. Sitzungsspeicher: in der Ordermaske nacheinander mehrere Symbole auswählen und die Ansicht
   „Märkte“ öffnen → im Serverprotokoll erscheint **kein** neuer Abruf, nur `/api/…`-Zeilen des
   Browsers; „Kurse aktualisieren“ im Depot lädt sichtbar neu und setzt den Zeitstempel oben hoch
10. Hell/Dunkel umschalten → Diagrammfarben wechseln mit
11. `web_app.html` per `file://` öffnen → Demo-Modus, alles bedienbar

## Grenzen

- Keine Dividenden als Kassenzufluss (Kurse sind aber um Ausschüttungen bereinigt, `adjclose`).
- Keine Steuern, kein Spread, keine Teilausführung; Order werden zum Tagesschlusskurs gebucht.
- Yahoo-Kurse sind verzögert und ohne Gewähr.
- Kurse werden innerhalb einer Sitzung **nicht** nachgeführt: gezeigt wird der Stand des Startabrufs,
  bis „Kurse aktualisieren“ gedrückt oder der Server neu gestartet wird.
