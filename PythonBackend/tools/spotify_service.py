"""
spotify_service.py -- resolving a song name to a real Spotify track URI.

WHY THIS EXISTS
---------------
Spotify's AppleScript dictionary can play a URI but has no search command,
and `play track` accepts only a real track/album/playlist URI. Sending it
`spotify:search:<song> <artist>` doesn't work -- Spotify silently ignores
URIs it can't resolve AND still returns success to AppleScript, so a caller
checking only the exit status gets a false positive every time.

Driving Spotify's UI instead isn't an option either -- it's an Electron app
that publishes 253 accessibility elements and zero named controls, so
there's nothing to find or click. AppleScript genuinely cannot search
Spotify; this module exists to do that resolution somewhere else, once,
before Spotify.swift ever gets involved.

TWO WAYS TO RESOLVE A NAME, TRIED IN THIS ORDER
------------------------------------------------
1. Spotify's own Web API (Client Credentials flow), *only if*
   SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are set. Official, accurate,
   rate-limit-friendly. Nothing here requires it anymore, but it's used
   automatically when present because it's the more reliable path.

2. A credential-free fallback, used whenever those env vars are absent:
   search the web for the matching open.spotify.com/track link, then read
   that track's own public page for its title/artist/album. No dashboard
   account, no keys, nothing to configure -- it works the moment this file
   is dropped in.

   This leans on two things that are public by design, not reverse-engineered:
     - a plain web search (DuckDuckGo's no-JS HTML results page)
     - the Open Graph / JSON-LD metadata Spotify puts on every track page
       so Slack/iMessage/etc. can render a link preview without login
   Deliberately NOT used: the various "anonymous open.spotify.com token"
   tricks floating around. Spotify has been actively hardening that
   endpoint (it now wants a TOTP computed from a secret embedded in their
   web player JS), which makes it exactly the kind of thing that reports
   success today and breaks silently next month. Scraping a public page's
   preview metadata doesn't fight that arms race.

   Trade-off, stated plainly: this depends on page markup and a search
   engine's HTML rather than a supported API, so treat it as
   good-for-personal-use, not bulletproof. If Spotify reshuffles its page
   markup or the search backend starts blocking scripted requests, this
   can start returning fewer/no results without warning. If that starts
   happening a lot, grabbing free credentials (path 1) is the fix -- see
   CREDENTIALS below. Either way, Spotify.swift still verifies every play
   by reading the player state back, so a bad resolution shows up as a
   clear "not playing" error rather than a silent no-op.

CREDENTIALS (OPTIONAL)
-----------------------
Only needed if you want the more accurate/durable path:
  1. https://developer.spotify.com/dashboard -> Create app (any name)
  2. Copy the Client ID and Client Secret
  3. export SPOTIFY_CLIENT_ID=...  SPOTIFY_CLIENT_SECRET=...
Takes about two minutes and costs nothing. Without it, search still works
via the fallback above.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from urllib.parse import unquote

import requests

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]{22})")
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_TITLE_TAG_RE = re.compile(r"<title>([^<]*)</title>")

# A plain desktop-browser UA. Not spoofing anything beyond "this is a
# browser, not a bare script" -- the pages fetched here are the same ones
# a browser gets with no login.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.4 Safari/605.1.15"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Cached app token for path 1. These last an hour; re-fetching one per
# search would add a needless round trip to every play.
_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = threading.Lock()


class SpotifyLookupError(RuntimeError):
    """A search failure that isn't 'no matching track' -- the search
    backend or a track page couldn't be reached, or credentials that were
    explicitly set turned out to be wrong."""


# Old name, kept as an alias so existing callers (tools.py does
# `except SpotifyNotConfigured`) keep working unmodified. Nothing is
# actually *unconfigured* any more -- both paths work with zero setup --
# this now only fires if SPOTIFY_CLIENT_ID/SECRET are set but wrong.
SpotifyNotConfigured = SpotifyLookupError


def _has_credentials() -> bool:
    return bool(os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
                and os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip())


# ─────────────────────────── Path 1: official API ───────────────────────

def _access_token() -> str:
    """App token via Client Credentials. Only called when both env vars
    are set -- see _has_credentials."""
    global _token, _token_expires_at
    with _token_lock:
        if _token and time.time() < _token_expires_at - 30:
            return _token

        import base64
        cid = os.environ["SPOTIFY_CLIENT_ID"].strip()
        secret = os.environ["SPOTIFY_CLIENT_SECRET"].strip()
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        resp = requests.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code == 400:
            raise SpotifyLookupError(
                "Spotify rejected SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET. Check "
                "they match an app on developer.spotify.com, or unset them "
                "entirely to use the credential-free search instead."
            )
        resp.raise_for_status()
        payload = resp.json()
        _token = payload["access_token"]
        _token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        return _token


def _search_tracks_via_api(query: str, limit: int) -> list[dict]:
    token = _access_token()
    resp = requests.get(
        _SEARCH_URL,
        params={"q": query, "type": "track", "limit": max(1, min(limit, 20))},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("tracks", {}).get("items", [])
    return [
        {
            "name": t.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in t.get("artists", [])),
            "album": t.get("album", {}).get("name", ""),
            "uri": t.get("uri", ""),
            "popularity": t.get("popularity", 0),
        }
        for t in items if t.get("uri")
    ]


# ───────────────────── Path 2: credential-free fallback ─────────────────

def _candidate_track_ids(query: str, limit: int) -> list[str]:
    """Finds open.spotify.com/track/<id> links for the query via
    DuckDuckGo's no-JS HTML results page -- a public search results page,
    not an API, so there's nothing to register or authenticate."""
    resp = requests.get(
        _DDG_HTML_URL,
        params={"q": f"{query} site:open.spotify.com/track"},
        headers=_BROWSER_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    page = unquote(resp.text)
    ids: list[str] = []
    for match in _TRACK_ID_RE.finditer(page):
        track_id = match.group(1)
        if track_id not in ids:
            ids.append(track_id)
        if len(ids) >= limit:
            break
    return ids


def _meta_tag(page: str, prop: str) -> str:
    pattern = re.escape(prop)
    m = (re.search(rf'<meta[^>]+property="{pattern}"[^>]+content="([^"]*)"', page)
         or re.search(rf'<meta[^>]+content="([^"]*)"[^>]+property="{pattern}"', page))
    return html.unescape(m.group(1)) if m else ""


def _artist_name(by_artist) -> str:
    if isinstance(by_artist, list):
        by_artist = by_artist[0] if by_artist else {}
    if isinstance(by_artist, dict):
        return by_artist.get("name", "")
    if isinstance(by_artist, str):
        return by_artist
    return ""


def _track_metadata(track_id: str) -> dict | None:
    """Reads the track's own public page -- no login needed, since this is
    exactly what Spotify serves so chat apps can render a link preview.
    Tries three independent parsing strategies, most-structured first, so
    a change to one doesn't take the whole thing down."""
    url = f"https://open.spotify.com/track/{track_id}"
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    page = resp.text

    # Tier 1: embedded JSON-LD (schema.org MusicRecording) -- structured,
    # least likely to shift wording.
    for block in _LD_JSON_RE.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        types = data.get("@type")
        if types == "MusicRecording" or (isinstance(types, list) and "MusicRecording" in types):
            name = data.get("name", "")
            if name:
                return {
                    "name": name,
                    "artist": _artist_name(data.get("byArtist")),
                    "album": (data.get("inAlbum") or {}).get("name", "") if isinstance(data.get("inAlbum"), dict) else "",
                    "uri": f"spotify:track:{track_id}",
                    "popularity": 0,
                }

    # Tier 2: Open Graph tags, present for link-preview purposes.
    title = _meta_tag(page, "og:title")
    if title:
        description = _meta_tag(page, "og:description")  # usually "Artist · Song · Year"
        artist = description.split("·")[0].strip() if "·" in description else ""
        return {"name": title, "artist": artist, "album": "",
                "uri": f"spotify:track:{track_id}", "popularity": 0}

    # Tier 3: the plain <title> tag, e.g. "Song - song and lyrics by Artist | Spotify".
    m = _TITLE_TAG_RE.search(page)
    if m:
        raw = html.unescape(m.group(1))
        raw = re.sub(r"\s*\|\s*Spotify\s*$", "", raw)
        m2 = re.match(r"(.+?)\s*-\s*song(?: and lyrics)? by\s+(.+)$", raw, re.I)
        if m2:
            return {"name": m2.group(1).strip(), "artist": m2.group(2).strip(),
                    "album": "", "uri": f"spotify:track:{track_id}", "popularity": 0}

    return None


def _search_tracks_via_scrape(query: str, limit: int) -> list[dict]:
    try:
        ids = _candidate_track_ids(query, limit=max(limit, 5))
    except requests.RequestException as e:
        raise SpotifyLookupError(f"Couldn't reach the search backend: {e}") from e

    results: list[dict] = []
    for track_id in ids:
        meta = _track_metadata(track_id)  # None on any per-track failure; just skip it
        if meta:
            results.append(meta)
        if len(results) >= limit:
            break
    return results


# ───────────── Path 3: open music databases (no keys, no scraping) ───────
#
# Wikidata records Spotify IDs for millions of recordings (P2207), artists
# (P1902) and albums (P2205), behind a public, documented API that doesn't
# rate-limit or CAPTCHA scripted use the way search engines do. Apple's
# iTunes Search API (also public, keyless) turns a loose request -- "that
# weeknd song blinding" -- into a canonical title and artist first. Spotify's
# own oEmbed endpoint then confirms the chosen ID really is that title.
#
# This path is why play_song works again without any setup: the earlier
# search-engine scrape broke whenever DuckDuckGo was unreachable or
# rate-limited (it is, on some networks, entirely).

_WD_API = "https://www.wikidata.org/w/api.php"
_ITUNES = "https://itunes.apple.com/search"
_OEMBED = "https://open.spotify.com/oembed"
_API_HEADERS = {"User-Agent": "Jugnu/1.0 (personal macOS assistant; https://github.com/) python-requests"}
_PROPS = {"track": "P2207", "artist": "P1902", "album": "P2205"}  # playlists aren't in Wikidata

_CACHE_LOCK = threading.Lock()


def _cache_path():
    try:
        from memory import APP_SUPPORT_DIR
        return APP_SUPPORT_DIR / "spotify_cache.json"
    except Exception:
        return None


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)             # (Remastered), [Live]
    text = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _cache_key(kind: str, name: str, artist: str = "") -> str:
    return f"{kind}|{_norm(name)}|{_norm(artist)}"


def _cache_get(key: str) -> dict | None:
    path = _cache_path()
    if not path or not path.exists():
        return None
    with _CACHE_LOCK:
        try:
            return json.loads(path.read_text() or "{}").get(key)
        except (OSError, json.JSONDecodeError):
            return None


def _cache_put(key: str, value: dict) -> None:
    path = _cache_path()
    if not path:
        return
    with _CACHE_LOCK:
        try:
            data = json.loads(path.read_text() or "{}") if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data[key] = value
        if len(data) > 2000:  # keep the most recent entries
            data = dict(list(data.items())[-1500:])
        try:
            path.write_text(json.dumps(data))
        except OSError:
            pass


def _similar(a: str, b: str) -> float:
    """0..1 token-overlap similarity of two normalized names."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    if _norm(a) == _norm(b):
        return 1.0
    return len(ta & tb) / max(len(ta), len(tb))


_SPARQL = "https://query.wikidata.org/sparql"


def _sparql(query: str) -> list[dict]:
    """One SPARQL request, honouring Wikimedia's Retry-After once."""
    for attempt in range(2):
        resp = requests.get(_SPARQL, params={"query": query, "format": "json"},
                            headers={**_API_HEADERS, "Accept": "application/sparql-results+json"}, timeout=6)
        if resp.status_code == 429 and attempt == 0:
            wait = resp.headers.get("retry-after", "3")
            time.sleep(min(float(wait) if wait.replace(".", "").isdigit() else 3.0, 6.0))
            continue
        resp.raise_for_status()
        return resp.json().get("results", {}).get("bindings", [])
    return []


def _sparql_text(text: str) -> str:
    return text.replace("\\", " ").replace('"', " ").strip()


def _wd_candidates(kind: str, name: str) -> list[dict]:
    """Search + Spotify ID + performer names in a single request (Wikidata's
    SPARQL endpoint calls the search API itself), so one lookup costs one
    HTTP round trip instead of three."""
    prop = _PROPS[kind]
    performer = "" if kind == "artist" else """
      OPTIONAL { ?item wdt:P175 ?perf . ?perf rdfs:label ?perfLabel . FILTER(LANG(?perfLabel) = "en") }"""
    query = f"""
    SELECT ?item ?label ?sid ?perfLabel WHERE {{
      SERVICE wikibase:mwapi {{
        bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "Search";
                        mwapi:srsearch "{_sparql_text(name)} haswbstatement:{prop}"; mwapi:srlimit "10" .
        ?item wikibase:apiOutputItem mwapi:title .
      }}
      ?item wdt:{prop} ?sid .
      OPTIONAL {{ ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }}{performer}
    }} LIMIT 60"""
    rows: dict[str, dict] = {}
    for b in _sparql(query):
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        row = rows.setdefault(qid, {"label": b.get("label", {}).get("value", ""), "ids": [], "performers": []})
        sid = b["sid"]["value"]
        if sid not in row["ids"]:
            row["ids"].append(sid)
        perf = b.get("perfLabel", {}).get("value")
        if perf and perf not in row["performers"]:
            row["performers"].append(perf)
    return list(rows.values())


def _oembed_title(kind: str, spotify_id: str) -> str | None:
    try:
        resp = requests.get(_OEMBED, params={"url": f"https://open.spotify.com/{kind}/{spotify_id}"},
                            headers=_API_HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        return resp.json().get("title")
    except (requests.RequestException, ValueError):
        return None


def _itunes_candidates(term: str, entity: str = "song", limit: int = 3) -> list[dict]:
    try:
        resp = requests.get(_ITUNES, params={"term": term, "entity": entity, "limit": limit},
                            headers=_API_HEADERS, timeout=8)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []


def _wd_resolve(kind: str, name: str, artist: str = "") -> dict | None:
    """Best Wikidata match for a track / artist / album, verified against1
    Spotify's own oEmbed title. None if nothing convincing is found."""
    scored = []
    for row in _wd_candidates(kind, name):
        name_score = _similar(name, row["label"]) if row["label"] else 0.0
        if name_score < 0.6:
            continue
        if artist and kind != "artist":
            artist_score = max((_similar(artist, p) for p in row["performers"]), default=0.0)
            if artist_score < 0.5:
                continue
        else:
            artist_score = 0.5
        scored.append((name_score + artist_score, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    for _, row in scored[:3]:
        for sid in row["ids"][:3]:
            title = _oembed_title(kind, sid)
            if title is None:
                continue  # removed from Spotify or unavailable in this region
            if _similar(title, name) >= 0.5 or _similar(title, row["label"]) >= 0.5:
                return {"uri": f"spotify:{kind}:{sid}", "name": title,
                        "artist": row["performers"][0] if row["performers"] else (artist or ""),
                        "album": "", "source": "wikidata"}
    return None


def _open_database_resolve(kind: str, name: str, artist: str = "") -> dict | None:
    """Wikidata directly, then again with Apple's canonical title/artist when
    the request was loose ("blinding lights weeknd", a typo, no artist)."""
    try:
        hit = _wd_resolve(kind, name, artist)
        if hit:
            return hit
    except requests.RequestException:
        return None  # slow or throttled -- a second round would only be slower
    try:
        if kind == "artist":
            for c in _itunes_candidates(name, entity="musicArtist", limit=2):
                hit = _wd_resolve("artist", c.get("artistName", ""))
                if hit:
                    return hit
            return None
        entity = "song" if kind == "track" else "album"
        field = "trackName" if kind == "track" else "collectionName"
        for c in _itunes_candidates(f"{name} {artist}".strip(), entity=entity, limit=3):
            canonical, canon_artist = c.get(field, ""), c.get("artistName", "")
            if not canonical or (artist and _similar(artist, canon_artist) < 0.5):
                continue
            hit = _wd_resolve(kind, canonical, canon_artist)
            if hit:
                return hit
    except requests.RequestException:
        return None
    return None


# ──────────────────────────────── Public API ─────────────────────────────

def search_tracks(query: str, limit: int = 5) -> list[dict]:
    """Search for tracks and return the fields needed to pick one and play
    it. Uses the official Web API when SPOTIFY_CLIENT_ID/SECRET are set;
    otherwise (the default) falls back to the credential-free web search
    + page-read described at the top of this file."""
    query = (query or "").strip()
    if not query:
        return []

    if _has_credentials():
        try:
            return _search_tracks_via_api(query, limit)
        except SpotifyLookupError:
            raise  # credentials are set but wrong -- worth surfacing clearly
        except Exception:
            pass  # network hiccup etc. -- fall through to the free path below

    try:
        results = search_spotify(query, "track", limit)
        if results:
            return results
    except SpotifyLookupError:
        pass
    hit = _open_database_resolve("track", query)
    results = [hit] if hit else []
    try:
        for r in _search_tracks_via_scrape(query, limit):
            if all(r["uri"] != x["uri"] for x in results):
                results.append(r)
    except SpotifyLookupError:
        pass  # search engine unreachable -- the open-database result stands on its own
    return results[:limit]


def resolve_track(song: str, artist: str = "") -> dict:
    """Best single match for a song, optionally narrowed by artist.

    When the official API is available, uses Spotify's field filters
    (`track:` / `artist:`), which are markedly more accurate than
    concatenating the two into free text -- "Hello Adele" as loose text
    happily matches a track called "Hello" by someone else entirely. The
    credential-free path can't use those filters (they're an API-only
    syntax), so it relies on search-engine relevance instead.
    """
    song = (song or "").strip()
    if not song:
        raise ValueError("song is required")
    artist = (artist or "").strip()
    return _resolve("track", song, artist)


def resolve_artist(artist: str) -> dict:
    """Spotify URI for an artist ("play some Drake")."""
    artist = (artist or "").strip()
    if not artist:
        raise ValueError("artist is required")
    return _resolve("artist", artist)


def resolve_playlist(name: str) -> dict:
    """Spotify URI for a public playlist by name ("lofi beats", "Today's Top Hits")."""
    name = (name or "").strip()
    if not name:
        raise ValueError("playlist name is required")
    return _resolve("playlist", name)


def resolve_album(album: str, artist: str = "") -> dict:
    """Spotify URI for an album."""
    album = (album or "").strip()
    if not album:
        raise ValueError("album is required")
    return _resolve("album", album, (artist or "").strip())


def remember_uri(kind: str, name: str, artist: str, result: dict) -> None:
    """Called after a verified play, so the same request is instant next
    time and works even offline."""
    _cache_put(_cache_key(kind, name, artist), {k: result.get(k, "") for k in ("uri", "name", "artist", "album")})


# ───────── Path 0 (primary): Spotify's own search, via MacBridge ─────────
#
# MacBridge loads open.spotify.com/search off-screen and reads the results
# (see MacBridge's SpotifySearch.swift): Spotify's own ranking, no key, no
# third-party service, ~1.5s. Everything below it is a fallback.

_WEB_TYPES = {"track": "tracks", "artist": "artists", "album": "albums", "playlist": "playlists"}


def search_spotify(query: str, kind: str = "track", limit: int = 5) -> list[dict]:
    """Spotify's own search results for `query` (tracks, artists, albums or
    playlists). Raises SpotifyLookupError if the search couldn't run."""
    from tools.macbridge import call
    try:
        r = call("/spotify/search", {"query": query, "type": _WEB_TYPES[kind], "limit": limit}, timeout=30,
                 session="primary")
    except Exception as e:
        raise SpotifyLookupError(f"Spotify search unavailable: {e}") from e
    if not r.get("ok"):
        if "returned no" in str(r.get("error", "")):
            return []
        raise SpotifyLookupError(str(r.get("error") or "Spotify search failed"))
    return [{"uri": x["uri"], "name": x.get("name", ""), "artist": x.get("artist", ""), "album": ""}
            for x in r.get("results", [])]


def pick_best(results: list[dict], name: str, artist: str = "", kind: str = "track") -> dict | None:
    """Spotify's top result, unless a lower one matches the requested title
    and artist clearly better (e.g. the original over a remix or a cover)."""
    if not results:
        return None
    wanted = _norm(name)

    def full(text: str) -> str:  # like _norm, but keeps "(Remix)", "[Live]" etc.
        return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())

    def score(i_r):
        i, r = i_r
        title = _norm(r.get("name", ""))
        s = 0.0
        exact = title == wanted or full(r.get("name", "")) == full(name)
        overlap = max(_similar(name, r.get("name", "")),
                      len(set(full(name).split()) & set(full(r.get("name", "")).split()))
                      / max(len(set(full(name).split())), len(set(full(r.get("name", "")).split())), 1))
        s += 3.0 if exact else 1.5 * overlap
        if full(r.get("name", "")) == full(name):
            s += 0.5   # "After Hours" over "After Hours (Deluxe)" unless the deluxe was asked for
        if artist:
            s += 2.0 * max((_similar(artist, a) for a in r.get("artist", "").split(",")), default=0.0)
        if kind == "track" and re.search(r"\b(remix|live|karaoke|instrumental|cover|sped up|slowed|8d)\b",
                                         r.get("name", ""), re.I) and not re.search(
                                             r"\b(remix|live|karaoke|instrumental|cover|sped up|slowed)\b", name, re.I):
            s -= 1.0
        return s - 0.15 * i   # keep Spotify's ranking as the tie-breaker
    best_i, best = max(enumerate(results), key=score)
    if artist and max((_similar(artist, a) for a in best.get("artist", "").split(",")), default=0.0) < 0.34 \
            and kind != "artist":
        # Nothing by that artist in the results -- don't silently play someone else.
        return None
    return best


def _resolve(kind: str, name: str, artist: str = "") -> dict:
    cached = _cache_get(_cache_key(kind, name, artist))
    if cached and cached.get("uri"):
        return {**cached, "source": "cache"}

    query = f"{name} {artist}".strip() if kind != "artist" else name
    try:
        best = pick_best(search_spotify(query, kind, limit=8), name, artist, kind)
        if best is None and artist and kind != "artist":
            best = pick_best(search_spotify(name, kind, limit=8), name, artist, kind)
        if best:
            return {**best, "source": "spotify_search"}
    except SpotifyLookupError:
        pass  # MacBridge or open.spotify.com unreachable -- try the fallbacks

    if kind == "track" and _has_credentials():
        attempts = [f'track:"{name}" artist:"{artist}"'] if artist else []
        attempts.append(f"{name} {artist}".strip())
        for query in attempts:
            try:
                results = _search_tracks_via_api(query, 5)
            except SpotifyLookupError:
                raise
            except Exception:
                results = []
            if results:
                return {**results[0], "source": "spotify_api"}

    if kind in _PROPS:
        hit = _open_database_resolve(kind, name, artist)
        if hit:
            return hit

    if kind == "track":
        for query in ([f"{name} {artist}"] if artist else []) + [name]:
            try:
                results = _search_tracks_via_scrape(query, 5)
            except SpotifyLookupError:
                break  # search engine unreachable
            if results:
                return {**results[0], "source": "web_search"}

    raise LookupError(
        f"Couldn't find a Spotify {kind} matching {name!r}"
        + (f" by {artist!r}" if artist else "")
        + ". Check the spelling, add the artist, or give a Spotify link."
    )
