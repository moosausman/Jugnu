"""
sources.py -- extra data, from OpenBB when it's installed.

The prices this package runs on come from Yahoo and Binance directly, and
always will: they're free, keyless, and the engine can't afford to depend
on a large install. OpenBB adds the things those two endpoints don't give
you -- fundamentals, options chains, insider and institutional activity,
analyst estimates, macro series -- behind one API across dozens of
providers.

It is optional, on purpose:

  * `pip install openbb` pulls a very large dependency tree into a Python
    environment that ships inside the Jugnu app bundle;
  * OpenBB is AGPLv3, which is strong copyleft. Importing it into a
    program you distribute makes that program AGPL too. Kept behind an
    optional import and installed by you, it stays your tool on your
    machine, and Jugnu itself stays unentangled.

So: everything here degrades to "install OpenBB for this" rather than
failing, and nothing else in the package imports it.

    jugnu install-openbb        # one-off, into Jugnu's own Python
    source                      # in the terminal: what's available
"""
from __future__ import annotations

import functools
import os
import subprocess
import sys
from pathlib import Path

_MISSING = ("OpenBB isn't installed. Run `jugnu install-openbb` (a few hundred MB, one-off) "
            "to add fundamentals, options and analyst data.")


def sidecar() -> Path:
    """OpenBB lives in its own environment beside Jugnu's data, never inside
    the app bundle: it's hundreds of megabytes, and it's AGPL, which has no
    business being linked into something you might hand to someone else."""
    base = Path(os.environ.get("JUGNU_DATA_DIR")
                or Path.home() / "Library" / "Application Support" / "Jugnu")
    return base / "openbb-env"


def _site_packages() -> Path | None:
    for path in sorted(sidecar().glob("lib/python*/site-packages")):
        return path
    return None


@functools.lru_cache(maxsize=1)
def obb():
    """The OpenBB app object, or None. Cached: the import takes seconds."""
    packages = _site_packages()
    if packages and str(packages) not in sys.path:
        sys.path.append(str(packages))
    try:
        from openbb import obb as _obb            # noqa: PLC0415
        return _obb
    except Exception:
        return None


def available() -> bool:
    return obb() is not None


def version() -> str:
    try:
        packages = _site_packages()
        if packages and str(packages) not in sys.path:
            sys.path.append(str(packages))
        import openbb
        return getattr(openbb, "__version__", "") or "installed"
    except Exception:
        return ""


def install(upgrade: bool = False, on_status=None) -> tuple[bool, str]:
    """Builds the sidecar environment and installs OpenBB into it. Takes
    minutes and several hundred megabytes; the caller says so first."""
    target = sidecar()
    python = target / "bin" / "python"
    try:
        if not python.exists():
            if on_status:
                on_status("creating the OpenBB environment…")
            subprocess.run([sys.executable, "-m", "venv", str(target)],
                           capture_output=True, text=True, timeout=600, check=True)
        if on_status:
            on_status("downloading OpenBB (this takes a few minutes)…")
        command = [str(python), "-m", "pip", "install", "--quiet", "openbb"]
        if upgrade:
            command.append("--upgrade")
        result = subprocess.run(command, capture_output=True, text=True, timeout=5400)
    except Exception as e:                       # noqa: BLE001
        return False, str(e)[:200]
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, tail[-1] if tail else "pip failed"
    # OpenBB generates its own API surface into openbb/package/*.py, and a
    # fresh install ships stale ones -- the first real call then dies with
    # an ImportError. Building here means it works the first time.
    if on_status:
        on_status("building OpenBB's interface…")
    subprocess.run([str(python), "-c", "import openbb; openbb.build()"],
                   capture_output=True, text=True, timeout=1800)
    obb.cache_clear()
    return True, version() or "installed"


def _flatten(value) -> list[dict]:
    """OpenBB hands back an OBBject whose to_dict() is *columnar*:
    {"name": ["Apple Inc."], "sector": ["Technology"]}. Turn any of its
    shapes into plain row dicts."""
    if isinstance(value, dict) and "results" in value:
        value = value["results"]
    if isinstance(value, list):
        return [row if isinstance(row, dict) else getattr(row, "__dict__", {}) for row in value]
    if isinstance(value, dict):
        if value and all(isinstance(column, list) for column in value.values()):
            length = max((len(column) for column in value.values()), default=0)
            return [{key: (column[i] if i < len(column) else None)
                     for key, column in value.items()} for i in range(length)]
        return [value]
    return []


def _rows(response) -> list[dict]:
    if response is None:
        return []
    for attempt in ("to_dict", "model_dump"):
        try:
            rows = _flatten(getattr(response, attempt)())
            if rows:
                return rows
        except Exception:
            continue
    return _flatten(getattr(response, "results", None))


def fundamentals(symbol: str) -> dict:
    """Valuation and growth, in the few numbers worth a glance."""
    app = obb()
    if app is None:
        return {"ok": False, "error": _MISSING}
    out: dict = {"ok": True, "symbol": symbol.upper()}
    try:
        overview = _rows(app.equity.profile(symbol=symbol))
        if overview:
            row = overview[0]
            out["name"] = row.get("name")
            out["sector"] = row.get("sector")
            out["industry"] = row.get("industry")
            out["market_cap"] = row.get("market_cap")
            out["employees"] = row.get("employees")
            out["description"] = (row.get("long_description") or row.get("short_description") or "")[:400]
    except Exception as e:
        out["profile_error"] = str(e)[:120]
    try:
        ratios = _rows(app.equity.fundamental.metrics(symbol=symbol))
        if ratios:
            row = ratios[0]
            for key in ("pe_ratio", "price_to_sales", "price_to_book", "dividend_yield",
                        "return_on_equity", "debt_to_equity", "gross_profit_margin",
                        "net_profit_margin", "free_cash_flow_yield"):
                if row.get(key) is not None:
                    out[key] = row[key]
    except Exception as e:
        out["metrics_error"] = str(e)[:120]
    try:
        income = _rows(app.equity.fundamental.income(symbol=symbol, limit=4))
        if len(income) >= 2:
            recent, older = income[0], income[-1]
            revenue, previous = recent.get("revenue"), older.get("revenue")
            if revenue and previous:
                out["revenue_growth"] = revenue / previous - 1
            out["revenue"] = revenue
            out["net_income"] = recent.get("net_income")
    except Exception as e:
        out["income_error"] = str(e)[:120]
    return out


def options(symbol: str) -> dict:
    """What the options market is charging -- the cheapest read on how much
    movement is expected."""
    app = obb()
    if app is None:
        return {"ok": False, "error": _MISSING}
    try:
        chain = _rows(app.derivatives.options.chains(symbol=symbol))
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    if not chain:
        return {"ok": False, "error": f"no options data for {symbol}"}
    ivs = [row.get("implied_volatility") for row in chain if row.get("implied_volatility")]
    calls = sum(1 for row in chain if str(row.get("option_type", "")).lower().startswith("c"))
    puts = len(chain) - calls
    volumes_call = sum(row.get("volume") or 0 for row in chain
                       if str(row.get("option_type", "")).lower().startswith("c"))
    volumes_put = sum(row.get("volume") or 0 for row in chain
                      if str(row.get("option_type", "")).lower().startswith("p"))
    expirations = sorted({str(row.get("expiration")) for row in chain if row.get("expiration")})[:6]
    return {"ok": True, "symbol": symbol.upper(), "contracts": len(chain),
            "implied_volatility": (sum(ivs) / len(ivs)) if ivs else None,
            "calls": calls, "puts": puts,
            "put_call_volume": (volumes_put / volumes_call) if volumes_call else None,
            "expirations": expirations}


def estimates(symbol: str) -> dict:
    """What analysts think, which is mostly useful as a contrarian read."""
    app = obb()
    if app is None:
        return {"ok": False, "error": _MISSING}
    out = {"ok": True, "symbol": symbol.upper()}
    try:
        targets = _rows(app.equity.estimates.consensus(symbol=symbol))
        if targets:
            row = targets[0]
            out["target_high"] = row.get("target_high")
            out["target_low"] = row.get("target_low")
            out["target_consensus"] = row.get("target_consensus")
            out["recommendation"] = row.get("recommendation")
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def macro(series: str = "GDP") -> dict:
    app = obb()
    if app is None:
        return {"ok": False, "error": _MISSING}
    try:
        rows = _rows(app.economy.fred_series(symbol=series, limit=60))
        return {"ok": True, "series": series, "rows": rows[-20:]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def status() -> dict:
    return {"installed": available(), "version": version(), "path": str(sidecar()),
            "note": "prices always come from Yahoo/Binance directly; OpenBB adds fundamentals, "
                    "options and analyst data" if available() else _MISSING}
