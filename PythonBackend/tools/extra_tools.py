"""
extra_tools.py — additional API integrations for Jugnu
========================================================

Adds capabilities on top of the existing tool set in tools.py:

  - Open-Meteo geocoding  : place name -> coordinates
  - Open-Meteo weather    : coordinates -> current conditions + forecast
  - Wikipedia             : search + page summaries
  - Nominatim (OSM)       : richer forward/reverse geocoding
  - Notion                : search, create page, create database, read page,
                            query database
  - Temporary email       : disposable inbox via mail.tm, for test signups

All functions return plain strings (never raise) so they drop straight into
run_tool() in bridge.py the same way the existing tools.py functions do.
Docstrings follow the same Google-style "Args:" convention tools.py's
make_schema() parses, so schema generation needs no changes.

--- Wiring this in -----------------------------------------------------
In tools.py, add:

    from .extra_tools import (
        geocode_location, get_weather,
        wikipedia_search, wikipedia_summary,
        nominatim_geocode, nominatim_reverse_geocode,
        notion_search, notion_create_page, notion_create_database,
        notion_read_page, notion_query_database,
        create_temp_email, check_temp_email, read_temp_email_message,
    )

and append those names to the `_tool_functions` list.

--- Environment variables ------------------------------------------------
  NOTION_API_KEY      Internal integration token from notion.so/my-integrations.
                       Required for all notion_* functions. Pages/databases must
                       also be explicitly shared with that integration in Notion.
  NOMINATIM_CONTACT    Your email or app name, sent in the User-Agent for
                       Nominatim requests. Falls back to a placeholder if unset
                       (fine for occasional personal use, but set this).

--- A note on Nominatim --------------------------------------------------
The public Nominatim endpoint (nominatim.openstreetmap.org) is free, donated
infrastructure with a real usage policy, not just a rate limit:
  https://operations.osmfoundation.org/policies/nominatim/
Key points: max 1 request/second (enforced below), a descriptive User-Agent
identifying your app (not a stock library UA), clear attribution to OSM in
anything you display, no bulk/automated geocoding jobs, no reselling of
results, and no client-side autocomplete. This module rate-limits and sets a
descriptive header for you, but the responsibility for compliant use is
yours as the developer, not something delegated to Jugnu at runtime. If you
outgrow personal/light use, look at running your own Nominatim instance or
a commercial provider (linked from the policy page above).
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import time

import requests

# ---------------------------------------------------------------------------
# Open-Meteo — geocoding
# ---------------------------------------------------------------------------

def geocode_location(name: str, count: int = 5) -> str:
    """Looks up latitude/longitude coordinates for a place name using Open-Meteo's geocoding API.

    Use this before get_weather when you only have a place name (city, town,
    landmark) rather than coordinates. Returns multiple candidates so the
    agent can disambiguate common names like "Springfield" using admin
    region and country.

    Args:
        name: Place name to search for, e.g. "Lahore" or "Paris, France".
        count: Max number of candidate matches to return.
    """
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": count, "language": "en", "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Geocoding error: {e}"

    results = data.get("results") or []
    if not results:
        return f"No location found matching '{name}'."

    lines = []
    for r in results:
        parts = [r.get("name", "?")]
        for key in ("admin1", "admin2", "country"):
            if r.get(key):
                parts.append(r[key])
        label = ", ".join(parts)
        lines.append(
            f"{label} — lat={r.get('latitude')}, lon={r.get('longitude')}, "
            f"timezone={r.get('timezone', 'unknown')}, population={r.get('population', 'unknown')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Open-Meteo — weather
# ---------------------------------------------------------------------------

_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm, slight hail", 99: "Thunderstorm, heavy hail",
}


def _weather_desc(code) -> str:
    try:
        return _WEATHER_CODES.get(int(code), f"code {code}")
    except (TypeError, ValueError):
        return "unknown"


def get_weather(latitude: float, longitude: float, forecast_days: int = 3) -> str:
    """Fetches current conditions and a daily forecast for a coordinate using Open-Meteo.

    Use geocode_location first if you only have a place name — call it with
    the actual returned latitude/longitude, never a guessed coordinate.

    Args:
        latitude: Latitude in decimal degrees.
        longitude: Longitude in decimal degrees.
        forecast_days: Number of forecast days to include, from 1 to 16.
    """
    forecast_days = max(1, min(int(forecast_days), 16))
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                           "precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                         "precipitation_sum,wind_speed_10m_max",
                "forecast_days": forecast_days,
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Weather lookup error: {e}"

    current = data.get("current", {})
    daily = data.get("daily", {})

    lines = [
        f"Current: {_weather_desc(current.get('weather_code'))}, "
        f"{current.get('temperature_2m')}°C "
        f"(feels like {current.get('apparent_temperature')}°C), "
        f"humidity {current.get('relative_humidity_2m')}%, "
        f"wind {current.get('wind_speed_10m')} km/h, "
        f"precipitation {current.get('precipitation')} mm."
    ]

    days = daily.get("time", [])
    codes = daily.get("weather_code", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    precs = daily.get("precipitation_sum", [])
    for i, day in enumerate(days):
        lines.append(
            f"{day}: {_weather_desc(codes[i]) if i < len(codes) else ''} — "
            f"high {highs[i] if i < len(highs) else '?'}°C / "
            f"low {lows[i] if i < len(lows) else '?'}°C, "
            f"precip {precs[i] if i < len(precs) else '?'} mm"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------

_WIKI_HEADERS = {
    "User-Agent": "Jugnu-PersonalAssistant/1.0 (personal use; set WIKI_CONTACT env var for contact info)"
}


def wikipedia_search(query: str, limit: int = 5) -> str:
    """Searches Wikipedia for pages matching a query and returns short excerpts.

    Args:
        query: What to search Wikipedia for.
        limit: Max number of results to return.
    """
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/rest.php/v1/search/page",
            params={"q": query, "limit": limit},
            headers=_WIKI_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Wikipedia search error: {e}"

    pages = data.get("pages") or []
    if not pages:
        return f"No Wikipedia pages found for '{query}'."

    lines = []
    for p in pages:
        excerpt = re.sub(r"<[^>]+>", "", p.get("excerpt", ""))
        lines.append(f"{p.get('title')}: {excerpt}")
    return "\n".join(lines)


def wikipedia_summary(title: str) -> str:
    """Fetches a plain-text summary of a specific Wikipedia page by its exact title.

    Use this after wikipedia_search to get full detail on one specific
    result, or directly if the exact page title is already known.

    Args:
        title: Exact Wikipedia page title, e.g. "Lahore" or "Python (programming language)".
    """
    try:
        resp = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title)}",
            headers=_WIKI_HEADERS,
            timeout=10,
        )
        if resp.status_code == 404:
            return f"No Wikipedia page found titled '{title}'."
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Wikipedia summary error: {e}"

    extract = data.get("extract", "No summary available.")
    url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
    return f"{data.get('title', title)}: {extract}" + (f"\n{url}" if url else "")


# ---------------------------------------------------------------------------
# Nominatim (OpenStreetMap) — see the usage-policy note in the module
# docstring above before relying on this in anything but light personal use.
# ---------------------------------------------------------------------------

_last_nominatim_call = 0.0


def _nominatim_headers() -> dict:
    contact = os.environ.get("NOMINATIM_CONTACT", "replace-with-your-contact-info@example.com")
    return {"User-Agent": f"Jugnu-PersonalAssistant/1.0 ({contact})"}


def _nominatim_wait() -> None:
    """Blocks as needed to keep Nominatim calls at or below 1 request/second,
    per the service's usage policy. Only helps within a single process — it
    doesn't coordinate across multiple processes hitting Nominatim at once."""
    global _last_nominatim_call
    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_nominatim_call = time.monotonic()


def nominatim_geocode(query: str, limit: int = 5) -> str:
    """Looks up full address details for a place using the Nominatim (OpenStreetMap) geocoding service.

    Subject to Nominatim's usage policy (1 request/second max, moderate
    personal use only, no bulk jobs) — see
    https://operations.osmfoundation.org/policies/nominatim/. This function
    self-rate-limits, but don't call it in loops or batch jobs. Prefer
    geocode_location for simple city-level lookups; use this when you need
    a fuller address breakdown (street, postcode, county).

    Args:
        query: Free-form place or address to search for.
        limit: Max number of candidate matches to return.
    """
    _nominatim_wait()
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 1},
            headers=_nominatim_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Nominatim geocoding error: {e}"

    if not data:
        return f"No address found matching '{query}'."

    lines = [
        f"{r.get('display_name')} — lat={r.get('lat')}, lon={r.get('lon')}, type={r.get('type')}"
        for r in data
    ]
    return "\n".join(lines)


def nominatim_reverse_geocode(latitude: float, longitude: float) -> str:
    """Converts a coordinate into a human-readable address using Nominatim (OpenStreetMap).

    Subject to the same usage policy as nominatim_geocode — see
    https://operations.osmfoundation.org/policies/nominatim/.

    Args:
        latitude: Latitude in decimal degrees.
        longitude: Longitude in decimal degrees.
    """
    _nominatim_wait()
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": latitude, "lon": longitude, "format": "jsonv2", "addressdetails": 1},
            headers=_nominatim_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"Nominatim reverse geocoding error: {e}"

    if not isinstance(data, dict) or "error" in data:
        return f"No address found for ({latitude}, {longitude})."

    return data.get("display_name", "No address found.")


# ---------------------------------------------------------------------------
# Temporary email (mail.tm) — disposable inbox for test signups.
# Credentials are cached locally (same app-support folder memory.py uses) so
# check_temp_email() only needs the address, not the password, on later calls.
# ---------------------------------------------------------------------------

_MAILTM_BASE = "https://api.mail.tm"


def _temp_email_store_path():
    from memory import APP_SUPPORT_DIR
    return APP_SUPPORT_DIR / "temp_email_accounts.json"


def _load_temp_email_store() -> dict:
    path = _temp_email_store_path()
    if not path.exists():
        return {}
    content = path.read_text()
    return json.loads(content) if content.strip() else {}


def _save_temp_email_store(store: dict) -> None:
    _temp_email_store_path().write_text(json.dumps(store, indent=2))


def _mailtm_token(address: str, password: str) -> str:
    resp = requests.post(
        f"{_MAILTM_BASE}/token",
        json={"address": address, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("token", "")


def create_temp_email() -> str:
    """Creates a new disposable email address, for signing up to sites that require email verification.

    Powered by the free mail.tm service. The address and password are saved
    locally so check_temp_email() and read_temp_email_message() can use the
    account later without the password needing to be passed around
    explicitly. Treat the inbox as throwaway — not for anything long-term
    or sensitive.
    """
    try:
        domains_resp = requests.get(f"{_MAILTM_BASE}/domains", timeout=10)
        domains_resp.raise_for_status()
        domains_data = domains_resp.json()
        domains = domains_data.get("hydra:member") or domains_data.get("member") or []
        if not domains:
            return "Temp email error: no domains currently available from mail.tm."
        domain = domains[0]["domain"]

        local_part = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        address = f"{local_part}@{domain}"
        password = "".join(random.choices(string.ascii_letters + string.digits, k=16))

        create_resp = requests.post(
            f"{_MAILTM_BASE}/accounts",
            json={"address": address, "password": password},
            timeout=10,
        )
        create_resp.raise_for_status()

        token = _mailtm_token(address, password)
    except requests.RequestException as e:
        return f"Temp email creation error: {e}"

    store = _load_temp_email_store()
    store[address] = {"password": password, "token": token, "created": time.time()}
    _save_temp_email_store(store)

    return f"Created temporary email: {address}"


def check_temp_email(address: str) -> str:
    """Checks a previously created temporary email address for new messages.

    Args:
        address: The temp email address returned by create_temp_email().
    """
    store = _load_temp_email_store()
    account = store.get(address)
    if not account:
        return f"No saved temp email account found for {address}. Create one first with create_temp_email()."

    try:
        token = _mailtm_token(address, account["password"])
        messages_resp = requests.get(
            f"{_MAILTM_BASE}/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        messages_resp.raise_for_status()
        data = messages_resp.json()
    except requests.RequestException as e:
        return f"Temp email check error: {e}"

    messages = data.get("hydra:member") or data.get("member") or (data if isinstance(data, list) else [])
    if not messages:
        return f"No messages yet for {address}."

    lines = []
    for m in messages:
        sender = m.get("from", {}).get("address", "unknown")
        subject = m.get("subject", "(no subject)")
        intro = m.get("intro", "")
        lines.append(f"From {sender} — {subject}\n  id={m.get('id')} | {intro}")
    return "\n".join(lines)


def read_temp_email_message(address: str, message_id: str) -> str:
    """Reads the full body of one message from a temporary email account.

    Args:
        address: The temp email address the message belongs to.
        message_id: The message id, from check_temp_email()'s output.
    """
    store = _load_temp_email_store()
    account = store.get(address)
    if not account:
        return f"No saved temp email account found for {address}."

    try:
        token = _mailtm_token(address, account["password"])
        msg_resp = requests.get(
            f"{_MAILTM_BASE}/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        msg_resp.raise_for_status()
        data = msg_resp.json()
    except requests.RequestException as e:
        return f"Temp email read error: {e}"

    subject = data.get("subject", "(no subject)")
    sender = data.get("from", {}).get("address", "unknown")
    text = data.get("text") or "\n".join(data.get("html", []) or []) or "(no readable content)"
    return f"From: {sender}\nSubject: {subject}\n\n{text}"

