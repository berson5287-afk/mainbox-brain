#!/usr/bin/env python3
"""
site_teacher.py  -  teach MaINbox a new vendor cross-reference site by clicking.
v0.1.0

Instead of hand-coding a scraper for every vendor, you TEACH the app a site
once: it opens the page in a real browser and walks you through clicking each
element — the search field, the submit button, one example cell in each result
column (competitor part, equivalent part, and optionally stock/price/notes),
and the next-page arrow if there is one. The app captures a robust CSS selector
for each click and saves a per-site JSON "recipe" to the registry. A generic
driver (see generic_xref.py) can then replay that recipe for any lookup — no
vision model, no custom code.

The capture works by injecting a small JS layer that, on each click, computes a
stable selector for the clicked element and records it. For result columns we
also record the element's column index within its table row so the driver can
read that column from EVERY row.

Storage: one JSON file per site under the registry dir (default:
./xref_sites/<slug>.json, override with XREF_SITES_DIR).

Requires Playwright:
    pip install playwright
    playwright install chromium

CLI:
    python site_teacher.py teach "https://www.example.com/cross-reference"
    python site_teacher.py teach <url> --name "Ilsco"
    python site_teacher.py list
    python site_teacher.py show <slug>
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import logging
import argparse

__version__ = "0.1.8"  # navigate to --search-url directly for teaching; build pattern from post-redirect URL (Legrand)

log = logging.getLogger("site_teacher")

# --- config ------------------------------------------------------------------
NAV_TIMEOUT_MS = 25_000
SITES_DIR = os.environ.get("XREF_SITES_DIR", "xref_sites")
PROFILE_DIR = os.environ.get("XREF_PROFILE", ".xref_browser_profile")

_REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# the steps we walk the user through, in order. Each: (key, prompt, kind).
# kind: 'field' = an input, 'button' = a clickable, 'cell' = a result-table
# cell (we capture its column index), 'optional_cell' = same but skippable,
# 'optional_button' = skippable clickable (e.g. pagination).
TEACH_STEPS = [
    ("mfr_select",     "If this site REQUIRES picking a MANUFACTURER from a "
                       "dropdown before searching, click that dropdown now. "
                       "(Skip if there's no manufacturer dropdown.)",
     "optional_dropdown"),
    ("search_input",   "Click the SEARCH FIELD where you type the part number",
     "field"),
    ("submit",         "Click the SUBMIT / SEARCH button (or press Skip if it "
                       "searches automatically as you type)", "optional_button"),
    ("col_competitor", "In the RESULTS, click a cell in the column that holds "
                       "the COMPETITOR part number (the one you searched)",
     "cell"),
    ("col_manufacturer","Click a cell in the MANUFACTURER / BRAND column — who "
                        "makes the competitor part (e.g. 'Eaton', 'RACO'). "
                        "Skip if the brand is mixed into the part-number cell.",
     "optional_cell"),
    ("col_equivalent", "Click a cell in the column with the EQUIVALENT part "
                       "number (the house-brand replacement)", "cell"),
    ("col_description","Click a cell in the DESCRIPTION column (or Skip)",
     "optional_cell"),
    ("col_stock",      "Click a cell in the STOCK/AVAILABILITY column (or Skip)",
     "optional_cell"),
    ("col_price",      "Click a cell in the PRICE column (or Skip)",
     "optional_cell"),
    ("col_notes",      "Click a cell in the NOTES / MATCH-QUALITY column "
                       "(or Skip)", "optional_cell"),
    ("next_page",      "Click the NEXT-PAGE arrow/button if results span "
                       "multiple pages (or Skip)", "optional_button"),
]


# --- JS capture layer --------------------------------------------------------
# Injected into the page. Exposes window.__teachArm(kind) which highlights
# elements on hover and, on the next click, records a selector (and column
# index for table cells) into window.__teachResult, then disarms.
_CAPTURE_JS = r"""
() => {
  if (window.__teachInstalled) return;
  window.__teachInstalled = true;
  window.__teachResult = null;
  window.__teachArmed = null;

  // build a reasonably stable CSS selector for an element
  function cssPath(el) {
    if (!(el instanceof Element)) return '';
    // prefer a stable id
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return '#' + el.id;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      let sel = cur.nodeName.toLowerCase();
      if (cur.id && /^[A-Za-z][\w-]*$/.test(cur.id)) {
        parts.unshift(sel + '#' + cur.id);
        break;
      }
      // add a stable-looking class (skip hashed/utility-looking ones)
      const cls = (cur.getAttribute('class') || '').split(/\s+/)
        .filter(c => c && c !== '__teach_hl' && c.length < 25 &&
                     !/[A-Z]{2,}|\d{3,}|--/.test(c));
      if (cls.length) sel += '.' + cls[0];
      // nth-of-type among siblings for uniqueness
      let i = 1, sib = cur;
      while ((sib = sib.previousElementSibling)) {
        if (sib.nodeName === cur.nodeName) i++;
      }
      sel += `:nth-of-type(${i})`;
      parts.unshift(sel);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  // for a table cell, find its column index and a selector for the table
  function cellInfo(el) {
    const td = el.closest('td,th');
    if (!td) return null;
    const tr = td.closest('tr');
    if (!tr) return null;
    const cells = Array.from(tr.children);
    const colIndex = cells.indexOf(td);
    const table = tr.closest('table');
    return {
      colIndex,
      tableSelector: table ? cssPath(table) : '',
      cellText: (td.innerText || '').trim().slice(0, 60)
    };
  }

  function onMove(e) {
    if (!window.__teachArmed) return;
    document.querySelectorAll('.__teach_hl').forEach(n =>
      n.classList.remove('__teach_hl'));
    if (e.target instanceof Element) e.target.classList.add('__teach_hl');
  }

  function onClick(e) {
    if (!window.__teachArmed) return;
    e.preventDefault(); e.stopPropagation();
    const kind = window.__teachArmed;

    // remove our highlight class from EVERYTHING before computing selectors,
    // so the transient '__teach_hl' class never leaks into a saved selector
    document.querySelectorAll('.__teach_hl').forEach(n =>
      n.classList.remove('__teach_hl'));

    let el = e.target;
    // for buttons/links, the user may click an icon (svg/use/span) inside the
    // real control — climb to the nearest clickable ancestor so we capture the
    // element that actually responds to clicks. Recognize classic ASP/HTML
    // submit controls: <button>, <a>, <input type=submit|button|image>, and
    // anything with role=button.
    function isClickable(n) {
      if (!n || n.nodeType !== 1) return false;
      const tag = n.tagName;
      if (tag === 'A' || tag === 'BUTTON') return true;
      if (n.getAttribute('role') === 'button') return true;
      if (tag === 'INPUT') {
        const ty = (n.getAttribute('type') || 'text').toLowerCase();
        return ty === 'submit' || ty === 'button' || ty === 'image' ||
               ty === 'reset';
      }
      return false;
    }
    if (kind === 'button') {
      let c = el;
      while (c && c !== document.body && !isClickable(c)) {
        c = c.parentElement;
      }
      if (c && c !== document.body) el = c;
    }

    const res = {
      kind,
      selector: cssPath(el),
      tag: el.nodeName.toLowerCase(),
      inputType: (el.getAttribute && (el.getAttribute('type') || '')) || '',
      text: (el.innerText || el.value ||
             el.getAttribute('aria-label') || '').trim().slice(0, 60),
      placeholder: el.getAttribute('placeholder') || ''
    };
    if (kind === 'cell') {
      const ci = cellInfo(el);
      if (ci) { res.colIndex = ci.colIndex; res.tableSelector = ci.tableSelector;
                res.cellText = ci.cellText; }
    }
    if (kind === 'dropdown') {
      // capture the available manufacturer options. Two cases:
      // (a) native <select> -> read its <option> values/text
      // (b) custom dropdown -> climb to the nearest <select>, else record the
      //     control so the driver can open it and read options live.
      let sel = el.closest('select') ||
                (el.tagName === 'SELECT' ? el : null);
      if (!sel) {
        // maybe the user clicked a label/wrapper; look for a select inside
        sel = el.querySelector ? el.querySelector('select') : null;
      }
      if (sel) {
        res.selector = cssPath(sel);
        res.tag = 'select';
        res.dropdownType = 'native';
        res.options = Array.from(sel.options || [])
          .map(o => ({ value: o.value, label: (o.textContent || '').trim() }))
          .filter(o => o.label && !/^select/i.test(o.label));
      } else {
        // custom widget: record selector; options read live at search time
        res.dropdownType = 'custom';
        res.options = [];
      }
    }
    // record FIRST, before anything can navigate the page away
    window.__teachResult = res;
    window.__teachArmed = null;
    // also stash in sessionStorage as a backstop: if the click triggers an
    // ASP postback/navigation that wipes window state, the teacher can recover
    // it after the reload.
    try { sessionStorage.setItem('__teachResult', JSON.stringify(res)); }
    catch (err) {}
    document.querySelectorAll('.__teach_hl').forEach(n =>
      n.classList.remove('__teach_hl'));
    return false;
  }

  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('click', onClick, true);

  // highlight style
  const st = document.createElement('style');
  st.textContent = '.__teach_hl{outline:3px solid #e60023 !important;' +
    'outline-offset:1px !important;background:rgba(230,0,35,.08)!important;}';
  document.head.appendChild(st);

  window.__teachArm = (kind) => {
    window.__teachResult = null;
    window.__teachArmed = kind;
  };
}
"""


def _make_page(playwright):
    """Open a visible, persistent-profile browser page (so cookies/consent are
    shared with the lookups and remembered)."""
    use_profile = bool(PROFILE_DIR) and PROFILE_DIR.lower() != "none"
    args = ["--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"]
    if use_profile:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        ctx = playwright.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, user_agent=_REAL_UA,
            viewport={"width": 1500, "height": 950}, locale="en-US", args=args)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return ctx, None, page
    browser = playwright.chromium.launch(headless=False, args=args)
    ctx = browser.new_context(user_agent=_REAL_UA,
                              viewport={"width": 1500, "height": 950})
    return ctx, browser, ctx.new_page()


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or "site"


def _capture_kind(step_kind: str) -> str:
    """Map a teach-step kind (possibly 'optional_*') to the capture kind the
    JS layer understands: 'field', 'button', 'cell', or 'dropdown'."""
    k = step_kind.replace("optional_", "")
    if k in ("field", "cell", "dropdown"):
        return k
    return "button"


def _re_replace_ci(haystack: str, needle: str, repl: str) -> str:
    """Replace every case-insensitive occurrence of `needle` with `repl`.
    Also handles URL-encoded forms of the needle (e.g. 'TA-0' vs 'TA%2D0')."""
    import urllib.parse as _up
    out = re.sub(re.escape(needle), repl, haystack, flags=re.IGNORECASE)
    enc = _up.quote(needle)
    if enc != needle:
        out = re.sub(re.escape(enc), repl, out, flags=re.IGNORECASE)
    return out


def _arm_and_wait(page, kind: str, capture_js: str, timeout_s: int = 120):
    """Arm the capture for one click and poll until the user clicks (or times
    out). Returns the captured dict or None.

    Robust to page navigation: classic ASP/postback search buttons reload the
    page on click, which wipes window state. We (a) clear any stale stash, (b)
    poll window.__teachResult, and (c) if the page navigated, re-install the
    capture layer and check sessionStorage where onClick also stashed the
    result before the nav."""
    # clear any stale stash from a previous step
    try:
        page.evaluate("() => { try { sessionStorage.removeItem('__teachResult'); "
                      "} catch(e){} window.__teachResult = null; }")
    except Exception:  # noqa: BLE001
        pass
    page.evaluate("(k) => window.__teachArm(k)", kind)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        res = None
        # primary: live window result
        try:
            res = page.evaluate("() => window.__teachResult")
        except Exception:  # noqa: BLE001  (page may be mid-navigation)
            res = None
        if res:
            return res
        # backstop: a postback may have navigated and wiped window state.
        # re-install the capture layer (idempotent) and check sessionStorage.
        try:
            page.evaluate(capture_js)          # re-install after any reload
            stashed = page.evaluate(
                "() => { try { const v = sessionStorage.getItem("
                "'__teachResult'); return v ? JSON.parse(v) : null; } "
                "catch(e){ return null; } }")
            if stashed and stashed.get("kind") == kind:
                # consume it so it isn't re-read next step
                page.evaluate("() => { try { sessionStorage.removeItem("
                              "'__teachResult'); } catch(e){} }")
                return stashed
            # if we navigated, the arm was lost — re-arm
            armed = page.evaluate("() => window.__teachArmed")
            if not armed:
                page.evaluate("(k) => window.__teachArm(k)", kind)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    return None


def teach(url: str, name: str | None = None,
          search_url: str | None = None) -> dict | None:
    """Walk the user through teaching a site; save and return the recipe.

    search_url: optionally pass the post-search RESULTS url (with the part
    number you searched still in it) to force Type-B (direct-URL) mode. The
    teacher will turn it into a {part} pattern. This is the reliable way to
    teach server-side search sites (ASP etc.) when auto-detection is uncertain.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — pip install playwright && "
              "playwright install chromium")
        return None

    recipe: dict = {"url": url, "steps": {}, "version": __version__}

    with sync_playwright() as p:
        ctx, browser, page = _make_page(p)
        try:
            print(f"\nOpening {url}")
            page.goto(url, wait_until="domcontentloaded",
                      timeout=NAV_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001
                pass

            # auto-detect a site name from the title, let user override
            try:
                page_title = page.title()
            except Exception:  # noqa: BLE001
                page_title = ""
            auto = name or page_title or url
            print(f"\nDetected site name: {auto!r}")
            typed = input("Press Enter to keep it, or type a different name: ")
            site_name = typed.strip() or auto
            recipe["name"] = site_name
            slug = _slugify(site_name)
            recipe["slug"] = slug

            print("\n--- STEP 1: get the RESULTS on screen ---")
            if search_url:
                # they gave the results URL — go straight there so we teach the
                # columns on the actual results page (and confirm URL mode)
                print(f"Navigating directly to your results URL...")
                try:
                    page.goto(search_url, wait_until="domcontentloaded",
                              timeout=NAV_TIMEOUT_MS)
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:  # noqa: BLE001
                    pass
                # let any client-side redirect settle, then show where we landed
                page.wait_for_timeout(1_500)
                try:
                    print(f"  Landed on: {page.url}")
                except Exception:  # noqa: BLE001
                    pass
                print("If you can see the search RESULTS now, good. (If it shows "
                      "everything or a wildcard, the URL may need adjusting.)")
            else:
                print("In the browser: type a part number into the search box "
                      "and submit, so the RESULTS are visible. Accept any "
                      "cookie/consent prompts too.")
                print("Tip: pick a part you KNOW returns results.")
            searched_term = input("\nType the EXACT part number you just "
                                  "searched, then press Enter: ").strip()
            # some sites (e.g. Legrand) put a manufacturer in the results URL
            # too. Ask for it so we can build a {mfr} placeholder. Blank = none.
            searched_mfr = input("If you also picked/typed a MANUFACTURER, type "
                                 "it EXACTLY as you selected it (or blank if "
                                 "none): ").strip()

            # --- Type B: server-side GET search (URL carries the term) --------
            # Many tools reload the page with the search term in the URL. We can
            # then navigate directly to that URL for any part — far more robust
            # than driving the form. Some sites also carry the manufacturer in
            # the URL (Legrand: ?competitorBrand=HUBBELL&text=P26W), which we
            # capture as a {mfr} placeholder. We look for the term(s) in (a) an
            # explicit --search-url, or (b) the live page URL.
            search_mode = "field"
            try:
                live_url = page.url
            except Exception:  # noqa: BLE001
                live_url = ""

            # pick the URL that actually contains the searched term. Prefer the
            # POST-REDIRECT live URL when it has the term (servers often clean
            # up the query, e.g. dropping an empty competitorBrand=), else the
            # provided --search-url.
            candidate_url = ""
            if searched_term and live_url and \
                    searched_term.lower() in live_url.lower():
                candidate_url = live_url
            elif search_url and searched_term and \
                    searched_term.lower() in search_url.lower():
                candidate_url = search_url

            if not candidate_url and search_url:
                # they gave a URL but the term wasn't in it — warn and show
                print(f"\n[!] The --search-url you provided does not contain "
                      f"{searched_term!r}, so I can't build a pattern from it. "
                      "Make sure the term in the URL matches what you typed.")

            if candidate_url:
                # substitute the part first, then the manufacturer (if given and
                # actually present in the URL)
                pattern = _re_replace_ci(candidate_url, searched_term, "{part}")
                has_mfr_in_url = bool(
                    searched_mfr and searched_mfr.lower() in
                    candidate_url.lower())
                if has_mfr_in_url:
                    pattern = _re_replace_ci(pattern, searched_mfr, "{mfr}")
                n_part = pattern.count("{part}")
                n_mfr = pattern.count("{mfr}")
                print(f"\nDetected a SERVER-SIDE search — part appears {n_part} "
                      f"time(s)" + (f", manufacturer {n_mfr} time(s)"
                                    if n_mfr else "") + " in the results URL.")
                print(f"  URL pattern: {pattern}")
                use_url = input("Use direct-URL search? (recommended, much more "
                                "reliable) [Y/n]: ").strip().lower()
                if use_url != "n":
                    search_mode = "url"
                    recipe["search_url_pattern"] = pattern
                    # default to RAW substitution (no URL-encoding): servers
                    # match the literal term, and the browser encodes on nav.
                    recipe["search_url_encode"] = bool(
                        re.search(r"[\s&?#=]", searched_term))
                    if n_mfr:
                        # the manufacturer is part of the URL — mark required so
                        # the driver resolves --mfr to the right value
                        recipe["mfr_required"] = True
                        recipe["mfr_in_url"] = True
                    print("   Using direct-URL search. Skipping the search-"
                          "field and submit steps."
                          + (" Manufacturer will be substituted into the URL."
                             if n_mfr else ""))
            elif not search_url:
                print("\n(No server-side URL pattern detected — your term "
                      "wasn't in the page URL. Using click-to-search mode. If "
                      "this site actually searches via the URL, re-run with "
                      "--search-url \"<paste the results URL here>\".)")

            recipe["search_mode"] = search_mode

            input("\nWhen results are on screen, press Enter to begin "
                  "teaching the result COLUMNS...")

            # install the capture layer (re-install after the manual search,
            # in case the page navigated)
            page.evaluate(_CAPTURE_JS)

            # in URL mode we skip the search_input + submit steps
            steps_to_run = TEACH_STEPS
            if search_mode == "url":
                steps_to_run = [s for s in TEACH_STEPS
                                if s[0] not in ("search_input", "submit")]

            for key, prompt, kind in steps_to_run:
                optional = kind.startswith("optional")
                print(f"\n[{key}] {prompt}")
                if optional:
                    print("   (type 's' then Enter to SKIP, or just click)")
                # let the user read, then arm
                # we arm immediately; if optional, also watch for a 's' skip
                page.evaluate(_CAPTURE_JS)  # ensure installed after any nav
                captured = None
                if optional:
                    # poll both for a click and for a typed skip is awkward in
                    # a console; simplest: ask first whether to skip
                    ans = input("   Skip this one? [y/N or Enter to click]: ")
                    if ans.strip().lower() == "y":
                        print("   skipped.")
                        continue
                print("   ... now click the element in the browser.")
                cap_kind = _capture_kind(kind)
                captured = _arm_and_wait(page, cap_kind, _CAPTURE_JS)
                if not captured:
                    if optional:
                        print("   no click captured — skipping.")
                        continue
                    print("   no click captured — this step is required. "
                          "Trying once more.")
                    captured = _arm_and_wait(page, cap_kind, _CAPTURE_JS)
                    if not captured:
                        print("   still nothing; aborting teach.")
                        return None
                recipe["steps"][key] = captured
                desc = captured.get("cellText") or captured.get("text") or \
                    captured.get("placeholder") or captured.get("selector", "")
                col = captured.get("colIndex")
                colmsg = f" (column #{col})" if col is not None else ""
                # for the manufacturer dropdown, report how many options we got
                if captured.get("kind") == "dropdown":
                    opts = captured.get("options", [])
                    dtype = captured.get("dropdownType", "?")
                    if opts:
                        recipe["mfr_required"] = True
                        print(f"   captured manufacturer dropdown ({dtype}, "
                              f"{len(opts)} options). First few: "
                              f"{', '.join(o['label'] for o in opts[:5])}...")
                    else:
                        recipe["mfr_required"] = True
                        print(f"   captured manufacturer dropdown ({dtype}); "
                              "options will be read at search time.")
                else:
                    print(f"   captured: {desc!r}{colmsg}")

            # save
            os.makedirs(SITES_DIR, exist_ok=True)
            path = os.path.join(SITES_DIR, f"{slug}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(recipe, f, indent=2)
            print(f"\nSaved site recipe -> {path}")
            print("You can now look this site up with the generic driver.")
            return recipe
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


def list_sites() -> list[str]:
    if not os.path.isdir(SITES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SITES_DIR)
                  if f.endswith(".json"))


def load_site(slug: str) -> dict | None:
    path = os.path.join(SITES_DIR, f"{slug}.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="site_teacher.py",
        description="Teach MaINbox a vendor cross-reference site by clicking.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("teach", help="learn a new site")
    t.add_argument("url", help="the cross-reference tool URL")
    t.add_argument("--name", default=None, help="override the site name")
    t.add_argument("--search-url", default=None,
                   help="for server-side search sites (ASP etc.): paste the "
                        "RESULTS url (with your searched part still in it) to "
                        "force direct-URL mode. The most reliable way to teach "
                        "sites where clicking the search button is flaky.")

    sub.add_parser("list", help="list taught sites")

    sh = sub.add_parser("show", help="print a taught site's recipe")
    sh.add_argument("slug")

    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(levelname)s %(message)s")

    if args.cmd == "teach":
        r = teach(args.url, name=args.name, search_url=args.search_url)
        return 0 if r else 1
    if args.cmd == "list":
        sites = list_sites()
        if not sites:
            print(f"No taught sites yet (dir: {SITES_DIR}).")
        else:
            print("Taught sites:")
            for s in sites:
                rec = load_site(s) or {}
                print(f"  {s:<20} {rec.get('name', '')}  <- {rec.get('url','')}")
        return 0
    if args.cmd == "show":
        rec = load_site(args.slug)
        if not rec:
            print(f"No such site: {args.slug}")
            return 1
        print(json.dumps(rec, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
