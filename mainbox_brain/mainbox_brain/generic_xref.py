#!/usr/bin/env python3
"""
generic_xref.py  -  run a cross-reference lookup on any TAUGHT site.
v0.1.0

Reads a site "recipe" produced by site_teacher.py and replays it for a given
part number: fills the captured search field, submits (button if one was
captured, else relies on live-as-you-type filtering), then walks every row of
the captured results table reading the mapped columns (manufacturer, competitor
part, equivalent part, and any optional description/stock/price/notes). If a
next-page button was captured, it paginates and accumulates. Results are stored
into the cross_reference SQLite store (shared with vendor_xref.py).

No per-site code, no vision model — the recipe drives everything.

Requires Playwright + a recipe saved under XREF_SITES_DIR (default ./xref_sites).

CLI:
    python generic_xref.py southwire "257"
    python generic_xref.py southwire "257" --mfr raco
    python generic_xref.py southwire "257" --no-store --debug --show-browser
    python generic_xref.py --list
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
import argparse
from dataclasses import dataclass, field

# shared helpers (normalize, mfr matching) — one source of truth across modules
try:
    from . import xref_common as xc
except ImportError:
    try:
        import xref_common as xc
    except ImportError:
        xc = None

__version__ = "0.1.11"  # use shared xref_common helpers (normalize, mfr-match) to avoid drift

log = logging.getLogger("generic_xref")

# --- config (kept compatible with vendor_xref.py) ----------------------------
HEADLESS       = True
NAV_TIMEOUT_MS = 25_000
SLOW_MO_MS     = 0
DB_PATH        = os.environ.get("XREF_DB", "cross_references.db")
SITES_DIR      = os.environ.get("XREF_SITES_DIR", "xref_sites")
PROFILE_DIR    = os.environ.get("XREF_PROFILE", ".xref_browser_profile")

_REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# column-key -> the recipe step name that holds its capture
_COL_KEYS = {
    "manufacturer": "col_manufacturer",
    "competitor":   "col_competitor",
    "equivalent":   "col_equivalent",
    "description":  "col_description",
    "stock":        "col_stock",
    "price":        "col_price",
    "notes":        "col_notes",
}


@dataclass
class XrefResult:
    src_part:   str
    src_mfr:    str
    equiv_part: str
    equiv_mfr:  str
    description: str = ""
    notes:      str = ""
    source_url: str = ""
    raw:        dict = field(default_factory=dict)

    def __str__(self) -> str:
        d = f" — {self.description[:60]}" if self.description else ""
        return f"{self.equiv_mfr or '?'} {self.equiv_part}{d}"


class MfrChoiceNeeded:
    """Returned by lookup() (when interactive=False) for a manufacturer-required
    site where the manufacturer wasn't resolved. Carries the option list so a
    caller can present a numbered picker."""
    def __init__(self, slug, part, options):
        self.slug = slug
        self.part = part
        self.options = options          # list of {value,label}

    def __repr__(self):
        return (f"MfrChoiceNeeded(slug={self.slug!r}, part={self.part!r}, "
                f"{len(self.options)} options)")


# --- recipe loading ----------------------------------------------------------
def load_recipe(slug: str) -> dict | None:
    import json
    path = os.path.join(SITES_DIR, f"{slug}.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_recipes() -> list[str]:
    if not os.path.isdir(SITES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SITES_DIR)
                  if f.endswith(".json"))


# --- browser -----------------------------------------------------------------
# visibility modes a recipe can request via "visibility" (or legacy
# needs_visible=true, which maps to "offscreen"):
#   "headless"     - normal headless (fastest; some SPAs detect & block it)
#   "offscreen"    - REAL visible browser parked far off-screen at -2400,-2400
#                    so the site sees genuine Chrome but you can't see/click it
#   "visible"      - real visible browser, on-screen (what you see now)
#   "new_headless" - Chrome's newer headless engine (harder to detect than old
#                    headless; invisible). Try this for sites that block plain
#                    headless but you'd rather not show a window.
_OFFSCREEN_POS = "-2400,-2400"


def _open_page(playwright, headless=None, visibility=None):
    """Open a page using the shared persistent profile when configured.

    visibility (str) overrides headless when given; see modes above. If only
    `headless` (bool) is passed, it maps to "headless"/"visible" as before."""
    # resolve the effective mode
    if visibility is None:
        if headless is None:
            visibility = "headless" if HEADLESS else "visible"
        else:
            visibility = "headless" if headless else "visible"

    use_profile = bool(PROFILE_DIR) and PROFILE_DIR.lower() != "none"
    args = ["--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"]

    # translate mode -> launch settings
    launch_headless = False
    if visibility == "headless":
        launch_headless = True
    elif visibility == "new_headless":
        # Chrome's new headless: real engine, much harder to detect. Playwright
        # enables it via the chromium arg below while keeping headless=False so
        # it doesn't fall back to the old headless implementation.
        args.append("--headless=new")
        launch_headless = False
    elif visibility == "offscreen":
        # real visible browser, parked far off the visible desktop
        args.append(f"--window-position={_OFFSCREEN_POS}")
        args.append("--window-size=1440,900")
        launch_headless = False
    else:  # "visible"
        launch_headless = False

    log.debug("browser visibility mode: %s (headless=%s)", visibility,
              launch_headless)

    if use_profile:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        ctx = playwright.chromium.launch_persistent_context(
            PROFILE_DIR, headless=launch_headless, slow_mo=SLOW_MO_MS,
            user_agent=_REAL_UA, viewport={"width": 1440, "height": 900},
            locale="en-US", args=args)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',"
                            "{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, None, page
    browser = playwright.chromium.launch(headless=launch_headless,
                                         slow_mo=SLOW_MO_MS, args=args)
    ctx = browser.new_context(user_agent=_REAL_UA,
                              viewport={"width": 1440, "height": 900})
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',"
                        "{get:()=>undefined});")
    return ctx, browser, ctx.new_page()


# --- the core: replay a recipe for a part ------------------------------------
def lookup(slug: str, part: str, *, mfr_filter: str | None = None,
           strict_match: bool = True, mfr_pick: int | None = None,
           interactive: bool = True,
           db: str | None = None) -> list[XrefResult]:
    """Replay the taught recipe `slug` to cross-reference `part`.

    For sites that require a manufacturer dropdown (recipe mfr_required):
      - mfr_filter is fuzzy-matched against the dropdown options first;
      - mfr_pick (1-based) selects an option directly by number;
      - if neither resolves it and interactive=True, the user is shown a
        numbered list to choose from; if interactive=False, a special result
        carrying the options is returned so a caller (e.g. chat) can ask.

    strict_match: when True (default), only keep rows whose competitor part
    actually contains the searched term (normalized) — filters out the fuzzy
    near-misses some vendor tools return (e.g. Hubbell returning 'BR20' for a
    'BR320' search). Set False to keep every row the tool returns."""
    recipe = load_recipe(slug)
    if not recipe:
        log.warning("no taught site %r (looked in %s)", slug, SITES_DIR)
        return []

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("playwright not installed — pip install playwright && "
                    "playwright install chromium")
        return []

    steps = recipe.get("steps", {})
    url = recipe.get("url", "")
    search_mode = recipe.get("search_mode", "field")
    url_pattern = recipe.get("search_url_pattern", "")
    # some sites detect headless Chrome and won't render. A recipe can request
    # a visibility mode: "headless" (default), "offscreen" (real browser parked
    # off-screen), "visible" (on-screen), or "new_headless". Legacy
    # needs_visible=true maps to "offscreen" (real browser, but out of the way).
    visibility = recipe.get("visibility")
    if not visibility:
        visibility = "offscreen" if recipe.get("needs_visible") else None
    # sites that require selecting a manufacturer before searching
    mfr_required = bool(recipe.get("mfr_required"))
    mfr_select = steps.get("mfr_select")
    si = steps.get("search_input")
    comp = steps.get("col_competitor")
    equiv = steps.get("col_equivalent")
    # column captures are always required; the search step depends on mode
    if not (comp and equiv):
        log.warning("recipe %r missing required column steps "
                    "(col_competitor / col_equivalent)", slug)
        return []
    if search_mode == "url":
        if not url_pattern:
            log.warning("recipe %r is url-mode but has no search_url_pattern",
                        slug)
            return []
    else:
        if not si:
            log.warning("recipe %r (field mode) missing search_input", slug)
            return []

    table_sel = comp.get("tableSelector") or equiv.get("tableSelector")
    # map of column-key -> colIndex (only those that were captured)
    col_idx: dict[str, int] = {}
    for ckey, stepname in _COL_KEYS.items():
        st = steps.get(stepname)
        if st and st.get("colIndex") is not None:
            col_idx[ckey] = st["colIndex"]

    # CARD MODE detection: if the result captures are NOT table cells (no
    # colIndex / tableSelector) but have full selectors, the results are a grid
    # of cards (e.g. Legrand's div.match-record), not an HTML table. Build a
    # card spec: a repeating container selector + a field->class map.
    card_spec = None
    if not col_idx and not table_sel and comp.get("selector"):
        card_spec = _build_card_spec(steps)
        if card_spec:
            log.debug("%s: card-mode (container %r, fields %s)", slug,
                      card_spec["container"], list(card_spec["fields"]))

    results: list[XrefResult] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as p:
            # honor the recipe's visibility mode (headless-detecting sites like
            # Schneider can run offscreen: real browser, parked off-screen)
            ctx, browser, page = _open_page(p, visibility=visibility)
            if visibility and visibility != "headless" and HEADLESS:
                log.debug("%s: using visibility mode %r", slug, visibility)
            try:
                inp = None
                if search_mode == "url":
                    # Type B: navigate directly to the results URL with the part
                    # (and manufacturer, if the pattern needs it) substituted in.
                    import urllib.parse as _up
                    term = (_up.quote(part)
                            if recipe.get("search_url_encode") else part)
                    target = url_pattern.replace("{part}", term)

                    # {mfr} substitution for two-parameter sites (e.g.
                    # Arlington: ?m=topaz&id=100). Resolve the manufacturer to
                    # the value the site expects.
                    if "{mfr}" in target:
                        mfr_value = _resolve_url_mfr(
                            mfr_filter, mfr_pick, mfr_select, interactive, slug)
                        if isinstance(mfr_value, _InvalidMfr):
                            # user gave a manufacturer not on the site's list
                            valid = ", ".join(o["label"]
                                              for o in mfr_value.options[:30])
                            log.warning("%s: %r is not a valid manufacturer "
                                        "for this site", slug, mfr_value.given)
                            print(f"\n  '{mfr_value.given}' isn't a "
                                  f"manufacturer {slug} cross-references.")
                            print(f"  Valid options: {valid}")
                            return []
                        if mfr_value is None:
                            # interactive=False and unresolved -> signal caller
                            opts = (mfr_select or {}).get("options", [])
                            if opts:
                                return MfrChoiceNeeded(slug, part, opts)
                            log.warning("%s: URL needs a manufacturer but none "
                                        "was given (use --mfr)", slug)
                            return []
                        mv = (_up.quote(mfr_value)
                              if recipe.get("search_url_encode") else mfr_value)
                        target = target.replace("{mfr}", mv)

                    log.debug("%s: url-mode navigate %s", slug, target)
                    page.goto(target, wait_until="domcontentloaded",
                              timeout=NAV_TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except PWTimeout:
                        pass
                else:
                    # Type A: load the base page and drive the search form
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=NAV_TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except PWTimeout:
                        pass

                    # 0) manufacturer dropdown (sites like Legrand require it)
                    if mfr_required and mfr_select:
                        # gather options: prefer those captured at teach time,
                        # else read them live now
                        options = mfr_select.get("options") or \
                            _read_live_options(page, mfr_select.get("selector",
                                                                    ""))
                        if not options:
                            log.warning("%s: manufacturer required but no "
                                        "options found", slug)
                            return []
                        chosen = None
                        # (a) direct pick by number
                        if mfr_pick is not None and 1 <= mfr_pick <= len(options):
                            chosen = options[mfr_pick - 1]
                        # (b) fuzzy-match the provided --mfr
                        if chosen is None and mfr_filter:
                            chosen = _match_mfr_option(mfr_filter, options)
                        # (c) still unresolved -> picker (interactive) or signal
                        if chosen is None:
                            if interactive:
                                chosen = _prompt_mfr_choice(slug, options)
                            else:
                                return MfrChoiceNeeded(slug, part, options)
                        if chosen is None:
                            log.warning("%s: no manufacturer chosen", slug)
                            return []
                        log.debug("%s: selecting manufacturer %r", slug,
                                  chosen["label"])
                        if not _select_mfr(page, mfr_select, chosen):
                            log.warning("%s: couldn't select manufacturer %r",
                                        slug, chosen["label"])
                        page.wait_for_timeout(600)

                    # 1) fill the search field (optional when a manufacturer was
                    # selected and no part was given — search by mfr alone)
                    inp = None
                    if part or not mfr_required:
                        field_sel = si["selector"] if si else ""
                        if field_sel:
                            try:
                                inp = page.locator(field_sel).first
                                inp.wait_for(state="visible", timeout=8_000)
                            except Exception:  # noqa: BLE001
                                ph = si.get("placeholder") if si else ""
                                if ph:
                                    try:
                                        inp = page.get_by_placeholder(ph).first
                                        inp.wait_for(state="visible",
                                                     timeout=4_000)
                                    except Exception:  # noqa: BLE001
                                        inp = None
                        if inp is None and not mfr_required:
                            log.warning("%s: search field not found", slug)
                            return []
                        if inp is not None and part:
                            inp.click()
                            inp.fill("")
                            inp.type(part, delay=40)
                            page.wait_for_timeout(800)

                    # 2) submit if a button was captured; else live-filter
                    submit = steps.get("submit")
                    if submit and submit.get("selector"):
                        for sel in (submit["selector"],
                                    "button:has-text('Search')"):
                            try:
                                b = page.locator(sel).first
                                b.wait_for(state="visible", timeout=3_000)
                                b.click(timeout=2_000)
                                break
                            except Exception:  # noqa: BLE001
                                continue
                    else:
                        # some tools submit on Enter
                        try:
                            if inp is not None:
                                inp.press("Enter")
                        except Exception:  # noqa: BLE001
                            pass

                # wait briefly for results to render. In card mode wait for the
                # card container; otherwise for any table cell.
                try:
                    if card_spec:
                        page.wait_for_selector(card_spec["container"],
                                               timeout=8_000)
                    else:
                        page.wait_for_selector("table tr td", timeout=6_000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(300)

                # 3) scrape page-by-page (table mode or card mode)
                next_btn = steps.get("next_page")
                max_pages = 25
                for pageno in range(1, max_pages + 1):
                    if card_spec:
                        added = _scrape_cards(page, card_spec, part, results,
                                              seen, url,
                                              strict_match=strict_match)
                    else:
                        added = _scrape_rows(page, table_sel, col_idx, part,
                                             results, seen, url,
                                             strict_match=strict_match)
                    log.debug("%s: page %d +%d rows (%d total)",
                              slug, pageno, added, len(results))
                    # advance if we have a next button and it's enabled
                    if not (next_btn and next_btn.get("selector")):
                        break
                    if not _click_next(page, next_btn["selector"]):
                        break
                    page.wait_for_timeout(800)

                if not results:
                    log.debug("%s: no rows scraped for %r", slug, part)
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if browser:
                        browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        log.warning("%s lookup failed for %r: %s", slug, part, e)

    # optional manufacturer filter
    if mfr_filter:
        want = _norm(mfr_filter)
        kept = []
        for r in results:
            if want in _norm(r.src_mfr + " " + r.description):
                r.raw["mfr_filtered"] = True
                kept.append(r)
        log.debug("%s: mfr_filter %r kept %d of %d", slug, mfr_filter,
                  len(kept), len(results))
        results = kept

    if results and db is not False:
        _persist(results, source=f"taught:{slug}", query_part=part, db=db)

    return results


def _find_table(page, table_sel):
    """Return a Playwright locator for the results table, resilient to
    session-specific IDs that change between the teach run and now.

    Order:
      1. the exact captured selector (if it matches and has rows)
      2. a stabilized form: if the selector ends in an id with a volatile
         hex/number suffix (e.g. #ddtTable_0C58... or table#ddtTable_0C58...),
         convert to a starts-with match on the stable prefix
         (table[id^='ddtTable_'])
      3. the table on the page with the most data rows (largest result grid)
    Returns (locator_or_None, how_str).
    """
    # 1) exact captured selector
    if table_sel:
        try:
            loc = page.locator(table_sel)
            if loc.count() >= 1 and loc.first.locator("tr").count() > 1:
                return loc.first, "exact"
        except Exception:  # noqa: BLE001
            pass

        # 2) stabilize a volatile id: pull the last '#id' or 'tag#id' segment
        m = re.search(r"#([A-Za-z][\w-]*)", table_sel)
        if m:
            full_id = m.group(1)
            # split a trailing volatile suffix (long hex / digit run) off a
            # stable prefix: ddtTable_0C58393D... -> prefix 'ddtTable_'
            pm = re.match(r"^(.*?[_-])([0-9A-Fa-f]{6,}|\d{4,})$", full_id)
            if pm:
                prefix = pm.group(1)
                for sel in (f"table[id^='{prefix}']", f"[id^='{prefix}']"):
                    try:
                        loc = page.locator(sel)
                        if loc.count() >= 1 and \
                                loc.first.locator("tr").count() > 1:
                            return loc.first, f"prefix:{prefix}"
                    except Exception:  # noqa: BLE001
                        continue

    # 3) fall back to the table with the most rows
    try:
        tables = page.locator("table")
        tcount = tables.count()
        best = None
        best_rows = 1
        for i in range(min(tcount, 12)):
            t = tables.nth(i)
            try:
                r = t.locator("tr").count()
            except Exception:  # noqa: BLE001
                r = 0
            if r > best_rows:
                best_rows, best = r, t
        if best is not None:
            return best, f"largest({best_rows} rows)"
    except Exception:  # noqa: BLE001
        pass
    return None, "none"


# field-key -> the recipe step that holds its capture (for card mode)
_CARD_FIELD_STEPS = {
    "competitor": "col_competitor",
    "equivalent": "col_equivalent",
    "description": "col_description",
    "manufacturer": "col_manufacturer",
    "stock": "col_stock",
    "price": "col_price",
    "notes": "col_notes",
}


def _build_card_spec(steps: dict) -> dict | None:
    """From captured (non-table) result selectors, derive a card spec:
      {container: "<selector for one card, repeatable>",
       fields: {competitor: "<relative selector>", equivalent: ..., ...}}

    The captured selectors share a common path down to the per-card container
    (e.g. div.match-record), then diverge by a final element with a distinctive
    class. We use the common prefix as the container (with nth-of-type stripped
    so it matches ALL cards) and each field's trailing segments as its relative
    selector within a card."""
    sels = {}
    for fk, stepname in _CARD_FIELD_STEPS.items():
        st = steps.get(stepname)
        if st and st.get("selector"):
            sels[fk] = st["selector"]
    if "competitor" not in sels or "equivalent" not in sels:
        return None

    seg_lists = {fk: [s.strip() for s in sel.split(">")]
                 for fk, sel in sels.items()}
    comp_segs = seg_lists["competitor"]
    equiv_segs = seg_lists["equivalent"]
    common = []
    for a, b in zip(comp_segs, equiv_segs):
        if a == b:
            common.append(a)
        else:
            break
    if not common:
        return None

    # The container is the common prefix (the repeating card). Strip
    # :nth-of-type from ALL segments so it matches EVERY card, not just the
    # specific one the user clicked. (The repeating element may not be the last
    # common segment — e.g. match-record > match-data are both shared — so we
    # must strip throughout, then let the descendant field selectors pick the
    # right element within each card.)
    container_segs = [re.sub(r":nth-of-type\(\d+\)", "", s) for s in common]
    container = " > ".join(container_segs)

    fields = {}
    depth = len(common)
    for fk, segs in seg_lists.items():
        rel = segs[depth:]
        if not rel:
            fields[fk] = ":scope"
            continue
        rel = [re.sub(r":nth-of-type\(\d+\)", "", s) for s in rel]
        fields[fk] = " ".join(rel)
    return {"container": container, "fields": fields}


def _split_sku_brand(raw: str) -> tuple[str, str]:
    """Parse 'SKU: DR20WHI | Hubbell' -> ('DR20WHI', 'Hubbell'). Tolerant of a
    missing 'SKU:' prefix or missing brand."""
    if not raw:
        return "", ""
    s = re.sub(r"^\s*SKU\s*:\s*", "", raw.strip(), flags=re.IGNORECASE)
    brand = ""
    if "|" in s:
        left, _, right = s.partition("|")
        s, brand = left.strip(), right.strip()
    return s.strip(), brand


def _scrape_cards(page, card_spec, part, results, seen, url,
                  strict_match=True) -> int:
    """Scrape a card-grid layout (e.g. Legrand). Walk each card container and
    read fields by their relative selectors. Returns count of new rows."""
    container = card_spec["container"]
    fields = card_spec["fields"]
    added = 0
    try:
        cards = page.locator(container)
        n = cards.count()
    except Exception:  # noqa: BLE001
        return 0
    log.debug("  card-mode: %d cards via %r", n, container)

    def field_text(card, key):
        rel = fields.get(key)
        if not rel:
            return ""
        try:
            if rel == ":scope":
                return (card.inner_text(timeout=500) or "").strip()
            el = card.locator(rel).first
            if el.count() == 0:
                return ""
            return (el.inner_text(timeout=500) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    for i in range(n):
        try:
            card = cards.nth(i)
        except Exception:  # noqa: BLE001
            continue
        comp_raw = field_text(card, "competitor")
        equiv_raw = field_text(card, "equivalent")
        if not comp_raw or not equiv_raw:
            continue
        comp_part, comp_brand = _split_sku_brand(comp_raw)
        equiv_part, equiv_brand = _split_sku_brand(equiv_raw)
        if not comp_part or not equiv_part:
            continue
        if strict_match and part:
            nq = _norm(part)
            if nq and nq not in _norm(comp_part):
                log.debug("  drop irrelevant card: searched %r, competitor %r",
                          part, comp_part)
                continue
        key = f"{comp_part}|{equiv_part}"
        if key in seen:
            continue
        seen.add(key)
        results.append(XrefResult(
            src_part=comp_part,
            src_mfr=comp_brand or "competitor",
            equiv_part=equiv_part,
            equiv_mfr=equiv_brand or "",
            description=field_text(card, "description"),
            notes=" | ".join(x for x in (field_text(card, "stock"),
                                         field_text(card, "price"),
                                         field_text(card, "notes")) if x),
            source_url=url,
            raw={"card": True},
        ))
        added += 1
    return added


def _scrape_rows(page, table_sel, col_idx, part, results, seen, url,
                 strict_match=True) -> int:
    """Read every row of the current table page, append new XrefResults.
    Returns the number of new rows added (dedup by competitor|equivalent)."""
    added = 0
    scope, how = _find_table(page, table_sel)
    if scope is None:
        log.debug("  no results table found on page")
        return 0
    log.debug("  using table via %s", how)
    try:
        rows = scope.locator("tr")
        n = rows.count()
    except Exception:  # noqa: BLE001
        return 0

    def cell(cells_txt: list[str], key: str) -> str:
        idx = col_idx.get(key)
        if idx is None or idx >= len(cells_txt):
            return ""
        return cells_txt[idx]

    for i in range(n):
        try:
            row = rows.nth(i)
            cell_loc = row.locator("td")
            cc = cell_loc.count()
            if cc == 0:
                continue   # header row (th) or empty
            cells_txt = []
            for j in range(cc):
                try:
                    cells_txt.append(
                        (cell_loc.nth(j).inner_text(timeout=500) or "").strip())
                except Exception:  # noqa: BLE001
                    cells_txt.append("")
        except Exception:  # noqa: BLE001
            continue

        comp_part = cell(cells_txt, "competitor")
        equiv_part = cell(cells_txt, "equivalent")
        if not comp_part or not equiv_part:
            continue
        # skip "no cross"/empty equivalents
        if re.search(r"no\s*cross|^-+$", equiv_part, re.I):
            continue

        # RELEVANCE GATE: the competitor part must actually contain what the
        # user searched. Some vendor tools (e.g. Hubbell) do fuzzy matching and
        # return near-misses — searching 'BR320' can return 'BR20', 'BY320'.
        # We require the normalized search term to appear in the normalized
        # competitor part, so 'BR320' keeps 'BR320W' but drops 'BR20'. Compare
        # on normalized values (strip separators + case) so 'BR-320' still
        # matches 'BR320'.
        if strict_match and part:
            nq = _norm(part)
            ncp = _norm(comp_part)
            if nq and nq not in ncp:
                log.debug("  drop irrelevant row: searched %r, competitor %r",
                          part, comp_part)
                continue

        key = f"{comp_part}|{equiv_part}"
        if key in seen:
            continue
        seen.add(key)

        results.append(XrefResult(
            src_part=comp_part,
            src_mfr=cell(cells_txt, "manufacturer") or "competitor",
            equiv_part=equiv_part,
            equiv_mfr="",      # the house brand; filled by caller/site name
            description=cell(cells_txt, "description"),
            notes=" | ".join(x for x in (cell(cells_txt, "stock"),
                                         cell(cells_txt, "price"),
                                         cell(cells_txt, "notes")) if x),
            source_url=url,
            raw={"all_cells": cells_txt},
        ))
        added += 1
    return added


def _click_next(page, selector: str) -> bool:
    """Click the captured next-page button if enabled. Verify the first data
    row changes so we know the page actually advanced."""
    try:
        before = _first_row_sig(page)
        btn = page.locator(selector).first
        btn.wait_for(state="visible", timeout=1_500)
        cls = (btn.get_attribute("class") or "").lower()
        ad = btn.get_attribute("aria-disabled")
        dis = btn.get_attribute("disabled")
        if "disabled" in cls or ad == "true" or dis is not None:
            return False
        btn.scroll_into_view_if_needed(timeout=1_000)
        btn.click(timeout=2_000)
        # wait for rows to change
        for _ in range(15):
            page.wait_for_timeout(200)
            if _first_row_sig(page) != before:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _first_row_sig(page) -> str:
    try:
        return page.locator("table tr").nth(1).inner_text(timeout=800)[:60]
    except Exception:  # noqa: BLE001
        return ""


class _InvalidMfr:
    """Sentinel returned by _resolve_url_mfr when the user gave a manufacturer
    that doesn't match any captured option. Carries the valid options."""
    def __init__(self, given, options):
        self.given = given
        self.options = options


def _resolve_url_mfr(mfr_filter, mfr_pick, mfr_select, interactive, slug):
    """Resolve the manufacturer VALUE to substitute into a {mfr} URL pattern.

    Priority:
      1. If the recipe captured dropdown options, use them: --mfr-pick N, then
         fuzzy-match --mfr, then interactive picker. Returns the option's
         'value' (what the site's URL expects, e.g. 'topaz'). If --mfr was given
         but matches NOTHING, returns _InvalidMfr (caller errors with the list).
      2. If no options captured, use the raw --mfr lowercased (URLs like
         Arlington's use a lowercase brand token).
    Returns: value string | None (no mfr given) | _InvalidMfr (bad mfr).
    """
    options = (mfr_select or {}).get("options") or []
    if options:
        # pick by number
        if mfr_pick is not None:
            if 1 <= mfr_pick <= len(options):
                return options[mfr_pick - 1].get("value") or \
                    options[mfr_pick - 1].get("label")
            return _InvalidMfr(f"#{mfr_pick}", options)
        # fuzzy match the provided --mfr
        if mfr_filter:
            m = _match_mfr_option(mfr_filter, options)
            if m:
                return m.get("value") or m.get("label")
            # a manufacturer was given but doesn't match any option -> invalid
            return _InvalidMfr(mfr_filter, options)
        # nothing given: interactive picker, else None (caller signals)
        if interactive:
            chosen = _prompt_mfr_choice(slug, options)
            if chosen:
                return chosen.get("value") or chosen.get("label")
        return None
    # no captured options: use the raw brand, lowercased (URL convention)
    if mfr_filter:
        return mfr_filter.strip().lower()
    return None


def _prompt_mfr_choice(slug: str, options: list[dict]) -> dict | None:
    """Show a numbered list of manufacturers and read the user's pick from the
    console. Returns the chosen option or None."""
    print(f"\n{slug}: this site needs a manufacturer. Choose one:")
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o['label']}")
    try:
        ans = input("Enter the number (or blank to cancel): ").strip()
    except EOFError:
        return None
    if ans.isdigit():
        n = int(ans)
        if 1 <= n <= len(options):
            return options[n - 1]
    print("  (no valid choice)")
    return None


def _norm(s: str) -> str:
    if xc is not None:
        return xc.normalize(s)
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_mfr_option(mfr: str, options: list[dict]) -> dict | None:
    """Fuzzy-match a user-given manufacturer string to a dropdown option.
    Delegates to the shared xref_common implementation (one source of truth);
    falls back to a local copy if xref_common isn't importable."""
    if xc is not None:
        return xc.match_mfr_option(mfr, options)
    if not mfr or not options:
        return None
    want = _norm(mfr)
    for o in options:
        if _norm(o["label"]) == want:
            return o
    contains = [o for o in options if want and want in _norm(o["label"])]
    if len(contains) == 1:
        return contains[0]
    rev = [o for o in options if _norm(o["label"]) and
           _norm(o["label"]) in want]
    if not contains and len(rev) == 1:
        return rev[0]
    return None


def _read_live_options(page, selector: str) -> list[dict]:
    """Read options from a (possibly custom) dropdown live on the page. Tries a
    native <select> first; returns [{value,label}]."""
    try:
        opts = page.evaluate("""
            (sel) => {
              const el = document.querySelector(sel);
              if (!el) return [];
              const s = el.tagName === 'SELECT' ? el : el.closest('select') ||
                        (el.querySelector ? el.querySelector('select') : null);
              if (s) {
                return Array.from(s.options || [])
                  .map(o => ({value: o.value, label: (o.textContent||'').trim()}))
                  .filter(o => o.label && !/^select/i.test(o.label));
              }
              return [];
            }
        """, selector)
        return opts or []
    except Exception:  # noqa: BLE001
        return []


def _select_mfr(page, mfr_select: dict, option: dict) -> bool:
    """Select the given option in the manufacturer dropdown. Handles native
    <select> via select_option; returns True on success."""
    sel = mfr_select.get("selector")
    if not sel:
        return False
    try:
        loc = page.locator(sel).first
        loc.wait_for(state="visible", timeout=6_000)
        # native select: use select_option by value (fall back to label)
        try:
            loc.select_option(value=option["value"])
            return True
        except Exception:  # noqa: BLE001
            try:
                loc.select_option(label=option["label"])
                return True
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return False


# --- persist (compatible with cross_reference store) -------------------------
def _persist(results: list[XrefResult], *, source: str,
             query_part: str | None = None, db: str | None = None) -> None:
    """Store findings into the cross_reference store.

    IMPORTANT: src_part is the part the USER SEARCHED (query_part), not the
    competitor value from the vendor's table. This makes confirm/reject match
    what the user types (they searched 'TA-0', so reject keys on 'TA-0'). The
    table's own competitor part is preserved in the notes for reference."""
    try:
        import importlib
        try:
            from . import cross_reference as xr
        except ImportError:
            xr = importlib.import_module("cross_reference")
        for r in results:
            try:
                conf = 0.85 if r.raw.get("mfr_filtered") else 0.70
                # key on the user's query when we have it; fall back to the
                # table's competitor value otherwise
                src_part = query_part if query_part else r.src_part
                comp_note = (f" [table competitor: {r.src_mfr} {r.src_part}]"
                             if query_part and r.src_part and
                             r.src_part != query_part else "")
                xr.add(
                    part=src_part,
                    mfr=r.src_mfr if r.src_mfr != "competitor" else "Unknown",
                    equiv_part=r.equiv_part,
                    equiv_mfr=r.equiv_mfr or source.replace("taught:", ""),
                    equiv_type=xr.T_SPEC,
                    confidence=conf,
                    notes=f"via {source}: {r.description[:70]}{comp_note}",
                    db=db or DB_PATH,
                )
            except Exception as e:  # noqa: BLE001
                log.debug("persist failed for %s: %s", r.equiv_part, e)
    except Exception as e:  # noqa: BLE001
        log.warning("cross_reference store unavailable: %s", e)


# --- CLI ---------------------------------------------------------------------
def _print(slug: str, results: list[XrefResult]) -> None:
    if not results:
        print(f"  {slug}: no equivalents found")
        return
    print(f"  {slug} ({len(results)} result(s)):")
    for r in results:
        mfr = f"[{r.src_mfr[:30]}] " if r.src_mfr and \
            r.src_mfr != "competitor" else ""
        extra = f"  {r.notes}" if r.notes else ""
        print(f"    {mfr}{r.src_part} -> {r.equiv_part}{extra}")


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="generic_xref.py",
        description="Run a cross-reference lookup on a TAUGHT site recipe.")
    ap.add_argument("slug", nargs="?", help="taught site slug "
                    "(see --list). e.g. southwire")
    ap.add_argument("part", nargs="?", help="competitor part number")
    ap.add_argument("--mfr", default=None, help="manufacturer filter")
    ap.add_argument("--mfr-pick", type=int, default=None,
                    help="for manufacturer-required sites: pick the dropdown "
                         "option by number directly (e.g. --mfr-pick 3)")
    ap.add_argument("--loose", action="store_true",
                    help="keep ALL rows the vendor returns, even fuzzy "
                         "near-misses (default: only keep rows whose competitor "
                         "part contains your search term)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-store", action="store_true")
    ap.add_argument("--list", action="store_true",
                    help="list taught sites and exit")
    ap.add_argument("--set-visibility",
                    choices=["headless", "offscreen", "visible", "new_headless"],
                    default=None,
                    help="set this site's browser visibility mode, then exit. "
                         "offscreen = real browser parked off-screen (good for "
                         "headless-detecting sites like Schneider, invisible to "
                         "you); new_headless = harder-to-detect headless. e.g. "
                         "generic_xref.py schneider --set-visibility offscreen")
    ap.add_argument("--set-visible", choices=["on", "off"], default=None,
                    help="(legacy) on = offscreen mode, off = headless")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--show-browser", action="store_true")
    args = ap.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s")

    # set visibility mode: write it to the recipe and exit
    if args.set_visibility is not None or args.set_visible is not None:
        if not args.slug:
            ap.error("setting visibility needs a site slug, e.g. "
                     "generic_xref.py schneider --set-visibility offscreen")
        rec = load_recipe(args.slug)
        if not rec:
            print(f"No taught site {args.slug!r}.")
            return 1
        if args.set_visibility is not None:
            mode = args.set_visibility
        else:  # legacy on/off
            mode = "offscreen" if args.set_visible == "on" else "headless"
        rec["visibility"] = mode
        rec.pop("needs_visible", None)   # supersede the old boolean
        import json
        path = os.path.join(SITES_DIR, f"{args.slug}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        desc = {
            "headless": "run headless (fastest, may be blocked by some sites)",
            "offscreen": "run a REAL browser parked off-screen (invisible to "
                         "you, looks legit to the site)",
            "visible": "run a visible on-screen browser",
            "new_headless": "use Chrome's harder-to-detect new headless mode",
        }.get(mode, mode)
        print(f"{args.slug}: visibility = {mode!r} — {desc}.")
        return 0

    if args.list:
        sites = list_recipes()
        if not sites:
            print(f"No taught sites yet (dir: {SITES_DIR}). "
                  "Teach one with site_teacher.py.")
        else:
            print("Taught sites:")
            for s in sites:
                print(f"  {s}")
        return 0

    if not args.slug or not args.part:
        ap.error("slug and part are required (or use --list)")

    if args.show_browser:
        global HEADLESS
        HEADLESS = False

    db = False if args.no_store else args.db
    print(f"Looking up {args.part!r} on taught site {args.slug!r}"
          + (f"  [mfr: {args.mfr}]" if args.mfr else ""))
    results = lookup(args.slug, args.part, mfr_filter=args.mfr,
                     strict_match=not args.loose, mfr_pick=args.mfr_pick,
                     interactive=True, db=db)
    # manufacturer-required site with no resolved choice shouldn't reach here in
    # interactive mode, but guard just in case
    if isinstance(results, MfrChoiceNeeded):
        print(f"\n{args.slug} needs a manufacturer. Options:")
        for i, o in enumerate(results.options, 1):
            print(f"  {i}. {o['label']}")
        print(f"\nRe-run with --mfr-pick N or --mfr <name>.")
        return 0
    _print(args.slug, results)
    if not args.no_store and db is not False and results:
        print(f"\nStored {len(results)} result(s) in the cross_reference DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
