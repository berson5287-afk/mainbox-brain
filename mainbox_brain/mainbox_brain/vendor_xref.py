#!/usr/bin/env python3
"""
vendor_xref.py  -  targeted vendor cross-reference adapters for MaINbox Brain.
v0.1.0

Drives the actual cross-reference tools on manufacturer websites via Playwright
(headless Chromium), extracts results, and stores them permanently in the local
cross_reference.py store so every part is only ever looked up once.

Supported vendors:
    Southwire   - southwire.com/cross-reference
                  "enter a competitor part number, get the Southwire equivalent"
    Hubbell     - hubbell.com/{brand}/en/part-cross-reference
                  covers RACO, Bryant, Wiegmann, Killark, Hubbell, and more.
                  accepts comma-separated part numbers (2-200 chars each).

stdlib only beyond playwright.  Playwright must be installed:
    pip install playwright
    playwright install chromium

CLI:
    python vendor_xref.py southwire  "Raco 257"
    python vendor_xref.py hubbell    "257"  --brand raco
    python vendor_xref.py southwire  "12/2 MC 250ft"

Import:
    from vendor_xref import lookup_southwire, lookup_hubbell, lookup_all
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
import argparse
from dataclasses import dataclass, field

__version__ = "0.2.17"  # Schneider: robust SPA load (networkidle + reload retry + load diagnostics)

log = logging.getLogger("vendor_xref")

# --- config ------------------------------------------------------------------
HEADLESS        = True          # set False to watch the browser during dev/debug
NAV_TIMEOUT_MS  = 25_000        # page navigation timeout
WAIT_MS         = 3_000         # wait for JS results to populate
SLOW_MO_MS      = 0             # set 200+ to slow-step actions for debug
DB_PATH         = os.environ.get("XREF_DB", "cross_references.db")

# Hubbell brands mapped to their URL slug
HUBBELL_BRANDS: dict[str, str] = {
    "raco":         "raco",
    "hubbell":      "hubbell",
    "bryant":       "bryant",
    "wiegmann":     "wiegmann",
    "killark":      "killark",
    "taymac":       "taymac",
    "hps":          "hubbellpowersystems",
    "aclara":       "aclara",
    "cpi":          "cpi",
}
DEFAULT_HUBBELL_BRAND = "raco"  # v0.2.1: default to RACO (primary use case)


# --- result container --------------------------------------------------------
@dataclass
class XrefResult:
    """One cross-reference hit returned by a vendor tool."""
    src_part:   str
    src_mfr:    str
    equiv_part: str
    equiv_mfr:  str
    description: str = ""
    notes:      str = ""
    source_url: str = ""
    raw:        dict = field(default_factory=dict)

    def __str__(self) -> str:
        desc = f" — {self.description[:60]}" if self.description else ""
        return f"{self.equiv_mfr} {self.equiv_part}{desc}"


# --- Playwright helpers ------------------------------------------------------
def _make_browser(playwright):
    """Launch a single Chromium instance configured to look like a real browser.

    v0.2.5: headless Chromium is detectable (navigator.webdriver, missing UA,
    odd viewport) and some sites (Hubbell) render differently or block it. We
    launch with anti-automation flags and create pages via a context that sets
    a real user-agent + viewport, so headless behaves like --show-browser.
    """
    return playwright.chromium.launch(
        headless=HEADLESS,
        slow_mo=SLOW_MO_MS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",  # hide webdriver
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )


# real Chrome UA string + desktop viewport for the browser context
_REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# v0.2.14: persistent browser profile. When set, adapters launch a Chromium
# profile that survives between runs, so cookies/consent accepted once (e.g.
# Schneider's Terms modal) are remembered and the modal never reappears — just
# like a real browser. Defaults to a folder next to the DB; override with
# XREF_PROFILE. Set to empty/"none" to disable and use ephemeral contexts.
_DEFAULT_PROFILE = os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)) or ".", ".xref_browser_profile")
PROFILE_DIR = os.environ.get("XREF_PROFILE", _DEFAULT_PROFILE)


class _BrowserSession:
    """Context manager yielding a ready-to-use Playwright page.

    Uses a PERSISTENT profile when PROFILE_DIR is set (cookies/consent survive
    between runs), otherwise an ephemeral context. Either way the page looks
    like a real desktop browser (UA, viewport, webdriver stripped). Closes
    everything on exit.

    Usage:
        with _BrowserSession(p) as page:
            page.goto(...)
    """

    def __init__(self, playwright):
        self._p = playwright
        self._ctx = None
        self._browser = None

    def __enter__(self):
        use_profile = bool(PROFILE_DIR) and PROFILE_DIR.lower() != "none"
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if use_profile:
            try:
                os.makedirs(PROFILE_DIR, exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
            # persistent context IS the browser+context in one
            self._ctx = self._p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=HEADLESS,
                slow_mo=SLOW_MO_MS,
                user_agent=_REAL_UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                args=launch_args,
            )
            log.debug("browser: persistent profile at %s", PROFILE_DIR)
        else:
            self._browser = self._p.chromium.launch(
                headless=HEADLESS, slow_mo=SLOW_MO_MS, args=launch_args)
            self._ctx = self._browser.new_context(
                user_agent=_REAL_UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            log.debug("browser: ephemeral context (no profile)")

        self._ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined});")
        # reuse the first page a persistent context opens, else make one
        pages = self._ctx.pages
        return pages[0] if pages else self._ctx.new_page()

    def __exit__(self, *exc):
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        return False


def _new_context_page(browser):
    """Create a page in a context that mimics a real desktop browser and
    strips the navigator.webdriver flag that exposes automation.

    NOTE: legacy helper kept for any direct callers. New code should use
    _BrowserSession so it benefits from the persistent profile.
    """
    context = browser.new_context(
        user_agent=_REAL_UA,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
    )
    # remove the webdriver flag before any page script runs
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    return context.new_page()


def _accept_cookies(page) -> None:
    """Dismiss cookie banners that may obscure the search input."""
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        for sel in (
            "button:has-text('Accept All')",
            "button:has-text('Accept All Cookies')",
            "button:has-text('Accept')",
            "#onetrust-accept-btn-handler",
        ):
            try:
                btn = page.locator(sel).first
                btn.click(timeout=2_000)
                page.wait_for_timeout(500)
                return
            except PWTimeout:
                continue
    except Exception:  # noqa: BLE001
        pass


def _accept_schneider_consent(page) -> bool:
    """Dismiss Schneider's 'Terms & condition' consent modal.

    v0.2.11: tools.se.app shows a CONFIDENTIAL AND AS-IS USE AGREEMENT modal on
    every fresh load (Playwright starts cookieless, so it can't remember a prior
    consent). The modal covers the search input. Sequence:
        1. detect the modal (heading 'Terms & condition' / 'Submit Consent' btn)
        2. tick 'I consent to this usage' — try a real checkbox, else click the
           label text, else force it via JS (some UIs render a styled overlay)
        3. verify the box is actually checked (Submit may be disabled otherwise)
        4. click 'Submit Consent' and VERIFY the modal is gone
    Returns True only if the modal was dismissed (or wasn't present).
    """
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        return False

    def _modal_present() -> bool:
        # The consent modal is uniquely identified by its 'Submit Consent'
        # button AND the agreement body text. We must NOT match the permanent
        # 'Terms & Conditions of Use' link in the page header (always present),
        # so we require the Submit Consent button specifically — that only
        # exists while the modal is open.
        try:
            return bool(page.evaluate(
                "() => { const t = document.body.innerText || '';"
                " const hasSubmit = /Submit\\s*Consent/i.test(t);"
                " const hasAgreement = /AS-IS USE AGREEMENT|I consent to this"
                " usage/i.test(t);"
                " return hasSubmit && hasAgreement; }"))
        except Exception:  # noqa: BLE001
            return False

    # 1) is the modal there? With a persistent profile the consent is usually
    # already remembered, so the modal won't appear — in that case we touch
    # NOTHING (no checkbox clicks) and return immediately. Only poll briefly.
    present = False
    for _ in range(6):            # up to ~1.8s — modal renders fast if at all
        if _modal_present():
            present = True
            break
        page.wait_for_timeout(300)
    if not present:
        log.debug("schneider: no consent modal (profile remembered it) — "
                  "leaving page untouched")
        return True               # nothing to dismiss -> success

    log.debug("schneider: consent modal detected, dismissing")

    # 2) tick the consent checkbox the way a REAL USER does ------------------
    # CRITICAL: this is a React controlled checkbox. Setting .checked via JS
    # changes the DOM property but NOT React's internal state, so React re-
    # renders and resets the box to unchecked (-> "Please approve the consent"
    # error). The only thing that works is a genuine browser click event that
    # React's onChange handler receives. So we do a real Playwright click on
    # the checkbox (or its label) and let React update its own state.
    def _is_box_checked() -> bool:
        # check the CONSENT checkbox specifically (the one near 'I consent'),
        # never the 'Only Display Active' filter checkbox elsewhere on the page
        try:
            return bool(page.evaluate("""
                () => {
                  // find the checkbox whose label/sibling text says 'I consent'
                  const cbs = Array.from(
                    document.querySelectorAll('input[type=checkbox]'));
                  for (const c of cbs) {
                    const lbl = (c.closest('label')?.innerText || '') + ' ' +
                                (c.parentElement?.innerText || '');
                    if (/I consent/i.test(lbl)) return c.checked;
                  }
                  return false;
                }
            """))
        except Exception:  # noqa: BLE001
            return False

    checked = False
    # Target ONLY the consent checkbox/label — explicitly NOT a bare
    # input[type=checkbox] (which could match 'Only Display Active').
    click_targets = [
        page.get_by_label("I consent to this usage"),
        page.locator("label:has-text('I consent')"),
        page.get_by_text("I consent to this usage", exact=False),
    ]
    for tgt in click_targets:
        try:
            el = tgt.first
            el.wait_for(state="visible", timeout=1_500)
            el.scroll_into_view_if_needed(timeout=1_000)
            el.click(timeout=1_500)
            page.wait_for_timeout(400)
            if _is_box_checked():
                checked = True
                log.debug("schneider: consent box checked via real click")
                break
        except Exception:  # noqa: BLE001
            continue

    # if a single click didn't register as checked, try one force-click on the
    # CONSENT label (bypasses overlay intercepts) — still a real click event.
    # Use the label, never a bare checkbox (which could be 'Only Display Active')
    if not checked:
        try:
            box = page.locator("label:has-text('I consent')").first
            box.click(timeout=1_500, force=True)
            page.wait_for_timeout(400)
            checked = _is_box_checked()
            if checked:
                log.debug("schneider: consent box checked via force-click")
        except Exception:  # noqa: BLE001
            pass

    if not checked:
        log.debug("schneider: WARNING could not verify consent box checked; "
                  "submitting anyway")

    # 3) click 'Submit Consent' with a REAL Playwright click and verify -------
    # Now that React knows the box is checked, a normal click submits. We click
    # the actual button element (scrolled into view) and verify the modal goes.
    for attempt in range(3):
        # re-assert the checkbox is still checked before clicking (React may
        # have re-rendered); if it came unchecked, click it again for real.
        # Target the consent label only — never the 'Only Display Active' box.
        if not _is_box_checked():
            for tgt in (page.locator("label:has-text('I consent')").first,
                        page.get_by_text("I consent to this usage",
                                         exact=False).first):
                try:
                    tgt.click(timeout=1_200, force=True)
                    page.wait_for_timeout(300)
                    if _is_box_checked():
                        break
                except Exception:  # noqa: BLE001
                    continue

        clicked = False
        for btn in (
            page.get_by_role("button", name="Submit Consent"),
            page.locator("button:has-text('Submit Consent')"),
            page.locator("button:has-text('Submit')"),
        ):
            try:
                b = btn.first
                b.wait_for(state="visible", timeout=2_000)
                b.scroll_into_view_if_needed(timeout=1_000)
                b.click(timeout=2_000)
                clicked = True
                log.debug("schneider: clicked Submit (attempt %d, box=%s)",
                          attempt + 1, _is_box_checked())
                break
            except Exception:  # noqa: BLE001
                continue

        page.wait_for_timeout(1_000)
        if not _modal_present():
            log.debug("schneider: consent modal dismissed (attempt %d)",
                      attempt + 1)
            return True
        if not clicked:
            log.debug("schneider: Submit not found on attempt %d", attempt + 1)

    # still here -> dump the modal's buttons/inputs for diagnosis
    try:
        info = page.evaluate("""
            () => {
              const btns = Array.from(document.querySelectorAll('button'))
                .map(b => ({t:(b.innerText||'').trim().slice(0,30),
                            dis:b.disabled}));
              const cbs = Array.from(
                document.querySelectorAll('input[type=checkbox]'))
                .map(c => ({checked:c.checked,
                            id:c.id||'', name:c.name||''}));
              return {buttons: btns, checkboxes: cbs};
            }
        """)
        log.warning("schneider: consent NOT dismissed. buttons=%s checkboxes=%s",
                    info.get("buttons"), info.get("checkboxes"))
    except Exception:  # noqa: BLE001
        pass
    return False


# --- Southwire adapter -------------------------------------------------------
def lookup_southwire(part: str, *, mfr_filter: str | None = None,
                     db: str | None = None) -> list[XrefResult]:
    """Drive southwire.com/cross-reference for a competitor part number.

    v0.2.0: Southwire's tool is client-side filtered — the full dataset loads
    with the page and filters as you type. No Search button exists. The page
    requires a real browser session (Akamai blocks raw httpx). Strategy:
        1. Navigate with Playwright (bypasses bot detection via real session)
        2. Find the input next to the 'Search:' label, type the part number
        3. Wait ~2s for the JS filter to run
        4. Scrape the visible table: Manufacturer | Manufacturer Number |
           Southwire Part Number
    No submit step needed — results appear live as you type.

    v0.2.8: mfr_filter (optional) keeps only rows whose manufacturer column
    contains that brand. Southwire groups brands ('Hubbell / RACO / TayMac /
    Bell / Killark'), so we substring-match (case/space-insensitive) within the
    group, not exact-match. When a filter is given, matched rows are stored at
    higher confidence (the user supplied the exact brand).
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("playwright not installed — pip install playwright && "
                    "playwright install chromium")
        return []

    url = "https://www.southwire.com/cross-reference"
    results: list[XrefResult] = []

    try:
        with sync_playwright() as p:
            with _BrowserSession(p) as page:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
                # wait for the table to load (the data is embedded in the page)
                try:
                    page.wait_for_selector("table", timeout=10_000)
                except PWTimeout:
                    log.warning("southwire: table never appeared — page may "
                                "have blocked the session")
                    return []

                _accept_cookies(page)

                # The cross-reference page has a 'Search:' label next to a
                # plain text input — distinct from the site's global search bar
                # at the top of every page. We scope to the form/section
                # containing that label to avoid grabbing the wrong input.
                # DO NOT press Enter — that triggers the global site search
                # redirect. The JS filter fires on keystroke, no submit needed.
                inp = None
                for loc in (
                    # most reliable: input inside the same container as
                    # the 'Search:' label (the cross-ref form section)
                    page.locator("label:has-text('Search:') + input"),
                    page.locator("label:has-text('Search:') ~ input"),
                    # scoped to main content area, not the header
                    page.locator("main input[type='text']"),
                    page.locator("#main input[type='text']"),
                    page.locator(".content input[type='text']"),
                    page.locator("form input[type='text']"),
                ):
                    try:
                        loc.first.wait_for(state="visible", timeout=2_000)
                        inp = loc.first
                        break
                    except PWTimeout:
                        continue

                if inp is None:
                    log.warning("southwire: search input not found")
                    return []

                inp.click()
                inp.fill("")          # clear any existing value
                inp.type(part, delay=50)   # type like a human, triggers JS filter

                # no submit — results filter live; wait for DOM to settle
                page.wait_for_timeout(2_000)

                # v0.2.4: SCRAPE EACH PAGE AS WE GO. Arrow pagination REPLACES
                # the visible rows on each advance, so scraping only once at the
                # end loses every page but the last. We accumulate across pages
                # and dedup by (competitor_part|southwire_part).
                results = []
                seen_keys: set[str] = set()

                def _collect_current_page() -> int:
                    """Scrape the current page, add new rows, return count added."""
                    html_now = page.content()
                    added = 0
                    for r in _parse_southwire_table(html_now, part):
                        k = f"{r.description}|{r.equiv_part}"
                        if k in seen_keys:
                            continue
                        seen_keys.add(k)
                        results.append(r)
                        added += 1
                    return added

                # collect page 1
                _collect_current_page()

                # v0.2.6: Southwire pagination is ONLY two arrows flanking an
                # "X of Y" label — there are NO numbered page links. The prior
                # version clicked links named '2','3'... which matched unrelated
                # links elsewhere and navigated off the cross-reference page.
                # Fix: find the element holding the "X of Y" text and click the
                # next clickable element AFTER it (the '>' right arrow), nothing
                # else. Re-collect rows after each advance.
                import re as _re
                total_pages = 1
                try:
                    body_txt = page.inner_text("body", timeout=2_000)
                    m = _re.search(r"\b(\d+)\s+of\s+(\d+)\b", body_txt)
                    if m:
                        total_pages = int(m.group(2))
                        log.debug("southwire: detected %d total pages",
                                  total_pages)
                except Exception:  # noqa: BLE001
                    pass

                def _click_next_arrow() -> bool:
                    """Click the '>' arrow that sits right after the 'X of Y'
                    pagination text. Returns True if a click happened.

                    Uses JS to locate the pagination control and dispatch a
                    real click on the actual clickable element (handles SVG-icon
                    arrows where the visible glyph isn't the click target).
                    Returns True only if the click likely advanced the page.
                    """
                    # JS finds the '>' next arrow next to the 'X of Y' text and
                    # clicks the nearest clickable ancestor. Returns a short
                    # description of what it clicked, or '' if nothing found.
                    js = r"""
                    () => {
                      // find the element whose direct text is like '1 of 5'
                      const all = Array.from(document.querySelectorAll('*'));
                      let pager = null;
                      for (const el of all) {
                        const own = Array.from(el.childNodes)
                          .filter(n => n.nodeType === 3)
                          .map(n => n.textContent).join('');
                        if (/\b\d+\s+of\s+\d+\b/.test(own)) { pager = el; break; }
                      }
                      if (!pager) return '';
                      // gather candidate clickables near the pager: its parent's
                      // a/button/[role=button] descendants, in document order
                      const scope = pager.parentElement || pager;
                      const clicks = Array.from(scope.querySelectorAll(
                        'a,button,[role=button],[class*=arrow],[class*=next],svg'));
                      // the next arrow is the LAST such control (prev is first)
                      if (!clicks.length) return '';
                      // prefer one whose aria-label/class mentions next
                      let target = clicks.find(c => {
                        const s = ((c.getAttribute('aria-label')||'') + ' ' +
                                   (c.className||'')).toLowerCase();
                        return s.includes('next');
                      });
                      if (!target) target = clicks[clicks.length - 1];
                      // climb to the nearest clickable ancestor (a/button)
                      let t = target;
                      while (t && t !== scope &&
                             !(t.tagName === 'A' || t.tagName === 'BUTTON' ||
                               t.getAttribute('role') === 'button')) {
                        t = t.parentElement;
                      }
                      t = t || target;
                      // skip if disabled
                      const dis = t.getAttribute('disabled') !== null ||
                                  t.getAttribute('aria-disabled') === 'true' ||
                                  (t.className||'').toLowerCase().includes('disabled');
                      if (dis) return 'DISABLED';
                      t.scrollIntoView({block:'center'});
                      t.click();
                      return (t.tagName + ' aria=' +
                              (t.getAttribute('aria-label')||'') + ' cls=' +
                              (t.className||'').toString().slice(0,40));
                    }
                    """
                    try:
                        what = page.evaluate(js)
                    except Exception as e:  # noqa: BLE001
                        log.debug("southwire: arrow JS error: %s", e)
                        return False
                    if not what:
                        log.debug("southwire: no pager arrow found")
                        return False
                    if what == "DISABLED":
                        log.debug("southwire: next arrow disabled (last page)")
                        return False
                    log.debug("southwire: clicked next -> %s", what)
                    return True

                # advance up to total_pages-1 times (or a hard cap of 20).
                # Verify each advance by checking the FIRST row's text changed,
                # which is more reliable than row counts (pages can repeat parts).
                max_advances = (total_pages - 1) if total_pages > 1 else 20

                def _first_row_signature() -> str:
                    try:
                        return page.locator("table tr").nth(1).inner_text(
                            timeout=1_000)[:60]
                    except Exception:  # noqa: BLE001
                        return ""

                for i in range(max_advances):
                    sig_before = _first_row_signature()
                    if not _click_next_arrow():
                        log.debug("southwire: stopping at page %d", i + 1)
                        break
                    # wait for the first row to actually change (page advanced)
                    changed = False
                    for _ in range(15):   # up to ~3s
                        page.wait_for_timeout(200)
                        if _first_row_signature() != sig_before:
                            changed = True
                            break
                    if not changed:
                        log.debug("southwire: click didn't change rows at page "
                                  "%d — arrow not functional, stopping", i + 1)
                        break
                    added = _collect_current_page()
                    log.debug("southwire: -> page %d, +%d rows (%d total)",
                              i + 2, added, len(results))

                if not results:
                    log.debug("southwire: no rows matched %r in table", part)
    except Exception as e:  # noqa: BLE001
        log.warning("southwire lookup failed for %r: %s", part, e)

    # v0.2.8: optional manufacturer filter. Southwire groups brands in one
    # cell ("Hubbell / RACO / TayMac / Bell / Killark"), so match the brand as
    # a normalized substring of the manufacturer name. User-supplied brand ->
    # higher confidence + flag so _persist stores it as a stronger match.
    if mfr_filter:
        want = _norm_mfr(mfr_filter)
        kept: list[XrefResult] = []
        for r in results:
            if want in _norm_mfr(r.src_mfr):
                r.raw["mfr_filtered"] = True   # signals higher confidence
                r.notes = (f"[brand match: {mfr_filter}] " + r.notes).strip()
                kept.append(r)
        log.debug("southwire: mfr_filter %r kept %d of %d rows",
                  mfr_filter, len(kept), len(results))
        results = kept

    if results and db is not False:
        _persist(results, source="southwire.com/cross-reference", db=db)

    return results


def _norm_mfr(s: str) -> str:
    """Normalize a manufacturer string for substring matching: lowercase,
    drop spaces/punctuation so 'RACO', ' raco ', 'Ra-Co' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse_southwire_table(html: str, src_part: str) -> list[XrefResult]:
    """Parse the Southwire cross-reference table after JS filtering.

    Confirmed column order from live page screenshot:
        col 0: Manufacturer (e.g. 'ABB / T&B / Steel City / Red Dot ...')
        col 1: Manufacturer Number (competitor part, e.g. '2IHD5-2')
        col 2: Southwire Part Number (e.g. 'WDB2575')

    Rows with 'No Cross' in the Southwire column are skipped.
    """
    results: list[XrefResult] = []
    row_re  = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_re = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>",
                         re.IGNORECASE | re.DOTALL)

    def clean(s: str) -> str:
        return re.sub(r"\s+", " ",
                      re.sub(r"<[^>]+>", " ", s)).strip()

    seen: set[str] = set()
    for row_m in row_re.finditer(html):
        cells = [clean(c.group(1)) for c in
                 cell_re.finditer(row_m.group(1))]
        if len(cells) < 3:
            continue
        mfr_text   = cells[0]     # "ABB / T&B / Steel City ..."
        mfr_num    = cells[1]     # competitor part number
        sw_part    = cells[2]     # Southwire part number

        # skip header row and "No Cross" rows
        if (mfr_text.lower() in ("manufacturer", "manufacturer *")
                or "no cross" in sw_part.lower()
                or not sw_part
                or not mfr_num):
            continue

        key = f"{mfr_num}|{sw_part}"
        if key in seen:
            continue
        seen.add(key)

        results.append(XrefResult(
            src_part=src_part,
            src_mfr=mfr_text[:80],
            equiv_part=sw_part,
            equiv_mfr="Southwire",
            description=f"Competitor: {mfr_text[:50]} {mfr_num}",
            source_url="https://www.southwire.com/cross-reference",
        ))

    return results


# --- Hubbell / RACO adapter --------------------------------------------------
def lookup_hubbell(part: str, brand: str = DEFAULT_HUBBELL_BRAND,
                   *, db: str | None = None) -> list[XrefResult]:
    """Drive hubbell.com/{brand}/en/part-cross-reference for a competitor part.

    v0.2.0: Confirmed client-side filtered like Southwire — the full dataset
    loads with the page and the SEARCH button filters it in JS. No XHR fires.
    Strategy:
        1. Navigate with Playwright (real session bypasses bot detection)
        2. Type into the textarea (placeholder: 'Enter one or more competitor
           parts. If multiple entries, use a comma...')
        3. Click the yellow SEARCH button
        4. Wait for 'Search Results' section to appear
        5. Scrape the table: Competitor Name | Competitor Part | Hubbell Part #
           | Hubbell Catalog # | Description | Brand | Degree of Match

    Confirmed columns from live page screenshot.
    Accepts comma-separated parts for batch lookup.
    Covers RACO, Bryant, Hubbell, Wiegmann, Killark, Taymac, HPS, Aclara, CPI.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("playwright not installed — pip install playwright && "
                    "playwright install chromium")
        return []

    slug = HUBBELL_BRANDS.get(brand.lower(), brand.lower())
    url  = f"https://www.hubbell.com/{slug}/en/part-cross-reference"
    results: list[XrefResult] = []
    brand_label = brand.title()

    try:
        with sync_playwright() as p:
            with _BrowserSession(p) as page:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)

                _accept_cookies(page)

                # --- find the textarea / input --------------------------------
                # v0.2.2: confirmed placeholder from RACO live page screenshot:
                # "Competitor Part No" (not "Enter one or more competitor parts")
                # Scoped to main content area to avoid the site search bar.
                inp = None
                for loc in (
                    page.get_by_placeholder("Competitor Part No"),
                    page.get_by_placeholder("Enter one or more competitor"),
                    page.locator("main input[type='text']"),
                    page.locator("main textarea"),
                    page.locator(".content input[type='text']"),
                    page.locator("form input[type='text']"),
                    page.locator("form textarea"),
                ):
                    try:
                        loc.first.wait_for(state="visible", timeout=4_000)
                        inp = loc.first
                        break
                    except PWTimeout:
                        continue

                if inp is None:
                    log.warning("hubbell/%s: search input not found", slug)
                    return []

                inp.click()
                inp.fill(part)
                page.wait_for_timeout(300)

                # --- click the SEARCH button ----------------------------------
                # v0.2.4: anchor the SEARCH button to the cross-reference input
                # we already found, NOT to the page. The header magnifying-glass
                # is a separate search box; matching page-wide kept hitting it.
                # Strategy: from the input, walk up to a common ancestor that
                # also contains a button whose EXACT (trimmed) text is "SEARCH",
                # using XPath following-sibling/ancestor relative to the input.
                clicked = False

                # 1) try: a button that is an XPath sibling-area of the input,
                #    exact text SEARCH (case-insensitive), excluding header
                xpath_candidates = [
                    # button after the input, within the same form/section,
                    # whose normalized text is exactly 'search'
                    "xpath=//button["
                    "translate(normalize-space(.),"
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
                    "='search']",
                ]
                for xp in xpath_candidates:
                    try:
                        btns = page.locator(xp)
                        n = btns.count()
                        for i in range(n):
                            el = btns.nth(i)
                            try:
                                el.wait_for(state="visible", timeout=1_000)
                            except Exception:  # noqa: BLE001
                                continue
                            # exclude anything inside a <header> or nav
                            in_header = el.evaluate(
                                "e => !!e.closest('header,nav,[role=banner]')")
                            if in_header:
                                continue
                            el.scroll_into_view_if_needed(timeout=2_000)
                            el.click(timeout=3_000)
                            clicked = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    if clicked:
                        break

                # 2) fallback: the yellow SEARCH button sits right after RESET;
                #    find a RESET button and click its next-sibling button
                if not clicked:
                    try:
                        reset = page.get_by_role("button", name="RESET").first
                        reset.wait_for(state="visible", timeout=2_000)
                        sib = page.locator(
                            "xpath=//button["
                            "translate(normalize-space(.),"
                            "'abcdefghijklmnopqrstuvwxyz',"
                            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ')='RESET']"
                            "/following-sibling::button[1]")
                        sib.first.click(timeout=3_000)
                        clicked = True
                    except Exception:  # noqa: BLE001
                        pass

                # 3) last resort + DIAGNOSTIC: dump every button so we can see
                #    exactly what's on the page if we still missed
                if not clicked:
                    try:
                        all_btns = page.locator("button")
                        cnt = all_btns.count()
                        log.warning("hubbell/%s: SEARCH not clicked. %d buttons "
                                    "on page:", slug, cnt)
                        for i in range(min(cnt, 25)):
                            b = all_btns.nth(i)
                            try:
                                txt = (b.inner_text(timeout=500) or "").strip()
                                in_hdr = b.evaluate(
                                    "e => !!e.closest('header,nav,[role=banner]')")
                                log.warning("  button[%d] text=%r header=%s",
                                            i, txt[:30], in_hdr)
                            except Exception:  # noqa: BLE001
                                continue
                    except Exception:  # noqa: BLE001
                        pass
                    log.warning("hubbell/%s: could not find the form SEARCH "
                                "button — run with --debug and report the dump",
                                slug)
                    return []

                # --- wait for 'Search Results' section -----------------------
                try:
                    page.wait_for_selector(
                        "text=Search Results", timeout=8_000)
                except PWTimeout:
                    log.debug("hubbell/%s: 'Search Results' heading didn't "
                              "appear — may be no results", slug)

                page.wait_for_timeout(1_000)   # let table finish rendering

                html = page.content()
                results = _parse_hubbell_table(html, part, brand_label, url)

                if not results:
                    log.debug("hubbell/%s: no rows matched %r", slug, part)
    except Exception as e:  # noqa: BLE001
        log.warning("hubbell/%s lookup failed for %r: %s", slug, part, e)

    if results and db is not False:
        _persist(results, source=url, db=db)

    return results


def _parse_hubbell_table(html: str, src_part: str,
                         brand_label: str, url: str) -> list[XrefResult]:
    """Parse the Hubbell cross-reference results table.

    Confirmed column order from live page screenshot:
        0: Competitor Name   (e.g. 'Southwire - Garvin')
        1: Competitor Part   (highlighted in yellow, e.g. '72171-S')
        2: Hubbell Part #    (e.g. '257')
        3: Hubbell Catalog # (e.g. '257' with external link)
        4: Description       (e.g. '4-11/16 in. Square Box, Welded...')
        5: Brand             (e.g. 'RACO')
        6: Image
        7: Degree of Match   (e.g. 'Equivalent')

    Degree of Match drives confidence: Equivalent=0.85, Similar=0.65, else=0.55
    """
    results: list[XrefResult] = []
    row_re  = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_re = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>",
                         re.IGNORECASE | re.DOTALL)

    def clean(s: str) -> str:
        return re.sub(r"\s+", " ",
                      re.sub(r"<[^>]+>", " ", s)).strip()

    # only process rows inside / after the Search Results section
    results_start = html.lower().find("search results")
    if results_start == -1:
        results_start = 0
    search_html = html[results_start:]

    seen: set[str] = set()
    for row_m in row_re.finditer(search_html):
        cells = [clean(c.group(1)) for c in
                 cell_re.finditer(row_m.group(1))]
        if len(cells) < 5:
            continue

        # skip header rows
        if any(h in cells[0].lower() for h in
               ("competitor name", "hubbell part", "degree")):
            continue

        comp_name   = cells[0]           # 'Southwire - Garvin'
        comp_part   = cells[1]           # '72171-S'
        hub_part    = cells[2]           # '257'
        catalog_num = cells[3] if len(cells) > 3 else hub_part
        description = cells[4] if len(cells) > 4 else ""
        brand_col   = cells[5] if len(cells) > 5 else brand_label
        dom_text    = cells[7] if len(cells) > 7 else ""

        if not hub_part or not comp_part:
            continue

        # confidence from Degree of Match column
        dom_low = dom_text.lower()
        if "equivalent" in dom_low:
            confidence = 0.85
            equiv_type = "ul_classified"   # manufacturer asserting equivalence
        elif "similar" in dom_low:
            confidence = 0.65
            equiv_type = "spec_equivalent"
        else:
            confidence = 0.55
            equiv_type = "spec_equivalent"

        key = f"{comp_part}|{hub_part}"
        if key in seen:
            continue
        seen.add(key)

        results.append(XrefResult(
            src_part=comp_part,
            src_mfr=comp_name[:80] or "competitor",
            equiv_part=hub_part,
            equiv_mfr=brand_col or brand_label,
            description=description[:120],
            notes=f"Degree of Match: {dom_text} | Catalog: {catalog_num}",
            source_url=url,
            raw={"degree_of_match": dom_text, "catalog": catalog_num},
        ))

    return results


# --- persist results to cross_reference store --------------------------------
def _persist(results: list[XrefResult], *, source: str,
             db: str | None = None) -> None:
    """Store adapter results into the cross_reference SQLite store.

    Uses cross_reference.add() so user-confirmed rows are never overwritten.
    Stores as spec_equivalent with confidence 0.70 (higher than seed guesses
    but below user-confirmed); the user can confirm/reject via CLI.
    """
    try:
        import importlib
        import sys as _sys
        # support both package-relative and standalone import
        try:
            from . import cross_reference as xr
        except ImportError:
            xr = importlib.import_module("cross_reference")
        for r in results:
            try:
                # v0.2.8: brand-matched rows (user named the manufacturer) are
                # more trustworthy -> store at 0.85 instead of 0.70.
                # v0.2.9: a Schneider 'Direct Replacement' is the strongest
                # signal -> store at 0.90 as ul_classified.
                etype = r.raw.get("equiv_type", xr.T_SPEC)
                if r.raw.get("direct_replacement"):
                    conf = 0.90
                elif r.raw.get("mfr_filtered"):
                    conf = 0.85
                else:
                    conf = 0.70
                xr.add(
                    part=r.src_part,
                    mfr=r.src_mfr if r.src_mfr != "competitor" else "Unknown",
                    equiv_part=r.equiv_part,
                    equiv_mfr=r.equiv_mfr,
                    equiv_type=etype,
                    confidence=conf,
                    notes=f"via {source}: {r.description[:80]}",
                    db=db or DB_PATH,
                )
                log.debug("stored %s %s -> %s %s (conf %.2f, %s)",
                          r.src_mfr, r.src_part, r.equiv_mfr, r.equiv_part,
                          conf, etype)
            except Exception as e:  # noqa: BLE001
                log.debug("persist failed for %s: %s", r.equiv_part, e)
    except Exception as e:  # noqa: BLE001
        log.warning("cross_reference store unavailable: %s", e)


# --- Schneider Electric adapter ----------------------------------------------
def lookup_schneider(part: str, *, mfr_filter: str | None = None,
                     db: str | None = None) -> list[XrefResult]:
    """Drive Schneider's cross-reference tool (tools.se.app/xref) for a part.

    v0.2.9: Confirmed a React SPA, client-side filtered (data loads with the
    page bundle, no XHR fires on search). Richest data of all the adapters —
    returns SE catalog number PLUS stock status, price, discount schedule, and
    a Notes column that sometimes says 'Direct Replacement' (a strong match).
    Strategy:
        1. Navigate with Playwright (real session; raw fetch gets an empty SPA)
        2. Type the part into the 'Search for a ... part number' input
        3. Click the search button (magnifier next to the input)
        4. Wait for the 'N Results for X' heading + results table to render
        5. Scrape: Competitor/Obs Cat Num | SE Catalog Number | SE Cat# Details
           | Notes

    Confirmed column order from live page screenshot. mfr_filter keeps only
    rows whose competitor cell contains that brand (case/space-insensitive).
    A 'Direct Replacement' note -> ul_classified + higher confidence.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("playwright not installed — pip install playwright && "
                    "playwright install chromium")
        return []

    url = ("https://tools.se.app/xref/v3/public/prod/v3-1/index.html"
           "?lang=EN_US")
    results: list[XrefResult] = []

    try:
        with sync_playwright() as p:
            with _BrowserSession(p) as page:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=NAV_TIMEOUT_MS)
                # give the SPA time to boot; networkidle helps but don't fail
                # the whole run if it times out
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except PWTimeout:
                    pass

                # SPA — wait for the search input to render. React can be slow
                # on a cold profile; poll generously before giving up, and dump
                # what DID load if we still can't find it.
                input_ready = False
                for _ in range(3):
                    try:
                        page.wait_for_selector("input", timeout=12_000)
                        input_ready = True
                        break
                    except PWTimeout:
                        # nudge a reload in case the first load stalled
                        log.debug("schneider: input not ready, reloading...")
                        try:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=NAV_TIMEOUT_MS)
                            page.wait_for_timeout(2_000)
                        except Exception:  # noqa: BLE001
                            pass

                if not input_ready:
                    # diagnostics: what's actually on the page?
                    try:
                        title = page.title()
                        body_len = len(page.inner_text("body", timeout=2_000))
                        ninputs = page.locator("input").count()
                        log.warning("schneider: app never rendered an input "
                                    "(title=%r, body_chars=%d, inputs=%d). The "
                                    "browser profile may be stale — delete the "
                                    ".xref_browser_profile folder and retry.",
                                    title, body_len, ninputs)
                    except Exception:  # noqa: BLE001
                        log.warning("schneider: app never rendered an input")
                    return []

                # v0.2.10: dismiss the 'Terms & condition' consent modal that
                # appears on every fresh load and covers the search input.
                _accept_schneider_consent(page)
                _accept_cookies(page)

                # --- find the cross-reference search input --------------------
                # placeholder/label: "Search for a Schneider Electric or
                # competitor part number". Avoid the site-wide "What are you
                # looking for?" search at the very top.
                inp = None
                for loc in (
                    page.get_by_placeholder(
                        "Search for a Schneider Electric"),
                    page.locator(
                        "input[placeholder*='competitor part number']"),
                    page.locator("input[placeholder*='part number']"),
                    page.locator("#root input[type='text']"),
                    page.locator("main input[type='text']"),
                ):
                    try:
                        loc.first.wait_for(state="visible", timeout=4_000)
                        inp = loc.first
                        break
                    except PWTimeout:
                        continue

                if inp is None:
                    log.warning("schneider: search input not found")
                    return []

                inp.click()
                inp.fill(part)
                page.wait_for_timeout(300)

                # capture the result-heading state BEFORE submitting so we can
                # tell whether a given submit method actually triggered a search
                def _heading_count() -> int:
                    try:
                        t = page.inner_text("body", timeout=1_000)
                        m = re.search(r"(\d+)\s+Results?\s+for", t, re.I)
                        return int(m.group(1)) if m else -1
                    except Exception:  # noqa: BLE001
                        return -1

                # --- submit: try EVERY method, don't stop early --------------
                # The app may submit on Enter, on a magnifier button, or only
                # after a real keystroke. We try them in sequence and check
                # after each whether a results heading appeared.
                if log.isEnabledFor(logging.DEBUG):
                    # dump buttons near the input so we can see the real trigger
                    try:
                        info = page.evaluate("""
                            () => {
                              const out = [];
                              document.querySelectorAll(
                                'button,[role=button],input[type=submit],a')
                                .forEach(b => {
                                  const t=(b.innerText||b.value||
                                           b.getAttribute('aria-label')||'').trim();
                                  const cls=(b.className||'').toString().slice(0,40);
                                  if (t.length<30) out.push({t, cls,
                                    tag:b.tagName});
                                });
                              return out.slice(0, 20);
                            }
                        """)
                        log.debug("schneider: clickable elements: %s", info)
                    except Exception:  # noqa: BLE001
                        pass

                # method 1: press Enter in the field
                try:
                    inp.press("Enter")
                    page.wait_for_timeout(1_500)
                except Exception:  # noqa: BLE001
                    pass

                # method 2: if no results yet, click the magnifier/search button.
                # Schneider's search button is adjacent to the input. Find a
                # button near the input (not in the header) and click it.
                if _heading_count() < 0:
                    log.debug("schneider: Enter didn't trigger; trying button")
                    btn_found = False
                    # buttons physically near the cross-ref input
                    for loc in (
                        inp.locator(
                            "xpath=ancestor::div[1]//button[1]"),
                        inp.locator(
                            "xpath=following::button[1]"),
                        inp.locator("xpath=../button[1]"),
                        inp.locator("xpath=../following-sibling::*//button[1]"),
                        page.locator("button:has(svg)"),
                    ):
                        try:
                            el = loc.first
                            el.wait_for(state="visible", timeout=1_500)
                            in_header = el.evaluate(
                                "e => !!e.closest('header,nav,[role=banner]')")
                            if in_header:
                                continue
                            el.click(timeout=2_000)
                            btn_found = True
                            log.debug("schneider: clicked a search button")
                            page.wait_for_timeout(1_500)
                            if _heading_count() >= 0:
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    if not btn_found:
                        log.debug("schneider: no search button found near input")

                # method 3: still nothing — re-type the last char to nudge the
                # app's onChange, then Enter again (some React inputs need a
                # keystroke event, and fill() alone doesn't always fire it)
                if _heading_count() < 0:
                    log.debug("schneider: trying keystroke nudge + Enter")
                    try:
                        inp.click()
                        inp.press("End")
                        inp.press("Backspace")
                        page.wait_for_timeout(150)
                        inp.type(part[-1], delay=80)  # retype last char
                        page.wait_for_timeout(300)
                        inp.press("Enter")
                        page.wait_for_timeout(1_500)
                    except Exception:  # noqa: BLE001
                        pass

                # --- wait for results -----------------------------------------
                # Wait for the "N Results for X" heading AND for actual table
                # rows to render (the React app populates the table a moment
                # after the heading). Poll up to ~12s for a non-empty table.
                got_results = False
                for _ in range(40):              # ~12s max
                    page.wait_for_timeout(300)
                    try:
                        # heading present?
                        body_now = page.inner_text("body", timeout=1_000)
                        if re.search(r"\d+\s+Results?\s+for", body_now, re.I):
                            # are there data rows in a table yet?
                            rows = page.locator("table tr")
                            if rows.count() > 1:    # header + at least one row
                                got_results = True
                                break
                        # explicit "0 Results" / "no results" -> stop waiting
                        if re.search(r"\b0\s+Results?\b|no results", body_now,
                                     re.I):
                            log.debug("schneider: tool reports 0 results")
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if not got_results:
                    log.debug("schneider: results table didn't populate in time")
                page.wait_for_timeout(600)        # settle

                # detect expected count from the "N Results" heading
                expected = None
                try:
                    htxt = page.inner_text("body", timeout=1_500)
                    mexp = re.search(r"(\d+)\s+Results?\s+for", htxt, re.I)
                    if mexp:
                        expected = int(mexp.group(1))
                        log.debug("schneider: heading reports %d results",
                                  expected)
                except Exception:  # noqa: BLE001
                    pass

                html = page.content()
                results = _parse_schneider_table(html, part)
                log.debug("schneider: scraped %d rows%s", len(results),
                          f" (expected {expected})" if expected else "")
    except Exception as e:  # noqa: BLE001
        log.warning("schneider lookup failed for %r: %s", part, e)

    # optional manufacturer filter (competitor brand is in src_mfr/description)
    if mfr_filter:
        want = _norm_mfr(mfr_filter)
        kept: list[XrefResult] = []
        for r in results:
            hay = _norm_mfr(r.src_mfr + " " + r.description)
            if want in hay:
                r.raw["mfr_filtered"] = True
                kept.append(r)
        log.debug("schneider: mfr_filter %r kept %d of %d",
                  mfr_filter, len(kept), len(results))
        results = kept

    if results and db is not False:
        _persist(results, source="tools.se.app/xref", db=db)

    return results


def _parse_schneider_table(html: str, src_part: str) -> list[XrefResult]:
    """Parse the Schneider cross-reference results table.

    Confirmed column order from live page screenshot:
        0: Competitor or Obs Cat Num  (part + manufacturer + description,
           multi-line, e.g. 'BR130 / CUTLER-HAMMER (EATON) / MINIATURE...')
        1: SE Catalog Number          (the Schneider equivalent, e.g. HOM130)
        2: SE Cat# Details            (stock / price / discount schedule)
        3: Notes                      (e.g. 'Direct Replacement')

    'Direct Replacement' in Notes -> ul_classified + higher confidence.
    """
    results: list[XrefResult] = []
    row_re  = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_re = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>",
                         re.IGNORECASE | re.DOTALL)

    def clean(s: str) -> str:
        # keep line structure by turning block tags into separators first
        s = re.sub(r"</(div|p|br|li)>", " | ", s, flags=re.IGNORECASE)
        s = re.sub(r"<br\s*/?>", " | ", s, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ",
                      re.sub(r"<[^>]+>", " ", s)).strip(" |")

    seen: set[str] = set()
    for row_m in row_re.finditer(html):
        cells = [clean(c.group(1)) for c in
                 cell_re.finditer(row_m.group(1))]
        if len(cells) < 2:
            continue

        comp_cell = cells[0]          # competitor part + mfr + desc
        se_cat     = cells[1].split("|")[0].strip() if cells[1] else ""
        details    = cells[2] if len(cells) > 2 else ""
        notes      = cells[3] if len(cells) > 3 else ""

        # skip header / empty
        if (not se_cat or se_cat.lower() in ("se catalog number", "-")
                or "competitor" in comp_cell.lower()[:20]):
            continue

        # split the competitor cell into part / mfr / description by '|'
        parts = [x.strip() for x in comp_cell.split("|") if x.strip()]
        comp_part = parts[0] if parts else comp_cell
        comp_mfr  = parts[1] if len(parts) > 1 else "competitor"
        comp_desc = parts[2] if len(parts) > 2 else ""

        # confidence/type from the Notes column
        notes_low = notes.lower()
        if "direct replacement" in notes_low:
            equiv_type = "ul_classified"
            conf_hint  = True
        else:
            equiv_type = "spec_equivalent"
            conf_hint  = False

        key = f"{comp_part}|{se_cat}"
        if key in seen:
            continue
        seen.add(key)

        results.append(XrefResult(
            src_part=comp_part,
            src_mfr=comp_mfr[:80],
            equiv_part=se_cat,
            equiv_mfr="Schneider Electric",
            description=comp_desc[:120],
            notes=(f"{notes} | {details}" if notes and notes != "-"
                   else details)[:160],
            source_url="https://tools.se.app/xref",
            raw={"se_details": details, "schneider_note": notes,
                 "direct_replacement": conf_hint, "equiv_type": equiv_type},
        ))

    return results


# --- combined lookup ---------------------------------------------------------
def lookup_all(part: str, *, brand: str = DEFAULT_HUBBELL_BRAND,
               mfr_filter: str | None = None,
               db: str | None = None) -> dict[str, list[XrefResult]]:
    """Run Southwire, Hubbell, and Schneider lookups; return combined results.
    mfr_filter applies to Southwire and Schneider (Hubbell already returns a
    single brand)."""
    return {
        "southwire": lookup_southwire(part, mfr_filter=mfr_filter, db=db),
        "hubbell":   lookup_hubbell(part, brand=brand, db=db),
        "schneider": lookup_schneider(part, mfr_filter=mfr_filter, db=db),
    }


# --- CLI ---------------------------------------------------------------------
def _print_results(vendor: str, results: list[XrefResult]) -> None:
    if not results:
        print(f"  {vendor}: no equivalents found")
        return
    print(f"  {vendor} ({len(results)} result(s)):")
    for r in results:
        print(f"    -> {r}")


def _run_setup() -> int:
    """Open each vendor tool in a VISIBLE browser using the persistent profile,
    so the user can accept cookies / consent dialogs once. Whatever they accept
    is saved to PROFILE_DIR and remembered on all future runs (even headless),
    which makes sites like Schneider stop showing their consent modal.

    The browser stays open for each site until the user presses Enter, giving
    them time to click 'Accept All' / tick consent / 'Submit Consent'.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — pip install playwright && "
              "playwright install chromium")
        return 1

    if not PROFILE_DIR or PROFILE_DIR.lower() == "none":
        print("Persistent profile is disabled (XREF_PROFILE=none). "
              "Set XREF_PROFILE to a folder to use setup.")
        return 1

    global HEADLESS
    HEADLESS = False   # setup is always visible

    sites = [
        ("Schneider", "https://tools.se.app/xref/v3/public/prod/v3-1/"
                       "index.html?lang=EN_US"),
        ("Southwire", "https://www.southwire.com/cross-reference"),
        ("Hubbell/RACO", "https://www.hubbell.com/raco/en/"
                         "part-cross-reference"),
    ]

    print(f"\nSaving browser data to: {PROFILE_DIR}")
    print("A browser window will open for each tool. Accept any cookie and "
          "consent prompts (click 'Accept All', tick 'I consent', click "
          "'Submit Consent'), then return here and press Enter.\n")

    with sync_playwright() as p:
        for name, url in sites:
            print(f"--- {name} ---")
            try:
                with _BrowserSession(p) as page:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=NAV_TIMEOUT_MS)
                    print(f"  Opened {url}")
                    print("  Accept all prompts in the browser, then press "
                          "Enter here to continue...")
                    try:
                        input()
                    except EOFError:
                        page.wait_for_timeout(8_000)
            except Exception as e:  # noqa: BLE001
                print(f"  ({name} setup issue: {e})")
    print("\nSetup complete. Consent/cookies saved — future runs should skip "
          "the prompts. You can now run normal lookups (headless is fine).")
    return 0


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="vendor_xref.py",
        description="Targeted vendor cross-reference adapters (Southwire, Hubbell/RACO).")
    ap.add_argument("vendor",
                    choices=["southwire", "hubbell", "raco", "schneider",
                             "se", "all", "setup"],
                    help="which tool to query ('raco'=hubbell --brand raco; "
                         "'se'=schneider). 'setup' opens a visible browser to "
                         "accept cookies/consent once and save them.")
    ap.add_argument("part", nargs="?", default=None,
                    help="competitor part number (e.g. '257'), or "
                         "'Brand PartNo' (e.g. 'Raco 257') to auto-set the "
                         "manufacturer filter. Omit for 'setup'.")
    ap.add_argument("--mfr", default=None,
                    help="manufacturer filter for Southwire (keeps only rows "
                         "whose brand contains this, e.g. --mfr raco)")
    ap.add_argument("--brand", default=DEFAULT_HUBBELL_BRAND,
                    help=f"hubbell brand slug (default: {DEFAULT_HUBBELL_BRAND}). "
                         f"options: {', '.join(HUBBELL_BRANDS)}")
    ap.add_argument("--db", default=None,
                    help=f"cross_reference DB path (default: {DB_PATH})")
    ap.add_argument("--no-store", action="store_true",
                    help="print results but do not persist to DB")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--show-browser", action="store_true",
                    help="run Chromium in non-headless mode (for debugging)")
    args = ap.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s")

    if args.show_browser:
        global HEADLESS
        HEADLESS = False

    # v0.2.14: 'setup' opens each tool in a VISIBLE persistent-profile browser
    # so the user can accept cookies/consent once; those are saved to the
    # profile dir and remembered on all future (even headless) runs.
    if args.vendor == "setup":
        return _run_setup()

    db = False if args.no_store else args.db

    if not args.part:
        print("error: a part number is required (omit only for 'setup').")
        return 2

    # v0.2.8: if part is 'Brand PartNo' (two tokens, first is non-numeric),
    # split it: first token becomes the mfr filter, rest the part number.
    part = args.part.strip()
    mfr_filter = args.mfr
    toks = part.split()
    if mfr_filter is None and len(toks) == 2 and not toks[0][0].isdigit():
        mfr_filter, part = toks[0], toks[1]
        print(f"(parsed brand={mfr_filter!r} part={part!r})")

    print(f"Looking up: {part!r}"
          + (f"  [mfr filter: {mfr_filter}]" if mfr_filter else ""))
    vendor = args.vendor
    if vendor == "raco":
        vendor = "hubbell"
        if args.brand == DEFAULT_HUBBELL_BRAND:
            args.brand = "raco"
    if vendor == "se":
        vendor = "schneider"

    if vendor == "southwire":
        results = lookup_southwire(part, mfr_filter=mfr_filter, db=db)
        _print_results("Southwire", results)
    elif vendor == "hubbell":
        results = lookup_hubbell(part, brand=args.brand, db=db)
        _print_results(f"Hubbell/{args.brand}", results)
    elif vendor == "schneider":
        results = lookup_schneider(part, mfr_filter=mfr_filter, db=db)
        _print_results("Schneider", results)
    elif vendor == "all":
        all_r = lookup_all(part, brand=args.brand,
                           mfr_filter=mfr_filter, db=db)
        for v, r in all_r.items():
            _print_results(v, r)

    if not args.no_store and db is not False:
        print("\nResults stored in cross_reference DB (if any found).")
        print("Confirm good results:  "
              f"py -m mainbox_brain.cross_reference confirm {part!r} <equiv_part>")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
