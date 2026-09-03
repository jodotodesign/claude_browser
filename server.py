#!/usr/bin/env python3
"""
Lokaler Server + CORS-Proxy fuer das virtuelle Finanzdashboard.

Warum ein Proxy noetig ist: Yahoo Finance liefert keine
Access-Control-Allow-Origin-Header, ein direkter fetch() aus dem Browser
wird daher blockiert. Dieser Server holt die Kursdaten serverseitig und
reicht sie unter /api/... an die Web-App weiter.

Kursdaten werden **einmal beim Start** geholt und danach fuer die ganze
Sitzung unveraendert weitergereicht (Sitzungsspeicher, siehe unten). Wer
mitten im Handeln neue Kurse will, drueckt im Depot "Kurse aktualisieren"
(POST /api/refresh).

Nur Python-Standardbibliothek, keine Installation noetig.

    python3 server.py            # startet auf http://localhost:8777
    python3 server.py 9000       # anderer Port
"""

import http.server
import json
import os
import re
import socketserver
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8777

# Beide Yahoo-Hosts werden getrennt gedrosselt – bei 429 auf den anderen ausweichen
YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
FRANKFURTER = "https://api.frankfurter.dev/v1"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Gueltigkeitsdauer des Plattencaches in Sekunden je Schluesselpraefix.
# Fuer die laufende Sitzung spielt sie keine Rolle – dort gilt der
# Sitzungsspeicher weiter unten; sie greift beim naechsten Start.
TTL = {"hist": 900, "search": 604800, "fx": 3600}

CACHE_DIR = os.path.join(BASE_DIR, ".cache")

_cache = {}
_cache_lock = threading.Lock()


def _cache_file(key):
    import hashlib
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def cache_get(key, allow_stale=False):
    """Frischen Wert liefern; mit allow_stale auch einen abgelaufenen."""
    with _cache_lock:
        entry = _cache.get(key)
    if entry:
        stored, value = entry
        if allow_stale or time.time() - stored < entry_ttl(key):
            return value

    try:                                        # Plattencache (ueberlebt Neustarts)
        with open(_cache_file(key), "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if allow_stale or time.time() - blob["t"] < entry_ttl(key):
            with _cache_lock:
                _cache[key] = (blob["t"], blob["v"])
            return blob["v"]
    except Exception:
        pass
    return None


def entry_ttl(key):
    return TTL.get(key.split(":", 1)[0], 600)


def cache_put(key, value):
    now = time.time()
    with _cache_lock:
        _cache[key] = (now, value)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _cache_file(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"t": now, "v": value}, fh)
        os.replace(tmp, _cache_file(key))
    except Exception:
        pass                                    # Cache ist Beiwerk, nie kritisch


# --------------------------------------------------------- TLS-Kontext
# Das von python.org gelieferte Python bringt auf macOS haeufig keinen
# Zertifikatsspeicher mit ("CERTIFICATE_VERIFY_FAILED"). Wir suchen daher
# beim ersten Zugriff ein brauchbares CA-Bundle.
_ssl_ctx = None


def ssl_context():
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx

    candidates = [None]                       # Standardeinstellung des Systems
    try:
        import certifi
        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates += [
        "/etc/ssl/cert.pem",                              # macOS / LibreSSL
        "/usr/local/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/ca-certificates/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",             # Linux
    ]

    def is_cert_error(exc):
        # urlopen verpackt SSL-Fehler in URLError -> Ursache auspacken
        while exc is not None:
            if isinstance(exc, ssl.SSLError):
                return True
            exc = getattr(exc, "reason", None) if not isinstance(exc, str) else None
        return False

    for ca in candidates:
        if ca and not os.path.exists(ca):
            continue
        try:
            ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        except Exception:
            continue
        try:
            req = urllib.request.Request(YAHOO_HOSTS[0] + "/v8/finance/chart/AAPL"
                                         "?range=1d&interval=1d", headers={"User-Agent": UA})
            urllib.request.urlopen(req, timeout=12, context=ctx).read(64)
        except urllib.error.HTTPError:
            pass                      # TLS stand, nur die Antwort war ein Fehlercode
        except Exception as exc:
            if is_cert_error(exc):
                continue              # naechstes CA-Bundle probieren
            # Netzwerkfehler sagt nichts ueber das Zertifikat aus
        _ssl_ctx = ctx
        if ca:
            print("  Zertifikatsspeicher: %s" % ca)
        return ctx

    print("  Hinweis: Kein gueltiger Zertifikatsspeicher gefunden.\n"
          "  Abhilfe: pip3 install certifi  (oder unter macOS das Skript\n"
          "  'Install Certificates.command' im Python-Programmordner ausfuehren).")
    _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


_opener = None
_opener_lock = threading.Lock()


def opener():
    """Ein Opener mit Cookie-Speicher – Yahoo drosselt Aufrufe ohne Cookies."""
    global _opener
    with _opener_lock:
        if _opener is None:
            import http.cookiejar
            _opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context()),
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        return _opener


def fetch_json(url, timeout=15, tries=2):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    last = None
    for attempt in range(tries):
        try:
            with opener().open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503):
                raise
            time.sleep(0.8 * (attempt + 1))       # kurz abwarten und erneut versuchen
    raise last


# Zuletzt erfolgreicher Yahoo-Host wird bevorzugt, damit nicht jeder
# Aufruf erneut in den gedrosselten Host laeuft.
_yahoo_host = [0]

# Yahoo sperrt bei Stossbetrieb die ganze IP fuer Minuten. Die Marktansicht
# fragt knapp 30 Symbole ab, deshalb werden Aufrufe zeitlich entzerrt.
MIN_GAP = 0.35
_throttle_lock = threading.Lock()
_last_call = [0.0]


def throttle():
    with _throttle_lock:
        wait = _last_call[0] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


# Nach einer Sperre pausiert Yahoo eine Weile komplett, damit nicht jeder
# Abruf zwei aussichtslose Anfragen verbrennt.
YAHOO_COOLDOWN = 180
_yahoo_blocked_until = [0.0]


def fetch_yahoo(path, params):
    if time.time() < _yahoo_blocked_until[0]:
        raise ValueError("Yahoo drosselt gerade (noch %d s)"
                         % (_yahoo_blocked_until[0] - time.time()))
    qs = urllib.parse.urlencode(params)
    last = None
    throttle()
    order = [_yahoo_host[0], 1 - _yahoo_host[0]]
    for idx in order:
        try:
            data = fetch_json(YAHOO_HOSTS[idx] + path + "?" + qs)
            _yahoo_host[0] = idx
            _yahoo_blocked_until[0] = 0.0
            return data
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code != 429:
                raise
    _yahoo_blocked_until[0] = time.time() + YAHOO_COOLDOWN
    raise last


# ================================================================ Quellen
# Reihenfolge: Yahoo (ohne Schluessel, breiteste Abdeckung) -> Twelve Data
# (kostenloser Schluessel, 800 Abrufe/Tag) -> CoinGecko (ohne Schluessel,
# nur Krypto). Faellt eine Quelle aus (z. B. Ratenbegrenzung), uebernimmt
# die naechste.

def config():
    """Schluessel aus config.json oder Umgebungsvariablen."""
    cfg = {}
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        pass
    for name in ("twelvedata_key", "alphavantage_key"):
        env = os.environ.get("FD_" + name.upper())
        if env:
            cfg[name] = env
    return cfg


# -------------------------------------------------------------- Yahoo

def _yahoo_history(symbol, rng, interval):
    raw = fetch_yahoo("/v8/finance/chart/" + urllib.parse.quote(symbol), {
        "range": rng, "interval": interval,
        "includePrePost": "false", "events": "div,split",
    })

    result = (raw.get("chart") or {}).get("result") or []
    if not result:
        err = (raw.get("chart") or {}).get("error") or {}
        raise ValueError(err.get("description") or "Keine Daten fuer %s" % symbol)

    res = result[0]
    meta = res.get("meta") or {}
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    adj = (((res.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose")

    points, last = [], None
    for i, ts in enumerate(stamps):
        close = None
        if adj and i < len(adj) and adj[i] is not None:
            close = adj[i]
        elif i < len(closes) and closes[i] is not None:
            close = closes[i]
        if close is None:
            close = last          # Luecken mit letztem Kurs fuellen
        if close is None:
            continue
        last = close
        points.append([time.strftime("%Y-%m-%d", time.gmtime(ts)), round(float(close), 6)])

    return {
        "symbol": meta.get("symbol") or symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": (meta.get("currency") or "EUR").upper(),
        "type": (meta.get("instrumentType") or "").upper(),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        "price": meta.get("regularMarketPrice"),
        "previousClose": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "points": points,
        "source": "Yahoo Finance",
    }


# --------------------------------------------------------- Twelve Data
# Yahoo-Ticker in Twelve-Data-Notation uebersetzen: EUNL.DE -> EUNL @ XETR
TD_EXCHANGE = {
    "DE": "XETR", "F": "XFRA", "AS": "XAMS", "PA": "XPAR", "MI": "XMIL",
    "MC": "BME", "SW": "XSWX", "L": "XLON", "VI": "XWBO", "BR": "XBRU",
    "LS": "XLIS", "HE": "XHEL", "ST": "XSTO", "CO": "XCSE", "OL": "XOSL",
    "TO": "XTSE", "HK": "XHKG", "T": "XTKS", "AX": "XASX",
}


def _td_symbol(symbol):
    if symbol.startswith("^") or symbol.endswith("=F") or symbol.endswith("=X"):
        raise ValueError("Twelve Data kennt dieses Symbol nicht")
    if "-" in symbol:                       # BTC-EUR -> BTC/EUR
        base, _, quote = symbol.partition("-")
        return base + "/" + quote, None
    if "." in symbol:
        base, _, suffix = symbol.rpartition(".")
        mic = TD_EXCHANGE.get(suffix.upper())
        if not mic:
            raise ValueError("Boerse %s nicht zugeordnet" % suffix)
        return base, mic
    return symbol, None


def _twelvedata_history(symbol, rng, interval):
    key = config().get("twelvedata_key")
    if not key:
        raise ValueError("Kein Twelve-Data-Schluessel hinterlegt")
    td_sym, mic = _td_symbol(symbol)
    params = {"symbol": td_sym, "interval": "1day", "outputsize": 5000,
              "order": "ASC", "apikey": key}
    if mic:
        params["mic_code"] = mic
    throttle()
    raw = fetch_json("https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params))
    if str(raw.get("status")) == "error" or "values" not in raw:
        raise ValueError(raw.get("message") or "Twelve Data lieferte keine Daten")

    meta = raw.get("meta") or {}
    points = []
    for v in raw["values"]:
        try:
            points.append([v["datetime"][:10], round(float(v["close"]), 6)])
        except (KeyError, TypeError, ValueError):
            continue
    points.sort()
    if not points:
        raise ValueError("Twelve Data lieferte keine Kurse")

    return {
        "symbol": symbol,
        "name": meta.get("symbol") or symbol,
        "currency": (meta.get("currency") or "EUR").upper(),
        "type": (meta.get("type") or "").upper(),
        "exchange": meta.get("exchange") or "",
        "price": points[-1][1],
        "previousClose": points[-2][1] if len(points) > 1 else None,
        "points": points,
        "source": "Twelve Data",
    }


# ----------------------------------------------------------- CoinGecko
COINS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "LTC": "litecoin", "DOT": "polkadot",
    "LINK": "chainlink", "BNB": "binancecoin", "AVAX": "avalanche-2",
    "TRX": "tron", "XMR": "monero", "MATIC": "matic-network",
    "USDT": "tether", "USDC": "usd-coin",
}


def _coingecko_history(symbol, rng, interval):
    base, _, quote = symbol.partition("-")
    coin = COINS.get(base.upper())
    if not coin or quote.upper() not in ("EUR", "USD", "CHF", "GBP"):
        raise ValueError("CoinGecko kennt %s nicht" % symbol)
    throttle()
    # Der kostenlose Zugang gibt hoechstens 365 Tage heraus (Fehler 10012).
    raw = fetch_json("https://api.coingecko.com/api/v3/coins/%s/market_chart?%s" % (
        coin, urllib.parse.urlencode({"vs_currency": quote.lower(), "days": 365})))

    dedup = {}
    for ms, price in raw.get("prices") or []:
        dedup[time.strftime("%Y-%m-%d", time.gmtime(ms / 1000.0))] = round(float(price), 6)
    points = sorted(dedup.items())
    if not points:
        raise ValueError("CoinGecko lieferte keine Kurse")
    points = [[d, p] for d, p in points]

    return {
        "symbol": symbol, "name": coin.replace("-", " ").title(),
        "currency": quote.upper(), "type": "CRYPTOCURRENCY", "exchange": "CoinGecko",
        "price": points[-1][1],
        "previousClose": points[-2][1] if len(points) > 1 else None,
        "points": points,
        "source": "CoinGecko",
    }


PROVIDERS = [("yahoo", _yahoo_history),
             ("twelvedata", _twelvedata_history),
             ("coingecko", _coingecko_history)]


def yahoo_history(symbol, rng="10y", interval="1d", refresh=False):
    """Kursreihe eines Symbols – probiert alle Quellen der Reihe nach.

    Mit refresh=True wird der Plattencache uebergangen (nur beim
    ausdruecklichen Neuladen ueber /api/refresh).
    """
    key = "hist:%s:%s:%s" % (symbol, rng, interval)
    hit = None if refresh else cache_get(key)
    if hit:
        return hit

    errors = []
    for name, fn in PROVIDERS:
        try:
            out = fn(symbol, rng, interval)
        except Exception as exc:
            errors.append("%s: %s" % (name, exc))
            continue
        # Doppelte Tage zusammenfassen (Intraday-Stempel des laufenden Tages)
        dedup = {}
        for day, close in out["points"]:
            dedup[day] = close
        out["points"] = [[d, c] for d, c in sorted(dedup.items())]
        if not out["points"]:
            errors.append("%s: leere Kursreihe" % name)
            continue
        cache_put(key, out)
        return out

    stale = cache_get(key, allow_stale=True)
    if stale:                        # lieber aeltere Kurse als gar keine
        return dict(stale, stale=True)
    raise ValueError(" | ".join(errors) or "Keine Quelle lieferte Daten")


# Yahoo-Suffix je Boersenplatz, um Twelve-Data-Treffer auf unsere
# Symbolschreibweise zurueckzuuebersetzen
MIC_SUFFIX = dict((v, k) for k, v in TD_EXCHANGE.items())


def _twelvedata_search(query):
    """Symbolsuche bei Twelve Data – funktioniert auch ohne Schluessel."""
    throttle()
    raw = fetch_json("https://api.twelvedata.com/symbol_search?" +
                     urllib.parse.urlencode({"symbol": query, "outputsize": 12}))
    items, seen = [], set()
    for q in raw.get("data") or []:
        sym, mic = q.get("symbol"), q.get("mic_code")
        if not sym:
            continue
        suffix = MIC_SUFFIX.get(mic)
        full = sym + ("." + suffix if suffix else "")
        if full in seen:
            continue
        seen.add(full)
        items.append({
            "symbol": full,
            "name": q.get("instrument_name") or full,
            "type": (q.get("instrument_type") or "").upper(),
            "exchange": q.get("exchange") or "",
        })
    return items


def yahoo_search(query):
    key = "search:%s" % query.lower()
    hit = cache_get(key)
    if hit:
        return hit
    try:
        raw = fetch_yahoo("/v1/finance/search", {
            "q": query, "quotesCount": 12, "newsCount": 0,
            "enableFuzzyQuery": "false",
        })
    except Exception:
        out = {"results": _twelvedata_search(query)}
        cache_put(key, out)
        return out
    items = []
    for q in raw.get("quotes", []):
        sym = q.get("symbol")
        if not sym:
            continue
        items.append({
            "symbol": sym,
            "name": q.get("longname") or q.get("shortname") or sym,
            "type": (q.get("quoteType") or "").upper(),
            "exchange": q.get("exchDisp") or "",
        })
    out = {"results": items}
    cache_put(key, out)
    return out


# ------------------------------------------------------------ Waehrung

def fx_series(base, start, refresh=False):
    """Tageskurse base -> EUR ab start (YYYY-MM-DD), inkl. aktuellem Kurs."""
    base = base.upper()
    if base == "EUR":
        return {"base": "EUR", "latest": 1.0, "rates": {}}

    key = "fx:%s:%s" % (base, start)
    hit = None if refresh else cache_get(key)
    if hit:
        return hit

    try:
        start_d = date.fromisoformat(start)
    except Exception:
        start_d = date.today() - timedelta(days=365 * 5)
    start_d = max(start_d - timedelta(days=7), date(1999, 1, 4))

    url = "%s/%s..?%s" % (FRANKFURTER, start_d.isoformat(),
                          urllib.parse.urlencode({"base": base, "symbols": "EUR"}))
    raw = fetch_json(url)
    rates = {}
    for day, vals in (raw.get("rates") or {}).items():
        if "EUR" in vals:
            rates[day] = vals["EUR"]
    latest = rates[max(rates)] if rates else None
    out = {"base": base, "latest": latest, "rates": rates}
    cache_put(key, out)
    return out


# ---------------------------------------------------------- Depotdatei
# Das Depot lebt zusaetzlich zum localStorage als Datei im Projektordner,
# damit ein geleerter Browser-Cache es nicht mitnimmt.
STATE_FILE = os.path.join(BASE_DIR, "depot.json")
BACKUP_FILE = os.path.join(BASE_DIR, "depot.backup.json")
MAX_STATE_BYTES = 8 * 1024 * 1024

_state_lock = threading.Lock()


def read_state():
    with _state_lock:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            # Beschaedigte Datei nicht verschweigen, aber auch nicht loeschen
            print("  Depotdatei unlesbar (%s) – es wird die Sicherung angeboten." % exc)
            try:
                with open(BACKUP_FILE, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return None


def write_state(data):
    """Atomar schreiben, vorherigen Stand als Sicherung behalten."""
    with _state_lock:
        if os.path.exists(STATE_FILE):
            try:
                os.replace(STATE_FILE, BACKUP_FILE)
            except Exception:
                pass
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)


# ------------------------------------------------------ Sitzungsspeicher
# Kursdaten werden **einmal beim Start** geholt und danach unveraendert
# weitergereicht, bis der Server neu startet oder /api/refresh aufgerufen
# wird. Grund: Yahoo drosselt bei vielen Abrufen die ganze IP-Adresse. Wer
# jeden Kurs erst dann holt, wenn er gebraucht wird, verliert genau den
# Abruf, auf den es ankommt – etwa beim Auswaehlen eines Wertpapiers in der
# Ordermaske. Was einmal im Sitzungsspeicher liegt, verfaellt deshalb nicht;
# die TTL des Plattencaches gilt hier bewusst nicht.

SESSION_RANGE = "10y"          # Zeitraum, der je Symbol einmal geholt wird

_session = {
    "hist": {},                # Symbol -> Kursreihe
    "fx": {},                  # Waehrung -> Tagesreihe nach EUR
    "errors": {},              # Symbol -> Fehler des Startabrufs
    "loading": False,
    "done": 0,
    "total": 0,
    "loadedAt": 0.0,
    "retryAt": 0.0,            # Zeitpunkt des naechsten Nachversuchs, 0 = keiner
    "attempt": 0,
}
_session_lock = threading.Lock()
_entry_locks = {}


def _entry_lock(name):
    """Je Symbol eine Sperre – zwei gleichzeitige Anfragen, ein Abruf."""
    with _session_lock:
        return _entry_locks.setdefault(name, threading.Lock())


def session_history(symbol, refresh=False):
    """Kursreihe aus dem Sitzungsspeicher; holt sie nur beim ersten Mal."""
    if not refresh:
        with _session_lock:
            hit = _session["hist"].get(symbol)
        if hit:
            return hit

    with _entry_lock("hist:" + symbol):
        if not refresh:
            with _session_lock:
                hit = _session["hist"].get(symbol)
            if hit:
                return hit
        data = dict(yahoo_history(symbol, SESSION_RANGE, "1d", refresh=refresh),
                    fetchedAt=time.time())
        with _session_lock:
            _session["hist"][symbol] = data
            _session["errors"].pop(symbol, None)
        return data


def fx_start():
    """Frueheste Buchung im Depot, mindestens aber zehn Jahre zurueck."""
    earliest = (date.today() - timedelta(days=365 * 10 + 30)).isoformat()
    for txn in (read_state() or {}).get("transactions") or []:
        day = txn.get("date")
        if isinstance(day, str) and len(day) == 10 and day < earliest:
            earliest = day
    return earliest


def session_fx(base, refresh=False):
    """Wechselkursreihe aus dem Sitzungsspeicher – Startdatum deckt alles ab."""
    base = (base or "EUR").upper()
    if base == "EUR":
        return {"base": "EUR", "latest": 1.0, "rates": {}}
    if not refresh:
        with _session_lock:
            hit = _session["fx"].get(base)
        if hit:
            return hit

    with _entry_lock("fx:" + base):
        if not refresh:
            with _session_lock:
                hit = _session["fx"].get(base)
            if hit:
                return hit
        out = fx_series(base, fx_start(), refresh=refresh)
        with _session_lock:
            _session["fx"][base] = out
        return out


def session_quote(symbol):
    """Aktueller Kurs – aus der Sitzungsreihe, ohne neuen Netzabruf."""
    hist = session_history(symbol)
    pts = hist["points"]
    price = hist.get("price")
    if price is None and pts:
        price = pts[-1][1]
    prev = hist.get("previousClose")
    if prev is None and len(pts) >= 2:
        prev = pts[-2][1]
    return {
        "symbol": hist["symbol"], "name": hist["name"],
        "currency": hist["currency"], "price": price,
        "previousClose": prev, "source": hist.get("source"),
        "date": pts[-1][0] if pts else None,
    }


# Notliste, falls der CATALOG in web_app.html nicht gelesen werden kann
FALLBACK_SYMBOLS = ["EUNL.DE", "VWCE.DE", "SXR8.DE", "4GLD.DE", "GC=F",
                    "SAP.DE", "AAPL", "MSFT", "BTC-EUR", "^GDAXI"]


def catalog_symbols():
    """Symbole aus dem CATALOG der Web-App lesen – keine zweite Liste pflegen."""
    try:
        with open(os.path.join(BASE_DIR, "web_app.html"), "r", encoding="utf-8") as fh:
            block = fh.read().split("const CATALOG = [", 1)[1].split("\n];", 1)[0]
        syms = re.findall(r'\{\s*s\s*:\s*"([^"]+)"', block)
    except Exception:
        syms = []
    return syms or list(FALLBACK_SYMBOLS)


def warmup_symbols():
    """Erst die gehaltenen Positionen, dann der Katalog der Marktansicht."""
    syms = []
    for txn in (read_state() or {}).get("transactions") or []:
        sym = txn.get("symbol")
        if sym and sym not in syms:
            syms.append(sym)
    for sym in catalog_symbols():
        if sym not in syms:
            syms.append(sym)
    return syms


# Steht Yahoo beim Start gerade auf Sperre, faellt der ganze Abruf aus und
# das Dashboard haette die Sitzung ueber keine Kurse. Deshalb werden die
# ausgefallenen Symbole spaeter von selbst noch einmal versucht – die Sperre
# loest sich nach Minuten, ohne dass jemand etwas druecken muss.
WARMUP_RETRIES = 5
RETRY_WAIT = 90


def warmup(symbols=None, force=False, attempt=1):
    """Startabruf: alle Symbole der Reihe nach in den Sitzungsspeicher."""
    syms = warmup_symbols() if symbols is None else list(symbols)
    with _session_lock:
        if _session["loading"]:
            return False
        _session["loading"] = True
        _session["done"] = 0
        _session["total"] = len(syms)
        _session["retryAt"] = 0.0
        _session["attempt"] = attempt
        for sym in syms:                     # nur die eigenen Fehler zuruecksetzen
            _session["errors"].pop(sym, None)
        if force:
            _session["hist"].clear()
            _session["fx"].clear()

    ok, currencies = 0, set()
    try:
        for sym in syms:
            try:
                hist = session_history(sym, refresh=force)
                cur = (hist.get("currency") or "EUR").upper()
                if cur not in currencies:
                    currencies.add(cur)
                    try:
                        session_fx(cur)
                    except Exception as exc:
                        print("  Wechselkurs %s fehlt (%s)" % (cur, exc))
                ok += 1
            except Exception as exc:
                with _session_lock:
                    _session["errors"][sym] = str(exc)
            finally:
                with _session_lock:
                    _session["done"] += 1
    finally:
        with _session_lock:
            _session["loading"] = False
            _session["loadedAt"] = time.time()
            failed = [s for s in syms if s in _session["errors"]]
    print("  Kurse der Sitzung geladen: %d von %d Symbolen" % (ok, len(syms)))

    if failed and attempt <= WARMUP_RETRIES:
        # Nach einer Yahoo-Sperre erst wieder anklopfen, wenn sie abgelaufen ist
        wait = max(RETRY_WAIT, _yahoo_blocked_until[0] - time.time() + 5)
        with _session_lock:
            _session["retryAt"] = time.time() + wait
        print("  %d Symbol(e) ohne Kurs – neuer Versuch in %d s (%d/%d)"
              % (len(failed), wait, attempt, WARMUP_RETRIES))
        timer = threading.Timer(wait, warmup, kwargs={"symbols": failed, "attempt": attempt + 1})
        timer.daemon = True
        timer.start()
    return True


def start_warmup(force=False):
    threading.Thread(target=warmup, kwargs={"force": force}, daemon=True).start()


def snapshot_meta(with_lists=True):
    with _session_lock:
        out = {
            "loading": _session["loading"],
            "done": _session["done"],
            "total": _session["total"],
            "loadedAt": _session["loadedAt"],
            "range": SESSION_RANGE,
            "count": len(_session["hist"]),
            "retryAt": _session["retryAt"],
            "attempt": _session["attempt"],
        }
        if with_lists:
            out["symbols"] = sorted(_session["hist"])
            out["currencies"] = sorted(_session["fx"])
            out["errors"] = dict(_session["errors"])
    return out


def snapshot_full():
    out = snapshot_meta()
    with _session_lock:
        out["history"] = dict(_session["hist"])
        out["fx"] = dict(_session["fx"])
    return out


# -------------------------------------------------------------- Server

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    # Ruhigeres Log: nur Fehler und API-Aufrufe
    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/" in msg or " 4" in msg or " 5" in msg:
            sys.stderr.write("  %s\n" % msg)

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/refresh":
            # Ausdrueckliches Neuladen: laeuft im Hintergrund, der Fortschritt
            # kommt ueber /api/snapshot zurueck.
            start_warmup(force=True)
            return self._send(snapshot_meta(with_lists=False))
        if parsed.path != "/api/state":
            return self._send({"error": "unbekannter Endpunkt"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send({"error": "leerer Rumpf"}, 400)
        if length > MAX_STATE_BYTES:
            return self._send({"error": "Depot zu gross (%d Bytes)" % length}, 413)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("transactions"), list):
                raise ValueError("kein gueltiges Depot")
            write_state(data)
            return self._send({"ok": True, "path": STATE_FILE,
                               "updatedAt": data.get("updatedAt", 0),
                               "transactions": len(data["transactions"])})
        except Exception as exc:
            return self._send({"error": str(exc)}, 400)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            if parsed.path == "/":
                self.path = "/web_app.html"
            return super().do_GET()

        params = urllib.parse.parse_qs(parsed.query)
        one = lambda k, d=None: (params.get(k) or [d])[0]

        try:
            if parsed.path == "/api/status":
                sources = ["Yahoo Finance"]
                if config().get("twelvedata_key"):
                    sources.append("Twelve Data")
                sources += ["CoinGecko (Krypto)", "EZB/Frankfurter (Wechselkurse)"]
                return self._send({"live": True, "source": " · ".join(sources),
                                   "twelvedata": bool(config().get("twelvedata_key")),
                                   "mode": "session",
                                   "snapshot": snapshot_meta(with_lists=False)})

            if parsed.path == "/api/snapshot":
                # Ohne full=1 nur der Fortschritt – die vollen Kursreihen sind
                # einige Megabyte und werden nur einmal je Sitzung geholt.
                if one("full") in ("1", "true", "yes"):
                    return self._send(snapshot_full())
                return self._send(snapshot_meta())

            if parsed.path == "/api/history":
                symbol = one("symbol")
                if not symbol:
                    return self._send({"error": "symbol fehlt"}, 400)
                # range/interval werden entgegengenommen, aber nicht beachtet:
                # die Sitzung haelt je Symbol genau eine Reihe (SESSION_RANGE),
                # aus der die App selbst den gewuenschten Ausschnitt schneidet.
                return self._send(session_history(symbol))

            if parsed.path == "/api/quote":
                symbols = [s for s in (one("symbols", "") or "").split(",") if s]
                if not symbols:
                    return self._send({"error": "symbols fehlt"}, 400)
                out, errs = {}, {}
                for s in symbols[:40]:
                    try:
                        out[s] = session_quote(s)
                    except Exception as exc:
                        errs[s] = str(exc)
                return self._send({"quotes": out, "errors": errs})

            if parsed.path == "/api/search":
                q = (one("q", "") or "").strip()
                if len(q) < 2:
                    return self._send({"results": []})
                return self._send(yahoo_search(q))

            if parsed.path == "/api/state":
                data = read_state()
                return self._send({"state": data,
                                   "path": STATE_FILE,
                                   "updatedAt": (data or {}).get("updatedAt", 0)})

            if parsed.path == "/api/fx":
                # start wird nicht beachtet: die Sitzungsreihe beginnt bereits
                # vor der aeltesten Buchung.
                return self._send(session_fx(one("base", "USD")))

            return self._send({"error": "unbekannter Endpunkt"}, 404)

        except urllib.error.HTTPError as exc:
            self._send({"error": "Datenquelle antwortete mit %s" % exc.code}, 502)
        except urllib.error.URLError as exc:
            self._send({"error": "Keine Verbindung: %s" % exc.reason}, 502)
        except Exception as exc:
            self._send({"error": str(exc)}, 500)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("Port muss eine Zahl sein.")
            return 1

    try:
        httpd = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        print("Port %d nicht verfuegbar (%s). Anderer Port: python3 server.py 9000" % (port, exc))
        return 1

    url = "http://localhost:%d/" % port
    print("Virtuelles Finanzdashboard")
    print("  %s" % url)
    print("  Kurse via Yahoo Finance, Wechselkurse via Frankfurter (EZB)")
    print("  Sie werden jetzt einmal geholt und gelten dann fuer die ganze")
    print("  Sitzung – neu laden im Depot ueber \"Kurse aktualisieren\".")
    existing = read_state()
    if existing:
        print("  Depot: %s (%d Buchungen)" % (STATE_FILE, len(existing.get("transactions") or [])))
    else:
        print("  Depot wird automatisch gespeichert in %s" % STATE_FILE)
    print("  Beenden mit Strg+C\n")
    start_warmup()                  # Kurse der Sitzung im Hintergrund holen
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
