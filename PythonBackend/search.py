"""
search.py -- web search for the search_web tool: find sources, read the
best few, return the passages that answer the question.

    1. results   MacBridge /web/search -- DuckDuckGo loaded in an off-screen
                 WebKit view, which search engines treat like a person
                 rather than a script. A self-hosted SearXNG is used first
                 only if JUGNU_SEARXNG_URL points at one; Wikipedia's API is
                 the last resort.
    2. read      the top pages, fetched in parallel on a short budget, main
                 text extracted
    3. excerpts  from each page, the passages that share the most words with
                 the question, plus the search snippet

No model calls and no embeddings: the agent that called search_web reads
the excerpts and writes the answer. (The previous pipeline spent two extra
model calls per search -- query rewriting and reranking -- and depended on a
local SearXNG server that usually wasn't running, so most searches failed.)

Public API: search_web(query) -> SearchResult, answer_from_search(...).
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PAGES_TO_READ = 4
PAGE_TIMEOUT = 4.0          # seconds per page
READ_BUDGET = 6.0           # seconds for all pages together
MAX_PAGE_BYTES = 1_500_000
PASSAGES_PER_PAGE = 2
PASSAGE_CHARS = 480
MAX_OUTPUT_CHARS = 3600     # what the agent reads, all sources together

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})

_STOP = set("""a an and are as at be by can could did do does for from had has have how i if in into is it its
me my of on or our should so than that the their them then there these they this to was we were what when
where which who whom why will with would you your about latest current today""".split())


# ---------------------------------------------------------------------------
# Result types (what the agent sees)
# ---------------------------------------------------------------------------

@dataclass
class SearchSource:
    title: str
    url: str
    text: str
    dates: list[date]

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "text": self.text,
                "dates": [d.isoformat() for d in self.dates]}


@dataclass
class SearchResult:
    query: str
    sources: list[SearchSource]
    error: Optional[str] = None

    def ok(self) -> bool:
        return self.error is None and bool(self.sources)

    def to_dict(self) -> dict:
        if not self.ok():
            return {"error": self.error or "No sources found", "sources": []}
        return {"query": self.query, "sources": [s.to_dict() for s in self.sources]}

    def to_agent_string(self) -> str:
        if not self.ok():
            return f"SEARCH_FAILED: {self.error or 'no sources'}"
        parts = []
        for i, s in enumerate(self.sources, 1):
            dated = f" ({', '.join(d.isoformat() for d in s.dates[:2])})" if s.dates else ""
            parts.append(f"[{i}] {s.title}{dated}\n{s.url}\n{s.text}")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 1. Finding results
# ---------------------------------------------------------------------------

def _macbridge_results(query: str, limit: int) -> list[dict]:
    from tools.macbridge import call
    r = call("/web/search", {"query": query, "limit": limit}, timeout=40, session="primary")
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or "web search failed")
    return [{"url": x["url"], "title": x.get("title", ""), "snippet": x.get("snippet", "")}
            for x in r.get("results", []) if x.get("url")]


def _searxng_results(query: str, limit: int) -> list[dict]:
    base = os.environ.get("JUGNU_SEARXNG_URL", "").rstrip("/")
    if not base:
        return []
    resp = _SESSION.get(f"{base}/search", params={"q": query, "format": "json"}, timeout=5)
    resp.raise_for_status()
    return [{"url": x["url"], "title": x.get("title", ""), "snippet": x.get("content", "")}
            for x in resp.json().get("results", [])[:limit] if x.get("url")]


def _wikipedia_results(query: str, limit: int) -> list[dict]:
    resp = _SESSION.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"},
        headers={"User-Agent": "Jugnu/1.0 (personal assistant)"}, timeout=8)
    resp.raise_for_status()
    return [{"url": "https://en.wikipedia.org/wiki/" + h["title"].replace(" ", "_"), "title": h["title"],
             "snippet": BeautifulSoup(h.get("snippet", ""), "html.parser").get_text(" ", strip=True)}
            for h in resp.json().get("query", {}).get("search", [])]


def find_results(query: str, limit: int = 8) -> tuple[list[dict], str]:
    """Search results from the first backend that returns any."""
    errors = []
    for name, backend in (("searxng", _searxng_results), ("web", _macbridge_results),
                          ("wikipedia", _wikipedia_results)):
        try:
            results = backend(query, limit)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        if results:
            return results, ""
    return [], "; ".join(errors) or "no results"


# ---------------------------------------------------------------------------
# 2. Reading pages
# ---------------------------------------------------------------------------

_BOILERPLATE = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe")


def _page_text(url: str) -> tuple[str, list[date]]:
    """Main readable text of a page, and any publication date it declares."""
    resp = _SESSION.get(url, timeout=PAGE_TIMEOUT, stream=True)
    if resp.status_code >= 400 or "html" not in resp.headers.get("Content-Type", "html"):
        return "", []
    raw = resp.raw.read(MAX_PAGE_BYTES, decode_content=True)
    soup = BeautifulSoup(raw, "html.parser")
    published = []
    for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"itemprop": "datePublished"}):
        tag = soup.find("meta", attrs)
        if tag and tag.get("content"):
            published = extract_dates(tag["content"][:30])
            if published:
                break
    for tag in soup(list(_BOILERPLATE)):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [el.get_text(" ", strip=True) for el in main.find_all(["p", "li", "h1", "h2", "h3", "td"])]
    return "\n".join(line for line in lines if len(line) > 40)[:60_000], published


def _read_pages(results: list[dict]) -> dict:
    pages = {}
    pool = ThreadPoolExecutor(max_workers=PAGES_TO_READ)
    futures = {pool.submit(_page_text, r["url"]): r["url"] for r in results[:PAGES_TO_READ]}
    done, _ = wait(futures, timeout=READ_BUDGET)
    for future in done:
        try:
            pages[futures[future]] = future.result()
        except Exception as e:
            logger.info("couldn't read %s: %s", futures[future], e)
    pool.shutdown(wait=False, cancel_futures=True)
    return pages


# ---------------------------------------------------------------------------
# 3. Picking passages
# ---------------------------------------------------------------------------

def _terms(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def best_passages(text: str, query: str, count: int = PASSAGES_PER_PAGE) -> list[str]:
    """The `count` stretches of `text` sharing the most words with the query."""
    wanted = _terms(query)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    windows, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) > PASSAGE_CHARS and current:
            windows.append(current.strip())
            current = ""
        current += " " + sentence
    if current.strip():
        windows.append(current.strip())
    scored = []
    for i, window in enumerate(windows):
        overlap = len(wanted & _terms(window))
        if overlap:
            has_number = 0.5 if re.search(r"\d", window) else 0
            scored.append((overlap + has_number, -i, window))
    scored.sort(reverse=True)
    return [w for _, _, w in scored[:count]]


_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def extract_dates(text: str) -> list[date]:
    out = []
    for y, m, d in re.findall(r"\b(20\d\d)-(\d\d)-(\d\d)", text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for mon, d, y in re.findall(r"\b([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}),? (20\d\d)", text):
        if mon.lower() in _MONTHS:
            try:
                out.append(date(int(y), _MONTHS.index(mon.lower()) + 1, int(d)))
            except ValueError:
                pass
    return sorted(set(out), reverse=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_web(query: str) -> SearchResult:
    """Structured evidence for the agent -- never a synthesized answer."""
    results, error = find_results(query)
    if not results:
        return SearchResult(query=query, sources=[], error=f"No results ({error})")
    pages = _read_pages(results)
    sources, used = [], 0
    for r in results[:6]:
        text, published = pages.get(r["url"], ("", []))
        snippet = r.get("snippet", "")
        passages = [p for p in (best_passages(text, query) if text else []) if p[:80] not in snippet]
        body = "\n".join(filter(None, [snippet] + [f"… {p}" for p in passages]))
        if not body:
            continue
        body = body[: max(200, MAX_OUTPUT_CHARS - used)]
        used += len(body)
        sources.append(SearchSource(title=r["title"] or r["url"], url=r["url"], text=body,
                                    dates=published or extract_dates(body)))
        if used >= MAX_OUTPUT_CHARS:
            break
    if not sources:
        return SearchResult(query=query, sources=[], error="Results had no readable content")
    return SearchResult(query=query, sources=sources)


def answer_from_search(query: str, result: Optional[SearchResult] = None) -> str:
    """A short answer written from search evidence (one model call). Only for
    callers that need prose rather than sources, e.g. proactive nudges."""
    result = result or search_web(query)
    if not result.ok():
        return f"SEARCH_FAILED: {result.error or 'no sources'}"
    from memory import persona_reply
    return persona_reply(f"Question: {query}\n\nSources:\n{result.to_agent_string()}\n\n"
                         "Answer briefly from these sources only, citing the links you used.", role="EASY")
