"""
alerts.py -- "tell me when", so you don't have to watch.

    alert NVDA < 200
    alert AAPL rsi < 30
    alert BTC-USD vol > 3

Rules live in the same SQLite file as everything else and are checked by a
background thread while the terminal is open. A fired alert prints in the
terminal and, when MacBridge is running, becomes a real macOS notification
-- so it reaches you even when the terminal isn't the window in front.

Alerts fire once and then disarm, because an alert that fires every ten
seconds is noise, not information.
"""
from __future__ import annotations

import re
import time

from . import quotes, screen
from ._http import HttpError, post_json
from .data import connect

FIELDS = {"price", "change", "rsi", "vol", "near_high", "near_low", "above_sma50",
          "above_sma200", "mom20", "atr"}

_RULE = re.compile(r"^\s*([A-Za-z0-9.\-]+)\s*([a-z_0-9]*)\s*(>=|<=|>|<)\s*(-?[\d.]+)\s*$")


def _init() -> None:
    connect().execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, field TEXT, op TEXT,
            value REAL, created REAL, fired REAL, active INTEGER DEFAULT 1, note TEXT
        )""")
    connect().commit()


def parse(text: str) -> tuple[str, str, str, float]:
    """"NVDA rsi < 30" or "NVDA < 200" (price is the default field)."""
    match = _RULE.match(text or "")
    if not match:
        raise ValueError("Say it like `alert NVDA < 200` or `alert AAPL rsi < 30`.")
    symbol, field, operator, value = match.groups()
    field = (field or "price").lower()
    if field not in FIELDS:
        raise ValueError(f"'{field}' isn't something to watch. Try: {', '.join(sorted(FIELDS))}.")
    return symbol.upper(), field, operator, float(value)


def add(text: str) -> dict:
    symbol, field, operator, value = parse(text)
    _init()
    cursor = connect().execute(
        "INSERT INTO alerts (symbol, field, op, value, created, active) VALUES (?,?,?,?,?,1)",
        (symbol, field, operator, value, time.time()))
    connect().commit()
    return {"id": cursor.lastrowid, "symbol": symbol, "field": field, "op": operator, "value": value}


def remove(alert_id: int) -> bool:
    _init()
    cursor = connect().execute("DELETE FROM alerts WHERE id=?", (int(alert_id),))
    connect().commit()
    return cursor.rowcount > 0


def all_alerts(active_only: bool = False) -> list[dict]:
    _init()
    query = "SELECT * FROM alerts" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return [dict(row) for row in connect().execute(query).fetchall()]


def _value(symbol: str, field: str) -> float | None:
    if field == "price":
        quote = quotes.one(symbol, max_age=30)
        return quote.price or None
    if field == "change":
        return quotes.one(symbol, max_age=30).change_pct
    try:
        return screen.facts(symbol).get(field)
    except Exception:
        return None


def _hit(actual: float, operator: str, value: float) -> bool:
    return {">": actual > value, ">=": actual >= value,
            "<": actual < value, "<=": actual <= value}.get(operator, False)


def check() -> list[dict]:
    """Evaluates every armed alert; returns the ones that just fired."""
    fired = []
    for alert in all_alerts(active_only=True):
        actual = _value(alert["symbol"], alert["field"])
        if actual is None:
            continue
        if _hit(actual, alert["op"], alert["value"]):
            connect().execute("UPDATE alerts SET active=0, fired=? WHERE id=?",
                              (time.time(), alert["id"]))
            connect().commit()
            alert["actual"] = actual
            fired.append(alert)
    return fired


def describe(alert: dict) -> str:
    unit = "" if alert["field"] != "price" else "$"
    text = f"{alert['symbol']} {alert['field']} {alert['op']} {unit}{alert['value']:g}"
    if alert.get("actual") is not None:
        text += f"  (now {alert['actual']:,.2f})"
    return text


def notify(alert: dict, port: int = 8765) -> bool:
    """Through MacBridge, so it lands as a real notification rather than a
    line in a window you aren't looking at."""
    try:
        post_json(f"http://127.0.0.1:{port}/notifications/send",
                  {"args": {"title": f"{alert['symbol']} alert", "body": describe(alert)}},
                  timeout=5)
        return True
    except HttpError:
        return False
