"""
search.py — Jugnu Web Research Pipeline v7
============================================
Speed improvements over v6:
  - Parallel rewrite + first SearXNG query (saves ~1-2s)
  - Direct HTML scrape instead of Jina AI (removes remote hop per page)
  - Early text truncation before chunking (fewer chunks → faster embed)
  - Scrape budget cut: 20 → 12 pages, workers 12 → 12 concurrent (all at once)
  - Snippet-only fallback chunks from SearXNG avoid scraping altogether for simple queries
  - MMR candidate pool trimmed: 50 → 30 (fewer vectors to score)
  - Rerank candidates cut: top_k * 3 → top_k * 2
  - Single embed call with pre-filtered chunk count cap (max 80 chunks)
  - LLM calls use streaming=False and think=False (already set; confirmed)

Pipeline:
  1. classify_query         — intent + time-sensitivity detection
  2. rewrite_queries        — 3-5 domain-aware queries (parallel with search start)
  3. searxng_search         — SearXNG JSON API with HTML fallback, priority domains front-loaded
  4. scrape_pages           — top 12 concurrent, direct BS4 scrape + meta date extraction
  5. sentence_window_chunks — sliding windows, capped at 80 chunks total before embed
  6. embed_and_rank         — title+text embedded, recency optional, MMR diversity filter
  7. python_date_filter     — future chunks sorted in Python, no LLM arithmetic
  8. llm_rerank             — separate model call, rich metadata shown
  9. synthesize             — grounded prompt, no reasoning theatre

Public API:
  search_web(query) -> str
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import numpy as np
import requests
from bs4 import BeautifulSoup

import llm_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    # Model choice used to be hardcoded here (and ignored JUGNU_MODEL
    # entirely -- setting it anywhere else in the app had no effect on
    # search). llm_client now resolves the SEARCH role's model itself
    # (JUGNU_SEARCH_MODEL, falling back to JUGNU_MODEL) and EMBED role's
    # embedding model (JUGNU_EMBED_MODEL) -- see llm_client._local_model_for
    # and default_embed_model(). Nothing here needs to duplicate that.

    # SearXNG
    searxng_url:     str = "http://localhost:8080"
    searxng_timeout: int = 8

    # Search
    num_rewrite_queries: int = 4          # was 5 — one fewer LLM query to generate
    results_per_query:   int = 8
    max_pages_to_scrape: int = 12         # was 20 — cuts scrape wall-time significantly
    max_snippet_chunks:  int = 30         # snippets-only fast path chunk cap

    # Sentence-window chunking
    window_size:     int = 5
    window_step:     int = 3              # was 2 — fewer overlapping windows = fewer chunks
    min_chunk_chars: int = 80             # was 60 — filters more noise early
    max_chunk_chars: int = 800            # new: truncate windows before embed
    max_total_chunks: int = 80            # new: hard cap before embedding

    # Per-page scrape
    max_content_chars: int = 8_000       # new: truncate scraped page before chunking
    scrape_timeout:    int = 5            # was 8 — fail fast on slow pages
    # Hard ceiling on bytes pulled off the wire per page, enforced while
    # streaming (see _scrape_one). 5MB is far more HTML than any article
    # needs -- max_content_chars trims the extracted text to 8k characters
    # regardless -- and it's what stops one oversized or hostile response
    # from being buffered whole, 12 workers at a time.
    max_download_bytes: int = 5 * 1024 * 1024

    # Retrieval
    mmr_candidate_k:    int = 30          # was 50
    mmr_output_k:       int = 12          # was 15
    rerank_top_k:       int = 5
    max_chunks_per_url: int = 2
    mmr_lambda:         float = 0.65

    # Recency (only for time-sensitive queries)
    recency_weight:   float = 0.25
    undated_age_days: int   = 90

    # Network
    max_scrape_workers: int = 12
    min_line_chars:     int = 45


CFG = Config()

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_RES = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\.?,?\s+(\d{4})\b",
        re.IGNORECASE,
    ),
]

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')


def _today() -> date:
    return datetime.now(timezone.utc).date()

def _today_iso() -> str:
    return _today().isoformat()

def _today_str() -> str:
    return _today().strftime("%A, %d %B %Y")


def _parse_date_groups(g: tuple) -> Optional[date]:
    try:
        if g[0].isdigit() and len(g[0]) == 4:
            return date(int(g[0]), int(g[1]), int(g[2]))
        elif g[0][0].isalpha():
            mo = _MONTHS.get(g[0].strip().lower()[:3])
            return date(int(g[2]), mo, int(g[1])) if mo else None
        else:
            mo = _MONTHS.get(g[1].strip().lower()[:3])
            return date(int(g[2]), mo, int(g[0])) if mo else None
    except (ValueError, TypeError):
        return None


def extract_dates(text: str) -> list[date]:
    found: set[date] = set()
    for pat in _DATE_RES:
        for m in pat.finditer(text):
            d = _parse_date_groups(m.groups())
            if d:
                found.add(d)
    return sorted(found)


def recency_score(dates: list[date]) -> float:
    age = abs((_today() - max(dates)).days) if dates else CFG.undated_age_days
    return 0.5 ** (age / 60.0)


# ---------------------------------------------------------------------------
# Stage 1 — Classifier
# ---------------------------------------------------------------------------

_TIME_SENSITIVE_RE = re.compile(
    r"\b(when|next|latest|upcoming|today|tonight|now|current|live|"
    r"price|score|schedule|fixture|result|news|weather|transfer|"
    r"standing|table|ranking|recently|this week|this month)\b",
    re.IGNORECASE,
)

_DOMAIN_PATTERNS = {
    "sports": re.compile(
        r"\b(match|game|fixture|score|goal|player|team|league|cup|tournament|"
        r"football|soccer|tennis|f1|grand prix|nba|nfl|cricket|rugby|"
        r"championship|season|transfer|lineup|squad|world cup)\b",
        re.IGNORECASE,
    ),
    "news": re.compile(
        r"\b(news|war|election|government|politics|president|minister|"
        r"economy|stock|market|crisis|disaster|attack|protest)\b",
        re.IGNORECASE,
    ),
    "tech": re.compile(
        r"\b(python|javascript|code|programming|api|library|framework|"
        r"error|bug|install|github|docker|kubernetes|llm|ai|model)\b",
        re.IGNORECASE,
    ),
    "medical": re.compile(
        r"\b(symptom|disease|drug|medication|treatment|doctor|hospital|"
        r"nhs|health|condition|diagnosis|pain|cancer|diabetes)\b",
        re.IGNORECASE,
    ),
}

_PRIORITY_DOMAINS = {
    "sports":  ["fifa.com", "uefa.com", "espn.com", "bbc.com/sport",
                "flashscore.com", "sofascore.com", "transfermarkt.com",
                "formula1.com", "premierleague.com"],
    "news":    ["bbc.com", "reuters.com", "apnews.com", "theguardian.com"],
    "tech":    ["docs.python.org", "stackoverflow.com", "github.com", "developer.mozilla.org"],
    "medical": ["nhs.uk", "mayoclinic.org", "who.int", "nih.gov"],
}

_AUTHORITATIVE_QUERY = {
    "sports":  "{subject} fixtures schedule {year} site:flashscore.com OR site:sofascore.com OR site:espn.com",
    "news":    "{subject} {year} site:reuters.com OR site:bbc.com",
    "tech":    "{subject} site:stackoverflow.com OR site:github.com",
    "medical": "{subject} site:nhs.uk OR site:mayoclinic.org",
    "general": None,
}


def classify_query(query: str) -> dict:
    domain = "general"
    for d, pat in _DOMAIN_PATTERNS.items():
        if pat.search(query):
            domain = d
            break
    return {
        "domain": domain,
        "time_sensitive": bool(_TIME_SENSITIVE_RE.search(query)),
        "priority_domains": _PRIORITY_DOMAINS.get(domain, []),
    }


def _extract_subject(query: str) -> str:
    q = re.sub(
        r"\b(when|is|are|what|where|the|next|latest|upcoming|match|game|fixture)\b",
        "", query, flags=re.IGNORECASE,
    )
    return " ".join(q.split()).strip() or query


# ---------------------------------------------------------------------------
# Stage 2 — Query rewriting (now overlapped with first search via threading)
# ---------------------------------------------------------------------------

_DOMAIN_INSTRUCTIONS = {
    "sports": (
        "Target: FIFA, UEFA, ESPN, BBC Sport, Flashscore, Sofascore. "
        "Use full official team/competition names. "
        "Include year {year} and terms like 'fixture', 'schedule', 'next match'."
    ),
    "news": (
        "Target: BBC, Reuters, AP News, The Guardian. "
        "Include year {year}, location, and key entity names."
    ),
    "tech": (
        "Target: official docs, Stack Overflow, GitHub. "
        "Use exact version numbers, library names, error messages."
    ),
    "medical": (
        "Target: NHS, Mayo Clinic, WHO, NIH. "
        "Use both clinical and plain-language terms."
    ),
    "general": "Cover different angles. Include year {year} if time-sensitive.",
}

_REWRITE_SYSTEM = """\
You are a search query expert. Generate 3-4 distinct search queries for this question.

{domain_instruction}

Return ONLY valid JSON: {{"queries": ["...", ...]}}
Each query: 3-8 words. No filler. Vary keywords.
""".strip()


def rewrite_queries(query: str, classification: dict) -> list[str]:
    domain = classification["domain"]
    instruction = _DOMAIN_INSTRUCTIONS[domain].format(year=_today().year)

    hardcoded: list[str] = []
    template = _AUTHORITATIVE_QUERY.get(domain)
    if template:
        subject = _extract_subject(query)
        hardcoded = [template.format(subject=subject, year=_today().year)]

    try:
        resp = llm_client.chat(
            think=False,
            role="SEARCH",
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM.format(domain_instruction=instruction)},
                {"role": "user",   "content": f"Today: {_today_iso()}\n\nQuestion: {query}"},
            ],
        )
        raw = re.sub(r"```(?:json)?|```", "", resp["message"]["content"]).strip()
        llm_queries = [q for q in json.loads(raw)["queries"] if q]
    except Exception as exc:
        logger.warning("rewrite_queries failed (%s) — using raw query", exc)
        llm_queries = [query]

    all_queries: list[str] = []
    seen: set[str] = set()
    for q in hardcoded + llm_queries:
        if q not in seen:
            seen.add(q)
            all_queries.append(q)

    return all_queries[: CFG.num_rewrite_queries]


# ---------------------------------------------------------------------------
# Stage 3 — SearXNG search
# ---------------------------------------------------------------------------

def _searxng_query_json(q: str) -> list[dict]:
    resp = _SESSION.get(
        f"{CFG.searxng_url}/search",
        params={"q": q, "format": "json", "pageno": 1},
        headers={"Accept": "application/json"},
        timeout=CFG.searxng_timeout,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("results", [])[: CFG.results_per_query]:
        url = item.get("url", "")
        if url:
            results.append({
                "href":  url,
                "title": item.get("title", ""),
                "body":  item.get("content", ""),
            })
    return results


def _searxng_query_html(q: str) -> list[dict]:
    resp = _SESSION.get(
        f"{CFG.searxng_url}/search",
        params={"q": q, "pageno": 1},
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=CFG.searxng_timeout,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for article in soup.select("article.result")[: CFG.results_per_query]:
        a_tag = article.select_one("h3 a") or article.select_one("a[href]")
        if not a_tag:
            continue
        url = a_tag.get("href", "").strip()
        if not url.startswith("http"):
            continue
        title   = a_tag.get_text(strip=True)
        snippet = article.select_one("p.content") or article.select_one("p")
        body    = snippet.get_text(strip=True) if snippet else ""
        results.append({"href": url, "title": title, "body": body})
    return results


def _searxng_query(q: str) -> list[dict]:
    try:
        r = _searxng_query_json(q)
        logger.info("SearXNG JSON ok: %r → %d", q, len(r))
        return r
    except Exception as e:
        logger.warning("SearXNG JSON failed (%s) — trying HTML", e)
    try:
        r = _searxng_query_html(q)
        logger.info("SearXNG HTML ok: %r → %d", q, len(r))
        return r
    except Exception as e:
        logger.warning("SearXNG HTML failed: %s", e)
        return []


def searxng_search(query: str, classification: dict) -> tuple[list[dict], list[str]]:
    """
    Overlap query rewriting with the first SearXNG call.
    The hardcoded authoritative query fires immediately while the LLM rewrites.
    """
    domain    = classification["domain"]
    priority  = classification.get("priority_domains", [])
    subject   = _extract_subject(query)
    year      = _today().year

    # Fire authoritative query immediately — don't wait for LLM rewrite
    early_future = None
    template = _AUTHORITATIVE_QUERY.get(domain)
    early_query  = template.format(subject=subject, year=year) if template else None

    seen: set[str]      = set()
    priority_hits: list[dict] = []
    other_hits:    list[dict] = []

    def _collect(hits: list[dict]) -> None:
        for hit in hits:
            url = hit.get("href", "")
            if not url or url in seen:
                continue
            seen.add(url)
            if any(p in url for p in priority):
                priority_hits.append(hit)
            else:
                other_hits.append(hit)

    with ThreadPoolExecutor(max_workers=CFG.num_rewrite_queries + 1) as pool:
        # Start authoritative query immediately
        if early_query:
            early_future = pool.submit(_searxng_query, early_query)

        # Start LLM rewrite in parallel
        rewrite_future = pool.submit(rewrite_queries, query, classification)

        # Collect early query result while rewrite is running
        if early_future:
            try:
                _collect(early_future.result())
            except Exception as e:
                logger.warning("Early query error: %s", e)

        # Get remaining queries from LLM rewrite
        try:
            all_queries = rewrite_future.result()
        except Exception:
            all_queries = [query]

        # Filter out already-run early_query
        remaining_queries = [q for q in all_queries if q != early_query]

        # Run remaining queries concurrently
        futures = {pool.submit(_searxng_query, q): q for q in remaining_queries}
        for fut in as_completed(futures):
            try:
                _collect(fut.result())
            except Exception as e:
                logger.warning("SearXNG worker error: %s", e)

    queries_used = ([early_query] if early_query else []) + remaining_queries
    return priority_hits + other_hits, queries_used


# ---------------------------------------------------------------------------
# Stage 4 — Direct HTML scrape (replaces Jina AI)
# ---------------------------------------------------------------------------
# Removes the extra remote hop to r.jina.ai — scrapes directly.
# BeautifulSoup strips boilerplate; meta publish-date extracted from <head>.

_BOILERPLATE_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}

_META_DATE_ATTRS = [
    ("meta", {"property": "article:published_time"}),
    ("meta", {"name": "pubdate"}),
    ("meta", {"name": "date"}),
    ("meta", {"itemprop": "datePublished"}),
    ("time", {"itemprop": "datePublished"}),
]


def _extract_meta_date(soup: BeautifulSoup) -> Optional[date]:
    for tag, attrs in _META_DATE_ATTRS:
        el = soup.find(tag, attrs)
        if el:
            val = el.get("content") or el.get("datetime") or el.get_text()
            if val:
                dates = extract_dates(str(val)[:30])
                if dates:
                    return dates[0]
    return None


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(list(_BOILERPLATE_TAGS)):
        tag.decompose()

    # Prefer <article> or <main> if present
    main = soup.find("article") or soup.find("main") or soup.find("body") or soup
    lines = []
    for el in main.find_all(["p", "h1", "h2", "h3", "h4", "li", "td", "th"]):
        text = el.get_text(" ", strip=True)
        if len(text) >= CFG.min_line_chars:
            lines.append(text)

    return "\n".join(lines)[: CFG.max_content_chars]


def _scrape_one(result: dict) -> Optional[dict]:
    url = result.get("href", "")
    if not url:
        return None

    pub_date: Optional[date] = None
    text = ""

    try:
        # stream=True so headers land before the body does -- that's what
        # makes the content-type and size checks below able to reject a
        # page *without* having already downloaded it. The old version used
        # stream=False, which buffers the entire response first: a
        # multi-hundred-MB file, or anything mislabelled as text/html, was
        # fully read into memory before the content-type check could refuse
        # it, and up to max_scrape_workers (12) of those run concurrently.
        resp = _SESSION.get(
            url,
            timeout=CFG.scrape_timeout,
            allow_redirects=True,
            stream=True,
        )
        try:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "html" not in ct and "text" not in ct:
                raise ValueError(f"Non-HTML content-type: {ct}")

            # Trust Content-Length when it's offered, but never rely on it:
            # it's optional, and a hostile or misconfigured server can
            # simply omit it or lie. The streamed read below is the real
            # bound -- this just avoids starting a download we already know
            # is too big.
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > CFG.max_download_bytes:
                raise ValueError(f"Declared size {declared} bytes exceeds scrape budget")

            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=65_536):
                chunks.append(chunk)
                total += len(chunk)
                if total > CFG.max_download_bytes:
                    # Everything downstream truncates to max_content_chars
                    # anyway, so a page that blows this budget has nothing
                    # to offer that the first few MB didn't -- stop reading
                    # rather than finishing the download to throw it away.
                    logger.debug("Scrape truncated for %s at %d bytes", url, total)
                    break
            raw = b"".join(chunks)
        finally:
            resp.close()

        html = raw.decode(resp.encoding or "utf-8", errors="replace")
        soup     = BeautifulSoup(html, "html.parser")
        pub_date = _extract_meta_date(soup)
        text     = _extract_text(soup)
    except Exception as exc:
        logger.debug("Scrape failed for %s: %s", url, exc)

    # Always fall back to SearXNG snippet — ensures we always have something
    if len(text) < 200:
        text = result.get("body", "")

    if not text:
        return None

    return {
        "title":    result.get("title", urlparse(url).netloc),
        "url":      url,
        "content":  text,
        "pub_date": pub_date,
    }


def scrape_pages(results: list[dict]) -> list[dict]:
    """
    Scrape all pages fully concurrently (all within max_scrape_workers at once).
    Fast-fail timeout (5s) keeps total wall-time bounded to ~5s for the scrape stage.
    """
    pages: list[dict] = []
    targets = results[: CFG.max_pages_to_scrape]
    with ThreadPoolExecutor(max_workers=CFG.max_scrape_workers) as pool:
        futures = {pool.submit(_scrape_one, r): r for r in targets}
        for fut in as_completed(futures):
            try:
                page = fut.result()
                if page:
                    pages.append(page)
            except Exception as exc:
                logger.warning("Scrape worker error: %s", exc)
    return pages


# ---------------------------------------------------------------------------
# Fast path: snippet-only chunks (skips scraping entirely for simple queries)
# ---------------------------------------------------------------------------

def snippets_to_chunks(results: list[dict]) -> list[dict]:
    """
    Build chunks directly from SearXNG snippets.
    Used as a fast fallback if scraping fails or as a preview before scraping completes.
    """
    chunks = []
    for r in results[: CFG.max_snippet_chunks]:
        text = r.get("body", "").strip()
        if len(text) < CFG.min_chunk_chars:
            continue
        title = r.get("title", "")
        chunks.append({
            "embed_text": f"{title}\n{text}",
            "text":       text,
            "source":     title,
            "url":        r.get("href", ""),
            "dates":      extract_dates(text),
            "pub_date":   None,
        })
    return chunks


# ---------------------------------------------------------------------------
# Stage 5 — Sentence-window chunking with hard cap
# ---------------------------------------------------------------------------

def _sentence_windows(text: str) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    if len(sentences) <= CFG.window_size:
        chunk = " ".join(sentences)
        return [chunk[: CFG.max_chunk_chars]] if len(chunk) >= CFG.min_chunk_chars else []
    windows: list[str] = []
    i = 0
    while i < len(sentences):
        w = " ".join(sentences[i: i + CFG.window_size])
        if len(w) >= CFG.min_chunk_chars:
            windows.append(w[: CFG.max_chunk_chars])
        i += CFG.window_step
    return windows


def pages_to_chunks(pages: list[dict], budget: int = 0) -> list[dict]:
    """
    Sentence-window chunk each page.
    budget: if > 0, stop generating chunks once this many are accumulated.
    Falls back to per-page cap when no budget given.
    """
    chunks: list[dict] = []
    hard_cap = budget or CFG.max_total_chunks

    for page in pages:
        if len(chunks) >= hard_cap:
            break
        title    = page["title"]
        pub_date = page.get("pub_date")
        for window in _sentence_windows(page["content"]):
            if len(chunks) >= hard_cap:
                break
            body_dates = extract_dates(window)
            all_dates  = body_dates if body_dates else ([pub_date] if pub_date else [])
            chunks.append({
                "embed_text": f"{title}\n{window}",
                "text":       window,
                "source":     title,
                "url":        page["url"],
                "dates":      all_dates,
                "pub_date":   pub_date,
            })
    return chunks


# ---------------------------------------------------------------------------
# Stage 6 — Embed + MMR
# ---------------------------------------------------------------------------

def _cosine(a, b) -> float:
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    return float(np.dot(va, vb) / (na * nb)) if na and nb else 0.0


def _mmr(
    q_emb: list[float],
    embs: list[list[float]],
    chunks: list[dict],
    k: int,
    lmbda: float,
) -> list[dict]:
    if not chunks:
        return []
    q  = np.array(q_emb, dtype=np.float32)
    es = [np.array(e, dtype=np.float32) for e in embs]
    relevances   = [_cosine(q, e) for e in es]
    remaining    = list(range(len(chunks)))
    selected_i:  list[int]        = []
    selected_e:  list[np.ndarray] = []
    url_counts:  dict[str, int]   = defaultdict(int)

    while remaining and len(selected_i) < k:
        best_idx, best_score = None, -float("inf")
        for idx in remaining:
            rel = relevances[idx]
            red = max((_cosine(es[idx], se) for se in selected_e), default=0.0)
            score = lmbda * rel - (1 - lmbda) * red
            if score > best_score:
                best_score, best_idx = score, idx
        if best_idx is None:
            break
        url = chunks[best_idx]["url"]
        if url_counts[url] < CFG.max_chunks_per_url:
            selected_i.append(best_idx)
            selected_e.append(es[best_idx])
            url_counts[url] += 1
        remaining.remove(best_idx)

    return [chunks[i] for i in selected_i]


def embed_and_rank(query: str, chunks: list[dict], time_sensitive: bool) -> list[dict]:
    if not chunks:
        return []
    # Hard cap before sending to embed model — keeps latency predictable
    chunks = chunks[: CFG.max_total_chunks]
    try:
        resp = llm_client.embed(
            input=[query] + [c["embed_text"] for c in chunks],
            role="EMBED",
        )
    except Exception as exc:
        logger.warning("Embed failed: %s", exc)
        return []

    embeddings = resp["embeddings"]
    q_emb      = embeddings[0]
    chunk_embs = embeddings[1:]

    scored: list[tuple[float, int]] = []
    for i, (chunk, emb) in enumerate(zip(chunks, chunk_embs)):
        sim = _cosine(q_emb, emb)
        if time_sensitive:
            rec   = recency_score(chunk["dates"])
            score = (1 - CFG.recency_weight) * sim + CFG.recency_weight * rec
        else:
            score = sim
        scored.append((score, i))

    scored.sort(reverse=True)
    top_i    = [i for _, i in scored[: CFG.mmr_candidate_k]]
    top_c    = [chunks[i] for i in top_i]
    top_embs = [chunk_embs[i] for i in top_i]

    return _mmr(q_emb, top_embs, top_c, CFG.mmr_output_k, CFG.mmr_lambda)


# ---------------------------------------------------------------------------
# Stage 7 — Python date filter
# ---------------------------------------------------------------------------

def python_date_filter(chunks: list[dict], time_sensitive: bool) -> list[dict]:
    if not time_sensitive:
        return chunks

    today  = _today()
    future: list[tuple[date, dict]] = []
    past:   list[tuple[date, dict]] = []

    for chunk in chunks:
        future_dates = [d for d in chunk["dates"] if d >= today]
        past_dates   = [d for d in chunk["dates"] if d < today]
        if future_dates:
            future.append((min(future_dates), chunk))
        elif past_dates:
            past.append((max(past_dates), chunk))

    if future:
        future.sort(key=lambda x: x[0])
        return [c for _, c in future][: CFG.rerank_top_k * 2]   # was * 3

    past.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in past][: CFG.rerank_top_k * 2]


# ---------------------------------------------------------------------------
# Stage 8 — LLM reranker
# ---------------------------------------------------------------------------

_RERANK_SYSTEM = """\
You are a search result reranker. Today is {today}.
Select the {k} excerpts that BEST answer the question.
Prioritise: direct specific answer > authoritative source > non-redundant.
Return ONLY valid JSON: {{"selected": [2, 0, 4, ...]}}
""".strip()


def llm_rerank(query: str, candidates: list[dict]) -> list[dict]:
    if len(candidates) <= CFG.rerank_top_k:
        return candidates

    numbered = "\n\n".join(
        (
            f"[{i}]\nSource: {c['source']}\nURL: {c['url']}\n"
            f"Dates: {', '.join(d.isoformat() for d in c['dates']) or 'none'}\n"
            f"Text: {c['text'][:400]}"       # was 500 — slightly smaller prompt
        )
        for i, c in enumerate(candidates)
    )

    try:
        resp = llm_client.chat(
            think=False,
            role="SEARCH",
            messages=[
                {"role": "system", "content": _RERANK_SYSTEM.format(today=_today_iso(), k=CFG.rerank_top_k)},
                {"role": "user",   "content": f"Question: {query}\n\nExcerpts:\n{numbered}"},
            ],
        )
        raw     = re.sub(r"```(?:json)?|```", "", resp["message"]["content"]).strip()
        indices = json.loads(raw)["selected"]
        return [candidates[i] for i in indices if 0 <= i < len(candidates)][: CFG.rerank_top_k]
    except Exception as exc:
        logger.warning("Rerank failed (%s) — using filtered order", exc)
        return candidates[: CFG.rerank_top_k]


# ---------------------------------------------------------------------------
# Stage 9 — Synthesis
# ---------------------------------------------------------------------------

_ANSWER_SYSTEM = """\
You are Jugnu, a sharp personal AI assistant.
Answer using ONLY the evidence provided. Never use training knowledge.
Today is {today} ({today_str}).

Rules:
- One direct sentence first. Then useful detail.
- Include exact date, time, opponent from evidence if present.
- If evidence describes only past events, say so clearly.
- If evidence conflicts, note it briefly.
- No filler. No sycophancy.
""".strip()


def synthesize(query: str, chunks: list[dict]) -> str:
    if not chunks:
        return "SEARCH_FAILED: No evidence retrieved. Do not guess."

    evidence = "\n\n".join(
        (
            f"Source: {c['source']} — {c['url']}\n"
            f"Dates: {', '.join(d.isoformat() for d in c['dates']) or 'none'}\n"
            f"{c['text']}"
        )
        for c in chunks
    )

    try:
        resp = llm_client.chat(
            think=False,
            role="SEARCH",
            messages=[
                {"role": "system", "content": _ANSWER_SYSTEM.format(today=_today_iso(), today_str=_today_str())},
                {"role": "user",   "content": f"Question: {query}\n\nEvidence:\n{evidence}"},
            ],
        )
        return resp["message"]["content"]
    except Exception as exc:
        logger.warning("Synthesis failed: %s", exc)
        return "SEARCH_FAILED: " + " | ".join(c["url"] for c in chunks[:3])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
#
# Two-layer design:
#
#   search_web(query)  →  SearchResult   ← AGENT TOOL
#       Returns structured evidence. No synthesis. The agent inspects
#       sources, decides if more tool calls are needed, then answers.
#
#   answer_from_search(query, result)  →  str   ← FINAL ANSWER HELPER
#       Runs synthesize() over a SearchResult when you want prose output.
#       Call this only at the point you're ready to reply to the user.
#
# Why this matters:
#   Tools should expose information, not compress it into conclusions.
#   When search_web returned a synthesized string, the agent had no way
#   to inspect actual dates/URLs, couldn't chain tool calls reliably, and
#   hallucinated inferences from compressed prose.
#
# ---------------------------------------------------------------------------

@dataclass
class SearchSource:
    title:   str
    url:     str
    text:    str
    dates:   list[date]

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url":   self.url,
            "text":  self.text,
            "dates": [d.isoformat() for d in self.dates],
        }


@dataclass
class SearchResult:
    query:   str
    sources: list[SearchSource]
    error:   Optional[str] = None

    # ---- convenience -------------------------------------------------------

    def ok(self) -> bool:
        return self.error is None and bool(self.sources)

    def to_dict(self) -> dict:
        """
        Structured dict returned to the agent tool handler.
        The agent sees facts, not conclusions.
        """
        if not self.ok():
            return {"error": self.error or "No sources found", "sources": []}
        return {
            "query":   self.query,
            "sources": [s.to_dict() for s in self.sources],
            # Flat date list lets the agent quickly find the most recent date
            # without parsing each source individually.
            "all_dates": sorted(
                {d.isoformat() for s in self.sources for d in s.dates},
                reverse=True,
            ),
        }

    def to_agent_string(self) -> str:
        """
        Compact string serialisation for models that receive tool results
        as text rather than structured JSON.
        Format: one block per source, clearly delimited.
        """
        if not self.ok():
            return f"SEARCH_FAILED: {self.error or 'no sources'}"
        parts = []
        for i, s in enumerate(self.sources, 1):
            date_str = ", ".join(d.isoformat() for d in s.dates) or "no date"
            parts.append(
                f"[{i}] {s.title}\n"
                f"URL: {s.url}\n"
                f"Dates: {date_str}\n"
                f"{s.text}"
            )
        return "\n\n---\n\n".join(parts)


def _run_pipeline(query: str) -> tuple[list[dict], Optional[str]]:
    """Shared retrieval pipeline used by search_web(). Returns (top_chunks, error)."""
    cls = classify_query(query)
    logger.info("Classified: domain=%s time_sensitive=%s", cls["domain"], cls["time_sensitive"])

    raw_results, queries_used = searxng_search(query, cls)
    logger.info("SearXNG: %d unique URLs from: %s", len(raw_results), queries_used)

    if not raw_results:
        return [], "SearXNG returned no results"

    pages = scrape_pages(raw_results)
    logger.info("Scraped %d pages", len(pages))

    chunks = pages_to_chunks(pages, budget=CFG.max_total_chunks)
    if len(chunks) < 5:
        logger.info("Scrape sparse — using snippet fallback")
        chunks = snippets_to_chunks(raw_results)

    logger.info("Generated %d chunks", len(chunks))
    if not chunks:
        return [], "Could not extract content from any page"

    candidates = embed_and_rank(query, chunks, cls["time_sensitive"])
    logger.info("MMR → %d candidates", len(candidates))

    filtered = python_date_filter(candidates, cls["time_sensitive"])
    logger.info("Date filter → %d chunks", len(filtered))

    top = llm_rerank(query, filtered)
    logger.info("Reranker → %d final chunks", len(top))

    return top, None


def search_web(query: str) -> SearchResult:
    """
    AGENT TOOL — returns structured evidence, never a synthesized answer.

    The agent (main model) receives SearchResult.to_dict() or
    SearchResult.to_agent_string() and decides:
      - Is this enough to answer the user?
      - Should I call another tool (e.g. a second search, calculator)?
      - What is the actual answer?

    Usage in your tool handler:
        result = search_web(query)
        return result.to_agent_string()   # for text-based tool results
        # or
        return result.to_dict()           # for structured JSON tool results
    """
    top, error = _run_pipeline(query)
    if error:
        return SearchResult(query=query, sources=[], error=error)

    sources = [
        SearchSource(
            title=c["source"],
            url=c["url"],
            text=c["text"],
            dates=c["dates"],
        )
        for c in top
    ]
    return SearchResult(query=query, sources=sources)


def answer_from_search(query: str, result: Optional[SearchResult] = None) -> str:
    """
    SYNTHESIS HELPER — call only when producing a final user-facing answer.

    If result is None, runs the full pipeline first.
    Uses synthesize() which instructs the model to answer from evidence only.

    Example usage in your agent's final-answer step:
        search_result = search_web(query)
        if search_result.ok():
            answer = answer_from_search(query, search_result)
        else:
            answer = "I couldn't find reliable information on that."
    """
    if result is None:
        top, error = _run_pipeline(query)
        if error or not top:
            return f"SEARCH_FAILED: {error or 'no content'}"
    else:
        if not result.ok():
            return f"SEARCH_FAILED: {result.error or 'no sources'}"
        top = [
            {
                "source": s.title,
                "url":    s.url,
                "text":   s.text,
                "dates":  s.dates,
            }
            for s in result.sources
        ]

    return synthesize(query, top)
