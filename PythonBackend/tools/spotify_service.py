"""
spotify_service.py -- resolving a song name to a real Spotify track URI.

WHY THIS EXISTS
---------------
Spotify's AppleScript dictionary can play a URI but has no search command,
and `play track` accepts only a real track/album/playlist URI. The old
play_song sent `spotify:search:<song> <artist>`, which is not a playable
URI at all. Spotify silently ignores URIs it can't resolve AND still
returns success to AppleScript, so the tool reported

    "Playing 'Blinding Lights' by The Weeknd on Spotify"

on every single call while nothing ever played. Verified: after such a
call, `name of current track` raises because nothing is loaded.

Driving Spotify's UI instead isn't an option either -- it's an Electron
app that publishes 253 accessibility elements and zero named controls, so
there is nothing to find or click.

That leaves the Web API, which is why this module exists.

CREDENTIALS
-----------
Uses the Client Credentials flow: an app-level token, no user login, no
redirect, no browser. Search is a public catalogue endpoint, so this is
all it needs.

  1. https://developer.spotify.com/dashboard -> Create app (any name)
  2. Copy the Client ID and Client Secret
  3. export SPOTIFY_CLIENT_ID=...  SPOTIFY_CLIENT_SECRET=...

Takes about two minutes and costs nothing. Without it, resolution is
impossible and the tools say so plainly rather than failing vaguely.
"""

from __future__ import annotations

import base64
import os
import threading
import time

import requests

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"

# Cached app token. These last an hour; re-fetching one per search would add
# a needless round trip to every play.
_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = threading.Lock()


class SpotifyNotConfigured(RuntimeError):
    """Raised when no client credentials are available. Carries the setup
    steps, because 'not configured' is only useful if it says what to do."""

    def __init__(self) -> None:
        super().__init__(
            "Spotify search needs API credentials, which take about two minutes to set "
            "up and cost nothing:\n"
            "  1. Open https://developer.spotify.com/dashboard and create an app\n"
            "  2. Copy its Client ID and Client Secret\n"
            "  3. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in the environment "
            "Jugnu launches with, then restart the backend\n"
            "Without them a song NAME cannot be turned into a playable Spotify URI -- "
            "Spotify's own AppleScript has no search command. Controlling playback "
            "(pause, skip, volume, what's playing) works without any of this."
        )


def _credentials() -> tuple[str, str]:
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise SpotifyNotConfigured()
    return cid, secret


def _access_token() -> str:
    """App token, cached until shortly before it expires."""
    global _token, _token_expires_at
    with _token_lock:
        if _token and time.time() < _token_expires_at - 30:
            return _token

        cid, secret = _credentials()
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        resp = requests.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code == 400:
            raise RuntimeError(
                "Spotify rejected the client credentials. Check SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET match an app on developer.spotify.com."
            )
        resp.raise_for_status()
        payload = resp.json()
        _token = payload["access_token"]
        _token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        return _token


def search_tracks(query: str, limit: int = 5) -> list[dict]:
    """Search the public catalogue. Returns the fields needed to pick a
    track and play it, newest-relevance first as Spotify ranks them."""
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
        for t in items
        if t.get("uri")
    ]


def resolve_track(song: str, artist: str = "") -> dict:
    """Best single match for a song, optionally narrowed by artist.

    Uses Spotify's field filters (`track:` / `artist:`) when an artist is
    given, which is markedly more accurate than concatenating the two into
    a free-text query -- "Hello Adele" as loose text happily matches a
    track called "Hello" by someone else entirely.
    """
    song = (song or "").strip()
    if not song:
        raise ValueError("song is required")

    artist = (artist or "").strip()
    attempts = []
    if artist:
        attempts.append(f'track:"{song}" artist:"{artist}"')
        attempts.append(f"{song} {artist}")
    attempts.append(song)

    for query in attempts:
        results = search_tracks(query, limit=5)
        if results:
            return results[0]

    raise LookupError(
        f"Spotify has no track matching {song!r}"
        + (f" by {artist!r}" if artist else "")
        + ". Check the spelling, or try without the artist."
    )
