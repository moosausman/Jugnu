"""Spotify: choosing the right result, caching, background playback wiring."""

import _env  # noqa: F401

import unittest

from tools import spotify_service as sp
import tools.tools as t


class PickBestTests(unittest.TestCase):
    def test_prefers_exact_title_over_remix_and_covers(self):
        results = [
            {"uri": "spotify:track:A", "name": "Blinding Lights (Remix)", "artist": "The Weeknd, ROSALÍA"},
            {"uri": "spotify:track:B", "name": "Blinding Lights", "artist": "The Weeknd"},
            {"uri": "spotify:track:C", "name": "Blinding Lights", "artist": "Rockabye Baby!"},
        ]
        self.assertEqual(sp.pick_best(results, "blinding lights", "the weeknd")["uri"], "spotify:track:B")
        self.assertEqual(sp.pick_best(results, "blinding lights")["uri"], "spotify:track:B")

    def test_keeps_spotify_ranking_on_ties(self):
        results = [{"uri": "spotify:track:1", "name": "Espresso", "artist": "Sabrina Carpenter"},
                   {"uri": "spotify:track:2", "name": "Espresso", "artist": "Sabrina Carpenter"}]
        self.assertEqual(sp.pick_best(results, "espresso", "sabrina carpenter")["uri"], "spotify:track:1")

    def test_wrong_artist_is_not_played(self):
        results = [{"uri": "spotify:track:1", "name": "Hello", "artist": "Lionel Richie"}]
        self.assertIsNone(sp.pick_best(results, "hello", "adele"))

    def test_asked_for_remix_gets_remix(self):
        results = [{"uri": "spotify:track:O", "name": "Levitating", "artist": "Dua Lipa"},
                   {"uri": "spotify:track:R", "name": "Levitating (Remix)", "artist": "Dua Lipa"}]
        self.assertEqual(sp.pick_best(results, "levitating remix", "dua lipa")["uri"], "spotify:track:R")

    def test_plain_title_beats_edition_unless_asked(self):
        # Spotify's own search ranks the deluxe edition first for "after hours the weeknd".
        results = [{"uri": "spotify:album:D", "name": "After Hours (Deluxe)", "artist": "The Weeknd"},
                   {"uri": "spotify:album:P", "name": "After Hours", "artist": "The Weeknd"}]
        self.assertEqual(sp.pick_best(results, "after hours", "the weeknd", "album")["uri"], "spotify:album:P")
        self.assertEqual(sp.pick_best(results, "after hours deluxe", "the weeknd", "album")["uri"], "spotify:album:D")


class ResolveAndPlayTests(unittest.TestCase):
    def setUp(self):
        self._search, self._call = sp.search_spotify, t.call
        self.calls = []
        sp.search_spotify = lambda query, kind="track", limit=5: [
            {"uri": f"spotify:{kind}:X{kind}", "name": "Drake" if kind == "artist" else "One Dance",
             "artist": "Drake", "album": ""}]

        def fake_call(endpoint, args=None, **kw):
            self.calls.append((endpoint, args))
            return {"ok": True, "playing": True, "track": "One Dance", "artist": "Drake", "background": True}
        t.call = fake_call

    def tearDown(self):
        sp.search_spotify, t.call = self._search, self._call

    def test_play_song_resolves_plays_and_caches(self):
        out = t.play_song("one dance", "drake")
        self.assertTrue(out["ok"])
        self.assertEqual(self.calls[-1], ("/spotify/play-uri", {"uri": "spotify:track:Xtrack"}))
        self.assertEqual(out["resolved"]["found_via"], "spotify_search")
        sp.search_spotify = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should use cache"))
        again = t.play_song("One Dance", "Drake")
        self.assertEqual(again["resolved"]["found_via"], "cache")

    def test_play_artist_and_playlist(self):
        self.assertTrue(t.play_artist("drake")["ok"])
        self.assertEqual(self.calls[-1][1]["uri"], "spotify:artist:Xartist")
        self.assertTrue(t.play_playlist("lofi beats")["ok"])
        self.assertEqual(self.calls[-1][1]["uri"], "spotify:playlist:Xplaylist")

    def test_not_found_is_an_error_not_a_guess(self):
        sp.search_spotify = lambda *a, **k: []
        sp._open_database_resolve, orig = (lambda *a, **k: None), sp._open_database_resolve
        sp._search_tracks_via_scrape, orig2 = (lambda *a, **k: []), sp._search_tracks_via_scrape
        try:
            out = t.play_song("zzqx nonexistent song", "nobody")
        finally:
            sp._open_database_resolve, sp._search_tracks_via_scrape = orig, orig2
        self.assertFalse(out["ok"])
        self.assertNotIn(("/spotify/play-uri",), [c[:1] for c in self.calls])


if __name__ == "__main__":
    unittest.main()
