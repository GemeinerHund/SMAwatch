#!/usr/bin/env python3
"""200-Tage-SMA-Wächter für S&P 500 und PHLX Semiconductor (SOX).

Läuft jeden US-Börsentag 2 Stunden nach Handelsstart (11:30 Uhr New York,
= 17:30 Uhr deutscher Zeit) als GitHub Action. Vergleicht den aktuellen Kurs mit
dem 200d SMA, schickt eine Push-Nachricht über ntfy und schreibt docs/status.json
für die Status-Webseite.

SMA-Berechnung: die letzten 199 Schlusskurse plus der aktuelle Kurs, also so,
wie ein Live-Chart (z. B. TradingView) den 200er SMA im laufenden Handel zeigt.
"Kreuzung" heißt: heute auf der anderen Seite als beim gestrigen Schlusskurs.

Umgebungsvariablen:
  NTFY_TOPIC    ntfy-Topic (Pflicht für Push; ohne Topic wird nur status.json geschrieben)
  NTFY_SERVER   optional, Standard https://ntfy.sh
  FORCE_NOTIFY  "true" = Zeitfenster und Tagessperre ignorieren (manueller Test)
  PAGES_URL     optional, Link der beim Antippen der Nachricht geöffnet wird
  ONLY_ON_CROSS "true" = nur bei Kreuzungen pushen (Standard: täglich + Kreuzungen)
  CHECK_AFTER   optional, frühester Prüfzeitpunkt New Yorker Zeit, Standard "11:15"
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SMA_LEN = 200
HISTORY_DAYS = 260  # ca. 1 Jahr für den Chart auf der Webseite
STATUS_FILE = Path(__file__).parent / "docs" / "status.json"
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")

INDICES = [
    {"key": "spx", "name": "S&P 500", "short": "S&P 500", "yahoo": "^GSPC", "stooq": "^spx"},
    {"key": "sox", "name": "PHLX SOX (Halbleiter)", "short": "SOX", "yahoo": "^SOX", "stooq": "^sox"},
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------- Daten holen
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def from_yahoo(symbol: str) -> tuple[list[tuple[str, float]], int | None]:
    """Tageskurse inkl. heutigem Live-Kurs. Gibt (rows, kurszeit_epoch) zurück."""
    last_err: Exception | None = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(symbol)}?range=2y&interval=1d&includePrePost=false")
        try:
            res = json.loads(http_get(url))["chart"]["result"][0]
            meta = res["meta"]
            off = int(meta.get("gmtoffset", 0))
            rows = []
            for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
                if c is not None:
                    d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date().isoformat()
                    rows.append((d, float(c)))
            price, qt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
            if price and qt:
                d = dt.datetime.fromtimestamp(qt + off, dt.timezone.utc).date().isoformat()
                if rows and rows[-1][0] == d:
                    rows[-1] = (d, float(price))
                elif not rows or d > rows[-1][0]:
                    rows.append((d, float(price)))
            return rows, qt
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Yahoo {symbol}: {last_err}")


def from_stooq(symbol: str) -> tuple[list[tuple[str, float]], int | None]:
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&i=d"
    text = http_get(url).decode("utf-8", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            rows.append((r["Date"], float(r["Close"])))
        except (KeyError, ValueError):
            continue
    if not rows:
        raise RuntimeError(f"Stooq {symbol}: keine Daten")
    return rows, None


def load_series(idx: dict):
    errors = []
    for name, fn, sym in (("Yahoo", from_yahoo, idx["yahoo"]), ("Stooq", from_stooq, idx["stooq"])):
        try:
            rows, qt = fn(sym)
            rows = sorted(dict(rows).items())  # deduplizieren + sortieren
            if len(rows) >= SMA_LEN + 1:
                return rows, name, qt
            errors.append(f"{name}: nur {len(rows)} Tage")
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    raise RuntimeError(f"{idx['short']}: " + " | ".join(errors))


# ---------------------------------------------------------------- Rechnen
def analyse(rows: list[tuple[str, float]]) -> dict:
    dates = [d for d, _ in rows]
    px = [c for _, c in rows]
    sma: list[float | None] = [None] * len(px)
    s = sum(px[:SMA_LEN])
    sma[SMA_LEN - 1] = s / SMA_LEN
    for i in range(SMA_LEN, len(px)):
        s += px[i] - px[i - SMA_LEN]
        sma[i] = s / SMA_LEN

    i = len(px) - 1
    above = px[i] > sma[i]
    prev_above = px[i - 1] > sma[i - 1]  # gestriger Schlusskurs vs. gestriger SMA
    j = i
    while j - 1 >= SMA_LEN - 1 and (px[j - 1] > sma[j - 1]) == above:
        j -= 1

    start = max(SMA_LEN - 1, len(px) - HISTORY_DAYS)
    return {
        "date": dates[i],
        "price": round(px[i], 2),
        "sma200": round(sma[i], 2),
        "diff_pct": round((px[i] / sma[i] - 1) * 100, 2),
        "above": above,
        "prev_above": prev_above,
        "crossed": above != prev_above,
        "since": dates[j],
        "streak_days": i - j + 1,
        "history": {
            "dates": dates[start:],
            "close": [round(c, 2) for c in px[start:]],
            "sma200": [round(x, 2) for x in sma[start:]],
        },
    }


# ---------------------------------------------------------------- Formatieren
def de_num(x: float, dec: int = 0) -> str:
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def de_date(iso: str) -> str:
    return dt.date.fromisoformat(iso).strftime("%d.%m.%Y")


def line_for(idx: dict, r: dict) -> str:
    arrow = "▲ über" if r["above"] else "▼ unter"
    sign = "+" if r["diff_pct"] >= 0 else ""
    days = r["streak_days"]
    cross = " (seit gestern gekreuzt!)" if r["crossed"] else ""
    return (f"{idx['short']} {arrow} SMA200{cross}\n"
            f"  {de_num(r['price'])} vs. {de_num(r['sma200'])} ({sign}{de_num(r['diff_pct'], 1)} %)\n"
            f"  seit {de_date(r['since'])} · {days} Handelstag{'e' if days != 1 else ''}")


# ---------------------------------------------------------------- ntfy
def ntfy(title: str, message: str, priority: int, tags: list[str]) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC fehlt – keine Push-Nachricht gesendet.")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = {"topic": topic, "title": title, "message": message,
            "priority": priority, "tags": tags}
    if os.environ.get("PAGES_URL"):
        body["click"] = os.environ["PAGES_URL"]
    req = urllib.request.Request(server + "/", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print("ntfy:", r.status)


# ---------------------------------------------------------------- Ablauf
def main() -> int:
    force = os.environ.get("FORCE_NOTIFY", "").lower() == "true"
    only_cross = os.environ.get("ONLY_ON_CROSS", "").lower() == "true"
    after = dt.time.fromisoformat(os.environ.get("CHECK_AFTER") or "11:15")

    now_ny = dt.datetime.now(NY)
    today = now_ny.date().isoformat()

    old = {}
    if STATUS_FILE.exists():
        try:
            old = json.loads(STATUS_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            old = {}

    # Zwei Cron-Zeiten (wegen Sommer-/Winterzeit) – nur die passende arbeitet.
    if not force:
        if now_ny.weekday() >= 5:
            print("Wochenende – nichts zu tun."); return 0
        if now_ny.time() < after:
            print(f"Erst {now_ny:%H:%M} Uhr in New York – zu früh, warte auf den nächsten Lauf."); return 0
        if old.get("sent_date") == today:
            print("Heute schon gemeldet – nichts zu tun."); return 0

    results, errors, quote_times = {}, [], []
    for idx in INDICES:
        try:
            rows, src, qt = load_series(idx)
            r = analyse(rows)
            r["source"] = src
            r["live"] = r["date"] == today
            results[idx["key"]] = r
            if qt:
                quote_times.append(qt)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    if errors and not results:
        ntfy("SMA-Wächter: Datenabruf fehlgeschlagen", "\n".join(errors), 2, ["warning"])
        print("\n".join(errors), file=sys.stderr)
        return 1

    if not force and not any(r["live"] for r in results.values()):
        print("Kein Kurs von heute – US-Börse vermutlich geschlossen (Feiertag)."); return 0

    qt = max(quote_times) if quote_times else None
    kurszeit = (dt.datetime.fromtimestamp(qt, BERLIN).strftime("%H:%M Uhr") if qt else "")

    lines = []
    for idx in INDICES:
        r = results.get(idx["key"])
        if r:
            lines.append(line_for(idx, r))
    if errors:
        lines.append("⚠ " + " | ".join(errors))
    stale = [i["short"] for i in INDICES if i["key"] in results and not results[i["key"]]["live"]]
    if stale:
        lines.append("Hinweis: kein Live-Kurs für " + ", ".join(stale) + " (letzter Schlusskurs)")
    msg = "\n".join(lines)
    print(msg)

    crosses = [(i, results[i["key"]]) for i in INDICES if i["key"] in results and results[i["key"]]["crossed"]]
    sent = False
    if crosses:
        parts = [f"{i['short']} {'über' if r['above'] else 'unter'}" for i, r in crosses]
        up = all(r["above"] for _, r in crosses)
        ntfy("Kreuzung 200d SMA: " + ", ".join(parts), msg, 5,
             ["rotating_light", "chart_with_upwards_trend" if up else "chart_with_downwards_trend"])
        sent = True
    elif not only_cross or force:
        status = " | ".join(f"{i['short']} {'▲' if results[i['key']]['above'] else '▼'}"
                            for i in INDICES if i["key"] in results)
        ntfy(f"200d SMA {kurszeit}: {status}".replace("  ", " "), msg, 3, ["bar_chart"])
        sent = True

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({
        "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote_utc": (dt.datetime.fromtimestamp(qt, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                      if qt else None),
        # Tagessperre nur, wenn regulär gemeldet wurde (ein Test blockiert den Tag nicht)
        "sent_date": today if (not force and (sent or only_cross)) else old.get("sent_date"),
        "sma_len": SMA_LEN,
        "names": {i["key"]: i["name"] for i in INDICES},
        "errors": errors,
        "indices": results,
    }, ensure_ascii=False, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
