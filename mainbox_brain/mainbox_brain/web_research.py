#!/usr/bin/env python3
"""
web_research.py - self-hosted search -> fetch -> extract -> gemma pipeline.

No paid API. Search comes from a local SearXNG instance; reasoning from a
local Ollama (gemma3:12b) on tillium-bridge. Drop-in for MaINbox Brain.

    pip install httpx trafilatura
    # optional, enables the JS/blocked-page fallback:
    pip install playwright && playwright install chromium

CLI:    python web_research.py "cross reference for Square D QO breaker"
Import: from web_research import research, gather, search, ask_gemma
"""

from __future__ import annotations

import os
import sys
import json
import logging
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import trafilatura

__version__ = "0.2.0"  # v0.2.0: headless-browser fallback for blocked/JS pages

# --- Config (override with env vars) ----------------------------------------
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://100.106.60.4:8080")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://tillium-bridge:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")

# fetch / extract tuning
MAX_RESULTS = 6          # search hits to consider
MAX_FETCH = 5            # how many of those to actually fetch
FETCH_WORKERS = 5        # parallel fetches
FETCH_TIMEOUT = 10.0     # seconds per request
GEN_TIMEOUT = 180.0      # seconds for the LLM generation
MAX_PAGE_BYTES = 5_000_000
PER_SOURCE_CHARS = 1500  # cap on extracted text per source fed to the LLM
CONTEXT_CHARS = 8000     # total context budget

# v0.2.0: browser-fallback tuning. httpx runs first; the headless browser only
# fires for pages that come back blocked (403) or empty (JS-rendered SPAs).
USE_BROWSER_FALLBACK = True   # set False to disable Playwright entirely
MIN_TEXT_CHARS = 200          # extracted text below this -> treat as blocked/empty, retry in browser
BROWSER_TIMEOUT_MS = 20000    # Playwright navigation timeout (milliseconds)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log = logging.getLogger("web_research")


@dataclass
class Source:
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""
    text: str = ""        # extracted main content (empty if fetch/extract failed)


# --- 1. Search --------------------------------------------------------------
def search(query: str, max_results: int = MAX_RESULTS,
           categories: str = "general", engines: str | None = None,
           time_range: str | None = None) -> list[Source]:
    """Query the local SearXNG JSON API. Returns ranked Source stubs (no text)."""
    params = {"q": query, "format": "json", "categories": categories}
    if engines:
        params["engines"] = engines
    if time_range:
        params["time_range"] = time_range

    url = f"{SEARXNG_URL.rstrip('/')}/search"
    try:
        r = httpx.get(url, params=params, timeout=FETCH_TIMEOUT,
                      headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"SearXNG request failed: {e}") from e

    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "SearXNG did not return JSON. Enable the 'json' format in "
            "settings.yml (search.formats) and restart the container."
        ) from e

    out: list[Source] = []
    for item in data.get("results", [])[:max_results]:
        out.append(Source(
            url=item.get("url", ""),
            title=item.get("title", ""),
            snippet=item.get("content", "") or "",
            engine=item.get("engine", "") or "",
        ))
    return out


# --- 2. Fetch ---------------------------------------------------------------
def fetch(url: str, client: httpx.Client | None = None) -> str | None:
    """GET a URL, return HTML text or None. Skips non-HTML and oversized bodies."""
    own = client is None
    if own:
        client = httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True,
                              headers={"User-Agent": USER_AGENT})
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype.lower():
                log.debug("skip non-html %s (%s)", url, ctype)
                return None
            clen = resp.headers.get("content-length")
            if clen and clen.isdigit() and int(clen) > MAX_PAGE_BYTES:
                log.debug("skip oversized %s (%s bytes)", url, clen)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_PAGE_BYTES:
                    log.debug("truncating oversized %s", url)
                    break
            return b"".join(chunks).decode(resp.encoding or "utf-8", "replace")
    except httpx.HTTPError as e:
        log.debug("fetch failed %s: %s", url, e)
        return None
    finally:
        if own:
            client.close()


# --- 3. Extract -------------------------------------------------------------
def extract(html: str | None, url: str = "") -> str | None:
    """Strip HTML to clean main-content markdown. None if nothing useful.

    Broad except is deliberate: trafilatura runs against arbitrary live web
    HTML and a single malformed page must not kill the batch.
    """
    if not html:
        return None
    try:
        text = trafilatura.extract(
            html, url=url or None,
            output_format="markdown",
            include_tables=True,
            include_comments=False,
            favor_recall=True,
        )
    except Exception as e:  # noqa: BLE001 - see docstring
        log.debug("extract failed %s: %s", url, e)
        return None
    return text or None


# --- 3b. Browser fallback (v0.2.0) ------------------------------------------
def _browser_rescue(sources: list[Source]) -> None:
    """Re-fetch thin/blocked pages with a headless browser, then re-extract.

    v0.2.0: handles the two cases plain httpx can't - sites that 403 a bot
    (real browser headers get through) and JS-rendered SPAs that return an
    empty shell to a raw GET (Chromium runs the JS so content actually exists).
    Runs sequentially on one browser instance: Playwright's sync API is fragile
    across worker threads, and only a handful of pages ever need this, so the
    cost stays bounded. No-ops cleanly if Playwright isn't installed.

    Mutates Source.text in place, only when the browser yields MORE text than
    httpx did. Broad excepts are deliberate - a browser driving arbitrary live
    pages will hit odd failures, and one bad page must not sink the rescue.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        log.info("playwright not installed; skipping browser fallback "
                 "(pip install playwright && playwright install chromium)")
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for s in sources:
                    try:
                        page = browser.new_page(user_agent=USER_AGENT)
                        try:
                            page.goto(s.url, wait_until="domcontentloaded",
                                      timeout=BROWSER_TIMEOUT_MS)
                            try:  # let SPA JS populate; take what's there if not
                                page.wait_for_load_state("networkidle",
                                                         timeout=5000)
                            except PWTimeout:
                                pass
                            html = page.content()
                        finally:
                            page.close()
                    except Exception as e:  # noqa: BLE001 - see docstring
                        log.debug("browser fetch failed %s: %s", s.url, e)
                        continue
                    new_text = extract(html, s.url)
                    if new_text and len(new_text) > len(s.text):
                        log.debug("browser rescued %s (%d -> %d chars)",
                                  s.url, len(s.text), len(new_text))
                        s.text = new_text
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - see docstring
        log.debug("browser fallback failed: %s", e)


# --- 4. Gather (search + parallel fetch/extract) ----------------------------
def gather(query: str, max_results: int = MAX_RESULTS,
           max_fetch: int = MAX_FETCH, **search_kw) -> list[Source]:
    """Search, then fetch+extract the top hits in parallel.

    Returns the fetched Sources with .text filled, preserving search ranking.
    """
    sources = search(query, max_results=max_results, **search_kw)
    targets = [s for s in sources if s.url][:max_fetch]

    client = httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT})
    try:
        def work(src: Source) -> None:
            html = fetch(src.url, client=client)
            src.text = extract(html, src.url) or ""

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = {pool.submit(work, s): s for s in targets}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 - one bad worker != fail
                    log.debug("worker error: %s", e)
    finally:
        client.close()

    # v0.2.0: phase 2 - rescue pages that httpx got blocked on or that rendered
    # empty (JS apps). Only the thin ones, only if the fallback is enabled.
    if USE_BROWSER_FALLBACK:
        weak = [s for s in targets if len(s.text) < MIN_TEXT_CHARS]
        if weak:
            _browser_rescue(weak)

    return targets


# --- 5. Build context -------------------------------------------------------
def build_context(sources: list[Source],
                  per_source_chars: int = PER_SOURCE_CHARS,
                  total_chars: int = CONTEXT_CHARS) -> tuple[str, list[Source]]:
    """Assemble a numbered, source-attributed context block for the LLM.

    Returns (context_text, used_sources).
    """
    blocks: list[str] = []
    used: list[Source] = []
    budget = total_chars
    for i, s in enumerate(sources, 1):
        body = (s.text or s.snippet or "").strip()
        if not body:
            continue
        body = body[:per_source_chars][:budget]
        if not body:
            break
        blocks.append(f"[{i}] {s.title}\n{s.url}\n{body}")
        used.append(s)
        budget -= len(body)
        if budget <= 0:
            break
    return "\n\n---\n\n".join(blocks), used


# --- 6. Ask gemma -----------------------------------------------------------
def ask_gemma(question: str, context: str,
              model: str = OLLAMA_MODEL, temperature: float = 0.2) -> str:
    """Send question + retrieved context to local Ollama, return answer text."""
    system = (
        "You are a procurement research assistant. Answer the question using "
        "ONLY the numbered sources provided. Cite sources inline as [n]. "
        "If the sources do not contain the answer, say so plainly."
    )
    user = f"Sources:\n\n{context}\n\nQuestion: {question}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    url = f"{OLLAMA_URL.rstrip('/')}/api/chat"
    try:
        r = httpx.post(url, json=payload, timeout=GEN_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"Ollama request failed: {e}") from e
    return (data.get("message") or {}).get("content", "").strip()


# --- 7. End-to-end ----------------------------------------------------------
def research(question: str, *, max_results: int = MAX_RESULTS,
             max_fetch: int = MAX_FETCH, model: str = OLLAMA_MODEL,
             **search_kw) -> dict:
    """Full pipeline: search -> fetch -> extract -> answer.

    Returns {'answer', 'sources' (cited), 'all' (everything gathered)}.
    """
    gathered = gather(question, max_results=max_results,
                      max_fetch=max_fetch, **search_kw)
    context, used = build_context(gathered)
    if not context:
        return {"answer": "No usable web content was retrieved for this query.",
                "sources": [], "all": gathered}
    answer = ask_gemma(question, context, model=model)
    return {"answer": answer, "sources": used, "all": gathered}


# --- CLI --------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('usage: python web_research.py "your question"', file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    question = " ".join(argv[1:])
    result = research(question)
    print("\n=== ANSWER ===\n")
    print(result["answer"])
    print("\n=== SOURCES ===")
    for i, s in enumerate(result["sources"], 1):
        print(f"[{i}] {s.title} - {s.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
