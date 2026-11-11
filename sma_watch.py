#!/usr/bin/env python3
"""200-Tage-SMA-Wächter für S&P 500 und PHLX Semiconductor (SOX).

Läuft jeden Börsentag nach US-Schluss (GitHub Actions), holt die Tagesschlusskurse,
rechnet den 200d SMA, schickt eine Push-Nachricht über ntfy und schreibt
docs/status.json für die Status-Webseite.

Umgebungsvariablen:
  NTFY_TOPIC    ntfy-Topic (Pflicht für Push; ohne Topic wird nur status.json geschrieben)
  NTFY_SERVER   optional, Standard https://ntfy.sh
  FORCE_NOTIFY  "true" = Push auch ohne neuen Handelstag senden (manueller Test)
  PAGES_URL     optional, Link der beim Antippen der Nachricht geöffnet wird
  ONLY_ON_CROSS "true" = nur bei Kreuzungen pushen (Standard: täglich + Kreuzungen)
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

SMA_LEN = 200
HISTORY_DAYS = 260  # ca. 1 Jahr für den Chart auf der Webseite
STATUS_FILE = Path(__file__).parent / "docs" / "status.json"

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


def from_yahoo(symbol: str) -> list[tuple[str, float]]:
    last_err: Exception | None = None
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(symbol)}?range=2y&interval=1d&includePrePost=false")
        try:
            res = json.loads(http_get(url))["chart"]["result"][0]
            meta = res["meta"]
            off = int(meta.get("gmtoffset", 0))
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            rows = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date().isoformat()
                rows.append((d, float(c)))
            # Laufenden Handelstag verwerfen (SMA nur auf Schlusskursen)
            reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
            if rows and reg.get("start") and reg.get("end") and reg["start"] <= now < reg["end"]:
                today = dt.datetime.fromtimestamp(reg["start"] + off, dt.timezone.utc).date().isoformat()
                if rows[-1][0] == today:
                    rows.pop()
            return rows
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Yahoo {symbol}: {last_err}")


def from_stooq(symbol: str) -> list[tuple[str, float]]:
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
    return rows


def load_series(idx: dict) -> tuple[list[tuple[str, float]], str]:
    errors = []
    for name, fn, sym in (("Yahoo", from_yahoo, idx["yahoo"]), ("Stooq", from_stooq, idx["stooq"])):
        try:
            rows = fn(sym)
            rows = sorted(dict(rows).items())  # deduplizieren + sortieren
            if len(rows) >= SMA_LEN + 1:
                return rows, name
            errors.append(f"{name}: nur {len(rows)} Tage")
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    raise RuntimeError(f"{idx['short']}: " + " | ".join(errors))


# ---------------------------------------------------------------- Rechnen
def analyse(rows: list[tuple[str, float]]) -> dict:
    dates = [d for d, _ in rows]
    closes = [c for _, c in rows]
    sma: list[float | None] = [None] * len(closes)
    s = sum(closes[:SMA_LEN])
    sma[SMA_LEN - 1] = s / SMA_LEN
    for i in range(SMA_LEN, len(closes)):
        s += closes[i] - closes[i - SMA_LEN]
        sma[i] = s / SMA_LEN

    i = len(closes) - 1
    above = closes[i] > sma[i]
    # Seit wann auf dieser Seite?
    j = i
    while j - 1 >= SMA_LEN - 1 and (closes[j - 1] > sma[j - 1]) == above:
        j -= 1
    prev_above = closes[i - 1] > sma[i - 1]

    start = max(SMA_LEN - 1, len(closes) - HISTORY_DAYS)
    return {
        "date": dates[i],
        "close": round(closes[i], 2),
        "sma200": round(sma[i], 2),
        "diff_pct": round((closes[i] / sma[i] - 1) * 100, 2),
        "above": above,
        "prev_above": prev_above,
        "since": dates[j],
        "streak_days": i - j + 1,
        "history": {
            "dates": dates[start:],
            "close": [round(c, 2) for c in closes[start:]],
            "sma200": [round(x, 2) for x in sma[start:]],
        },
    }


# ---------------------------------------------------------------- Formatieren
def de_num(x: float, dec: int = 0) -> str:
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def de_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y")


def line_for(idx: dict, r: dict) -> str:
    arrow = "▲ über" if r["above"] else "▼ unter"
    sign = "+" if r["diff_pct"] >= 0 else ""
    return (f"{idx['short']} {arrow} SMA200\n"
            f"  {de_num(r['close'])} vs. {de_num(r['sma200'])} ({sign}{de_num(r['diff_pct'], 1)} %)\n"
            f"  seit {de_date(r['since'])} · {r['streak_days']} Handelstag{'e' if r['streak_days'] != 1 else ''}")


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

    old = {}
    if STATUS_FILE.exists():
        try:
            old = {k: v for k, v in json.loads(STATUS_FILE.read_text("utf-8"))["indices"].items()}
        except Exception:  # noqa: BLE001
            old = {}

    results, sources, errors = {}, {}, []
    for idx in INDICES:
        try:
            rows, src = load_series(idx)
            results[idx["key"]] = analyse(rows)
            sources[idx["key"]] = src
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    if errors and not results:
        ntfy("SMA-Wächter: Datenabruf fehlgeschlagen", "\n".join(errors), 2, ["warning"])
        print("\n".join(errors), file=sys.stderr)
        return 1

    new_day = False
    crosses = []
    for idx in INDICES:
        r = results.get(idx["key"])
        if not r:
            continue
        prev = old.get(idx["key"])
        if not prev or prev.get("date") != r["date"]:
            new_day = True
        # Kreuzung: gegenüber dem letzten gespeicherten Stand (fängt auch ausgefallene
        # Läufe ab); beim allerersten Lauf gegenüber dem Vortag; bei erneutem Lauf
        # am selben Tag bleibt die Referenz gleich.
        if not prev:
            ref_above = r["prev_above"]
        elif prev.get("date") != r["date"]:
            ref_above = prev["above"]
        else:
            ref_above = prev.get("ref_above", r["prev_above"])
        r["ref_above"] = ref_above
        r["crossed"] = r["above"] != ref_above
        r["source"] = sources[idx["key"]]
        if r["crossed"]:
            crosses.append((idx, r))
        # alten Stand behalten, falls ein Index diesmal fehlte
    for idx in INDICES:
        if idx["key"] not in results and idx["key"] in old:
            results[idx["key"]] = old[idx["key"]]

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({
        "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sma_len": SMA_LEN,
        "names": {i["key"]: i["name"] for i in INDICES},
        "errors": errors,
        "indices": results,
    }, ensure_ascii=False, indent=1), "utf-8")

    lines = [line_for(i, results[i["key"]]) for i in INDICES if i["key"] in results]
    if errors:
        lines.append("⚠ " + " | ".join(errors))
    msg = "\n".join(lines)
    print(msg)

    if not new_day and not force:
        print("Kein neuer Handelstag – keine Push-Nachricht.")
        return 0

    if crosses:
        parts = [f"{i['short']} {'über' if r['above'] else 'unter'}" for i, r in crosses]
        title = "Kreuzung 200d SMA: " + ", ".join(parts)
        up = all(r["above"] for _, r in crosses)
        ntfy(title, msg, 5, ["rotating_light", "chart_with_upwards_trend" if up else "chart_with_downwards_trend"])
    elif not only_cross or force:
        status = " | ".join(
            f"{i['short']} {'▲' if results[i['key']]['above'] else '▼'}" for i in INDICES if i["key"] in results)
        ntfy(f"200d SMA: {status}", msg, 3, ["bar_chart"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
