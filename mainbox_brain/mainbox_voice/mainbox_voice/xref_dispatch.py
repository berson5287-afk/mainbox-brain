#!/usr/bin/env python3
"""
xref_dispatch.py  -  the "what do I know" layer for MaINbox cross-references.
v0.1.0

One entry point that answers "what's equal to <part>?" by consulting, in order:

    1. the curated cross_reference store (instant, offline, user-correctable)
    2. every TAUGHT vendor site (site_teacher.py recipes, run by generic_xref)

It aggregates everything, stores new web findings back into the curated store
(so the next lookup is instant), and — when NO source returns anything — tells
you honestly and points you at teaching a new site. This is what lets you ask
about an Ilsco TA-0, a Burndy KA25U, a Morris 90716, etc. and get whatever the
known sources can find, with a clear path to add a source when one is missing.

It is deliberately a thin router: the curated store and the generic driver do
the real work; this decides who to ask and merges the answers.

CLI:
    python xref_dispatch.py "QO130"
    python xref_dispatch.py "257" --mfr raco
    python xref_dispatch.py "Ilsco TA-0" "Burndy KA25U" "Morris 90716"
    python xref_dispatch.py --sites          # what can I actively query?
    python xref_dispatch.py "BR130" --no-web # curated store only
"""

from __future__ import annotations

import os
import re
import sys
import logging
import argparse

# shared brain: normalize + natural-language parsing (voice-ready foundation)
try:
    from . import xref_common as xc
except ImportError:
    try:
        import xref_common as xc
    except ImportError:
        xc = None

__version__ = "0.2.2"  # skip mfr-required sites (e.g. Arlington) when no manufacturer given, instead of stalling a browser

log = logging.getLogger("xref_dispatch")

DB_PATH = os.environ.get("XREF_DB", "cross_references.db")
SITES_DIR = os.environ.get("XREF_SITES_DIR", "xref_sites")

# v0.1.3: remember the last numbered result list so the user can confirm/reject
# findings by their displayed number (e.g. 'reject 2,3'). Stored as JSON next
# to the DB.
_LAST_RESULTS_PATH = os.environ.get(
    "XREF_LAST_RESULTS",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".",
                 ".xref_last_results.json"))


# --- soft imports: work both as a package and as loose scripts ---------------
def _imp(modname):
    import importlib
    try:
        return importlib.import_module("." + modname, package=__package__) \
            if __package__ else importlib.import_module(modname)
    except Exception:  # noqa: BLE001
        return importlib.import_module(modname)


def _load_modules():
    xr = generic = None
    try:
        xr = _imp("cross_reference")
    except Exception as e:  # noqa: BLE001
        log.debug("cross_reference unavailable: %s", e)
    try:
        generic = _imp("generic_xref")
    except Exception as e:  # noqa: BLE001
        log.debug("generic_xref unavailable: %s", e)
    return xr, generic


# --- aggregated result -------------------------------------------------------
class Finding:
    """One equivalent for a queried part, from any source."""
    __slots__ = ("equiv_part", "equiv_mfr", "src_mfr", "equiv_type",
                 "confidence", "status", "caveat", "notes", "source")

    def __init__(self, equiv_part, equiv_mfr="", src_mfr="", equiv_type="",
                 confidence=0.0, status="", caveat="", notes="", source=""):
        self.equiv_part = equiv_part
        self.equiv_mfr = equiv_mfr
        self.src_mfr = src_mfr
        self.equiv_type = equiv_type
        self.confidence = confidence
        self.status = status
        self.caveat = caveat
        self.notes = notes
        self.source = source

    def key(self):
        _n = xc.normalize if xc else (
            lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower()))
        return (_n(self.equiv_part), _n(self.equiv_mfr or ""))


def known_sites() -> list[str]:
    """Taught site slugs we can actively query."""
    if not os.path.isdir(SITES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SITES_DIR)
                  if f.endswith(".json"))


# --- the dispatcher ----------------------------------------------------------
def cross_reference(part: str, *, mfr: str | None = None,
                    use_web: bool = True, only_sites: list[str] | None = None,
                    live_policy: str = "auto",
                    db: str | None = None) -> dict:
    """Resolve equivalents for `part` across all known sources.

    live_policy controls when the (slower) live vendor sites are queried:
      - "auto"   : query live sites ONLY if the curated store found nothing.
                   Fast: confirmed/curated answers return instantly, live tools
                   are the fallback for genuinely new parts. (default)
      - "always" : query live sites even when the curated store had hits — the
                   user override for "I want fresh vendor data regardless."
      - "never"  : curated store only; never open a browser.
    (use_web=False is treated as live_policy="never" for backward compat.)

    Returns:
        {
          "part": str, "mfr": str|None,
          "findings": [Finding, ...],          # merged, de-duped, ranked
          "curated_hit": bool,                 # store had something
          "sites_queried": [slug, ...],
          "sites_with_hits": [slug, ...],
          "covered": bool,                     # any source returned anything
          "suggestion": str|None,              # next-step hint when thin/empty
        }
    """
    xr, generic = _load_modules()
    findings: list[Finding] = []
    seen: set = set()

    def _add(f: Finding):
        k = f.key()
        if k in seen or not f.equiv_part:
            return
        seen.add(k)
        findings.append(f)

    # ---- tier 1: curated store --------------------------------------------
    curated_hit = False
    if xr is not None:
        try:
            res = xr.resolve(part, mfr, db=db or DB_PATH)
            equivs = res.get("equivalents", [])
            # If an mfr hint was given but filtered everything out, retry
            # WITHOUT the mfr filter. The brand is a ranking hint, not a hard
            # gate — the stored cross-reference may record the competitor brand
            # differently (e.g. 'WESTINGHOUSE MINIATURE' vs your 'EATON'), and
            # a valid part match shouldn't be hidden by a brand mismatch.
            if not equivs and mfr:
                log.debug("curated: no hit for part+mfr %r/%r; retrying "
                          "part-only", part, mfr)
                res = xr.resolve(part, None, db=db or DB_PATH)
                equivs = res.get("equivalents", [])
            for e in equivs:
                curated_hit = True
                _add(Finding(
                    equiv_part=e.part, equiv_mfr=e.mfr, src_mfr=mfr or "",
                    equiv_type=e.equiv_type, confidence=e.confidence,
                    status=e.status, caveat=e.caveat,
                    source=f"curated:{e.source}" if e.source else "curated"))
        except Exception as e:  # noqa: BLE001
            log.debug("curated lookup failed: %s", e)

    # ---- tier 2: taught vendor sites --------------------------------------
    # Decide whether to go live. "auto" only goes live when curated found
    # nothing (fast path); "always" overrides that; "never"/use_web=False skip.
    if not use_web:
        live_policy = "never"
    go_live = (generic is not None and live_policy != "never" and
               (live_policy == "always" or not curated_hit))
    if generic is not None and live_policy == "auto" and curated_hit:
        log.debug("curated store had hits — skipping live sites "
                  "(use live_policy='always' / --research to override)")

    sites_queried: list[str] = []
    sites_with_hits: list[str] = []
    sites_need_mfr: list[str] = []          # mfr-required, skipped (no mfr)
    if go_live:
        sites = only_sites if only_sites is not None else known_sites()
        _no_mfr = (mfr is None or not str(mfr).strip())
        for slug in sites:
            # A site whose recipe requires picking a manufacturer (e.g.
            # Arlington's two-parameter ?m={mfr}&id={part} form) cannot run
            # without one — it would otherwise launch a browser and stall
            # waiting on a dropdown. Skip it cleanly when no mfr was given and
            # record it, so the caller can suggest re-asking with the maker.
            if _no_mfr:
                try:
                    _rec = generic.load_recipe(slug)
                except Exception:  # noqa: BLE001
                    _rec = None
                if _rec and _rec.get("mfr_required"):
                    sites_need_mfr.append(_rec.get("name") or slug.title())
                    continue
            sites_queried.append(slug)
            try:
                rows = generic.lookup(slug, part, mfr_filter=mfr, db=db)
            except Exception as e:  # noqa: BLE001
                log.debug("site %s lookup failed: %s", slug, e)
                rows = []
            # if lookup returned the "manufacturer needed" sentinel instead of
            # a list of rows, treat it as a skip (don't try to iterate it)
            if not isinstance(rows, list):
                sites_need_mfr.append(slug.title())
                rows = []
            if rows:
                sites_with_hits.append(slug)
            # the equivalent part belongs to this vendor's house brand; use
            # the recipe's display name (e.g. 'Morris') rather than the slug
            site_brand = slug.title()
            try:
                import generic_xref as _g
                rec = _g.load_recipe(slug)
                if rec and rec.get("name"):
                    site_brand = rec["name"]
            except Exception:  # noqa: BLE001
                pass
            for r in rows:
                _add(Finding(
                    equiv_part=r.equiv_part,
                    equiv_mfr=r.equiv_mfr or site_brand,
                    src_mfr=r.src_mfr,
                    equiv_type="spec_equivalent",
                    confidence=0.85 if r.raw.get("mfr_filtered") else 0.70,
                    status="from_vendor_tool",
                    notes=r.notes,
                    source=f"taught:{slug}"))

    # ---- rank: confirmed/curated first, then confidence -------------------
    def rank(f: Finding):
        curated = f.source.startswith("curated")
        confirmed = f.status == "confirmed"
        return (0 if confirmed else (1 if curated else 2), -f.confidence)
    findings.sort(key=rank)

    covered = bool(findings)

    # ---- next-step suggestion ---------------------------------------------
    suggestion = None
    if not covered:
        if not known_sites():
            suggestion = ("No cross-reference sources are set up yet. Teach "
                          "one with:  python site_teacher.py teach <url>")
        else:
            suggestion = ("Nothing found in the curated store or the taught "
                          f"sites ({', '.join(known_sites())}). If a vendor "
                          "has a cross-reference tool for this brand, teach it: "
                          "python site_teacher.py teach <url>")
    elif use_web and not sites_with_hits and known_sites():
        # curated had it but no taught site did — fine, just note coverage
        suggestion = None

    # if a manufacturer-required site was skipped, tell the user how to use it
    if sites_need_mfr:
        need = ", ".join(dict.fromkeys(sites_need_mfr))
        tip = (f"{need} can only search with a manufacturer — ask again "
               f"including the maker (e.g. 'equal for a Topaz {part}').")
        suggestion = (suggestion + " " + tip) if suggestion else tip

    return {
        "part": part,
        "mfr": mfr,
        "findings": findings,
        "curated_hit": curated_hit,
        "sites_queried": sites_queried,
        "sites_with_hits": sites_with_hits,
        "sites_need_mfr": sites_need_mfr,
        "covered": covered,
        "suggestion": suggestion,
    }


def cross_reference_many(parts: list[str], *, mfr: str | None = None,
                         use_web: bool = True, live_policy: str = "auto",
                         db: str | None = None) -> dict:
    """Resolve several parts at once (e.g. 'Ilsco TA-0', 'Burndy KA25U', ...).
    Returns {part: result_dict}."""
    out = {}
    for pt in parts:
        out[pt] = cross_reference(pt, mfr=mfr, use_web=use_web,
                                  live_policy=live_policy, db=db)
    return out


# --- correction-by-number support --------------------------------------------
def _save_last_results(parsed_results: list[tuple]) -> None:
    """Persist the flat numbered list shown to the user so a follow-up
    confirm/reject can map numbers back to (part, equiv_part).

    parsed_results: list of (number, part, equiv_part, equiv_mfr, label).
    """
    import json
    try:
        data = [{"n": n, "part": p, "equiv_part": ep, "equiv_mfr": em,
                 "label": lbl}
                for (n, p, ep, em, lbl) in parsed_results]
        with open(_LAST_RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:  # noqa: BLE001
        log.debug("could not save last results: %s", e)


def _load_last_results() -> list[dict]:
    import json
    try:
        with open(_LAST_RESULTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def _save_last_results_raw(rows: list[dict]) -> None:
    """Persist the already-dict rows (preserving the per-row 'decided' marker),
    used after a correction so the next command in this round sees what's been
    handled."""
    import json
    try:
        with open(_LAST_RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
    except Exception as e:  # noqa: BLE001
        log.debug("could not save last results: %s", e)


def _parse_number_spec(spec: str, max_n: int):
    """Parse a selection like '2,3' or '1-3' or 'all' into a list of ints, OR a
    sentinel string for the bulk forms (the caller expands these against which
    findings are still undecided):
      'all' / 'rest' / 'remaining'   -> 'UNDECIDED'  (only ones not yet decided)
      'everything' / 'force'         -> 'ALL'        (literally every finding)
    Returns a list[int] for explicit numbers, or 'UNDECIDED' / 'ALL'.
    """
    spec = spec.strip().lower()
    # literally-everything forms
    if spec in ("everything", "force", "all force", "*"):
        return "ALL"
    # undecided-remainder forms (the safe default for "all")
    if spec in ("all", "rest", "remaining", "the rest", "others",
                "the others"):
        return "UNDECIDED"
    nums: set[int] = set()
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            nums.update(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            nums.add(int(part))
    return sorted(n for n in nums if 1 <= n <= max_n)


def apply_correction(action: str, spec: str, db: str | None = None) -> int:
    """Apply confirm/reject to findings selected by number (or 'all'/'rest'/
    'everything') from the last lookup. action is 'confirm' or 'reject'.

    'all' and 'rest' apply ONLY to findings not already confirmed/rejected this
    round, so a two-step triage works as expected:
        confirm 1,2   then   reject all   -> rejects 3,4 only (keeps 1,2)
    Use 'everything' (or 'force') to apply to literally every finding.
    Returns count applied."""
    rows = _load_last_results()
    if not rows:
        print("No remembered results to correct. Run a lookup first, e.g.:\n"
              "  python xref_dispatch.py \"BR330\"")
        return 0
    max_n = max(r["n"] for r in rows)
    sel = _parse_number_spec(spec, max_n)

    # expand the bulk sentinels against what's already been decided this round
    decided = {r["n"]: r.get("decided") for r in rows}
    if sel == "ALL":
        picks = list(range(1, max_n + 1))
    elif sel == "UNDECIDED":
        picks = [r["n"] for r in rows if not r.get("decided")]
        if not picks:
            print("Every result has already been confirmed or rejected this "
                  "round. (Use 'everything' to re-apply to all of them.)")
            return 0
    else:
        picks = sel

    if not picks:
        print(f"Couldn't read selection {spec!r}. Use numbers (2,3), a range "
              "(1-3), 'rest' (the undecided ones), or 'everything'.")
        return 0

    try:
        xr = _imp("cross_reference")
    except Exception as e:  # noqa: BLE001
        print(f"cross_reference store unavailable: {e}")
        return 0

    by_n = {r["n"]: r for r in rows}
    applied = 0
    skipped_decided = 0
    for n in picks:
        r = by_n.get(n)
        if not r:
            continue
        # when the user names explicit numbers we honor them even if previously
        # decided (they're being deliberate); only the bulk 'rest' form skips
        # already-decided ones (handled above by building picks from undecided).
        part, equiv = r["part"], r["equiv_part"]
        try:
            if action == "confirm":
                res = xr.confirm(part, equiv, db=db or DB_PATH)
            else:
                res = xr.reject(part, equiv, db=db or DB_PATH)
            applied += 1
            r["decided"] = action + "ed"   # record so future 'rest' skips it
            mark = "✓" if action == "confirm" else "✗"
            print(f"  {mark} {action}ed #{n}: {part} -> "
                  f"{r.get('equiv_mfr','')} {equiv}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! #{n} ({part} -> {equiv}) failed: {e}")

    # persist the updated decisions so the NEXT correction command in this round
    # knows what's already been handled
    _save_last_results_raw(rows)

    print(f"\n{action}ed {applied} finding(s). These are now permanent.")
    # if other findings remain undecided, gently say so (helps triage flow)
    remaining = [r["n"] for r in rows if not r.get("decided")]
    if remaining:
        print(f"  ({len(remaining)} still undecided: "
              f"{', '.join(map(str, remaining))}. "
              f"e.g. 'reject rest' or 'confirm {remaining[0]}'.)")
    return applied


# --- CLI ---------------------------------------------------------------------
def _print_sites() -> None:
    """List taught sites we can actively query."""
    sites = known_sites()
    if not sites:
        print(f"No taught sites yet (dir: {SITES_DIR}).")
        print("Teach one:  python site_teacher.py teach <url>")
    else:
        print("Cross-reference sources I can actively query:")
        for s in sites:
            print(f"  - {s}")
        print("\nPlus the curated cross_reference store (always checked).")


def _print_one(res: dict, start_n: int = 1) -> list[tuple]:
    """Print one part's findings, numbering them starting at start_n.
    Returns collected rows as (n, part, equiv_part, equiv_mfr, label)."""
    part = res["part"]
    findings = res["findings"]
    collected: list[tuple] = []
    if not findings:
        print(f"\n{part}: no equivalents found.")
        if res.get("suggestion"):
            print(f"  -> {res['suggestion']}")
        return collected

    print(f"\n{part}: {len(findings)} equivalent(s)")
    src_note = []
    if res["curated_hit"]:
        src_note.append("curated store")
    if res["sites_with_hits"]:
        src_note.append("taught: " + ", ".join(res["sites_with_hits"]))
    if src_note:
        print(f"  (from {'; '.join(src_note)})")
    # if we answered from curated and never went live, let the user know they
    # CAN force fresh vendor data with --research
    if res["curated_hit"] and not res["sites_queried"] and known_sites():
        print("  (curated answer — add --research to also query live vendor "
              "tools)")

    n = start_n
    for f in findings:
        mfr = f"{f.equiv_mfr} " if f.equiv_mfr else ""
        bits = []
        if f.equiv_type:
            bits.append(f.equiv_type)
        if f.confidence:
            bits.append(f"conf {f.confidence:.2f}")
        if f.status and f.status not in ("from_vendor_tool",):
            bits.append(f.status)
        meta = f"  [{', '.join(bits)}]" if bits else ""
        srcmfr = f"{f.src_mfr[:28]} " if f.src_mfr and \
            f.src_mfr != "competitor" else ""
        print(f"    {n}. {srcmfr}-> {mfr}{f.equiv_part}{meta}")
        if f.caveat:
            print(f"        ! {f.caveat}")
        if f.notes:
            print(f"        {f.notes[:90]}")
        collected.append((n, part, f.equiv_part, f.equiv_mfr, part))
        n += 1

    # correction hint: confirm/reject BY NUMBER (no retyping part numbers)
    if any(f.source.startswith("taught") or f.source.startswith("curated")
           for f in findings):
        lo, hi = start_n, n - 1
        print(f"\n  Correct these by NUMBER (your fixes become permanent):")
        print(f"    confirm:  python xref_dispatch.py confirm {hi}   "
              f"(also: confirm {lo}-{hi}  |  confirm all)")
        print(f"    reject:   python xref_dispatch.py reject {lo},{hi}  "
              f"(also: reject all)")
    return collected


def _extract_bulk_word(phrase: str) -> str:
    """From a correction phrase, return the bulk selector word that drives the
    undecided-vs-everything distinction. 'reject everything' -> 'everything',
    'confirm the rest' -> 'rest', default 'all' (= undecided only)."""
    low = (phrase or "").lower()
    if re.search(r"\b(everything|every one|all of them force|force)\b", low):
        return "everything"
    if re.search(r"\b(rest|remaining|the others|others)\b", low):
        return "rest"
    return "all"   # safe default: undecided only


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="xref_dispatch.py",
        description="Cross-reference a part across the curated store + taught "
                    "vendor sites.")
    ap.add_argument("parts", nargs="*", help="one or more part numbers, e.g. "
                    "\"QO130\" or \"Ilsco TA-0\" \"Burndy KA25U\"")
    ap.add_argument("--mfr", default=None, help="manufacturer filter/hint")
    ap.add_argument("--no-web", action="store_true",
                    help="curated store only; don't query taught sites")
    ap.add_argument("--research", action="store_true",
                    help="OVERRIDE: query live vendor sites even if the curated "
                         "store already has an answer (fetches fresh data)")
    ap.add_argument("--sites", action="store_true",
                    help="list taught sites we can query, and exit")
    ap.add_argument("--db", default=None)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--show-browser", action="store_true",
                    help="watch the taught-site browser (passes through)")
    args = ap.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s")

    if args.sites:
        _print_sites()
        return 0

    if not args.parts:
        ap.error("give at least one part number (or use --sites)")

    # Correction (confirm/reject) BY NUMBER from the last lookup. We route the
    # whole phrase through the shared parser so all of these work:
    #   confirm 2,3   |   reject all   |   reject the second one   |
    #   confirm two and three
    joined = " ".join(args.parts).strip()
    if xc is not None:
        it = xc.parse_query(joined)
        if it.action in ("confirm", "reject"):
            if it.selection:
                apply_correction(it.action,
                                 ",".join(map(str, it.selection)), db=args.db)
            elif it.select_all:
                # pass the ORIGINAL words so 'rest'/'everything' are honored
                # (parse_query sets select_all for all of all/rest/everything;
                # apply_correction's parser then picks undecided vs literally-all
                # based on the exact word the user said).
                bulk = _extract_bulk_word(joined)
                apply_correction(it.action, bulk, db=args.db)
            else:
                print(f"Which results should I {it.action}? e.g. "
                      f"'{it.action} 2,3', '{it.action} rest', or "
                      f"'{it.action} everything'.")
            return 0
        if it.action == "list_sites":
            args.sites = True   # fall through to the --sites handler below
    else:
        # fallback: exact-syntax confirm/reject
        if args.parts[0].lower() in ("confirm", "reject"):
            action = args.parts[0].lower()
            spec = " ".join(args.parts[1:]).strip()
            if not spec:
                print(f"Usage: {action} <numbers|all|rest|everything>")
                return 2
            apply_correction(action, spec, db=args.db)
            return 0

    if args.sites:
        _print_sites()
        return 0

    # pass show-browser through to the generic driver
    if args.show_browser:
        try:
            g = _imp("generic_xref")
            g.HEADLESS = False
        except Exception:  # noqa: BLE001
            pass

    db = args.db
    use_web = not args.no_web
    # --research overrides the fast curated-first shortcut: go live even when
    # the curated store already has an answer.
    research = bool(args.research)

    # Route each input string through the shared natural-language parser so
    # terse ("Topaz 100"), multi-word brands ("Steel City 632S"), and spoken
    # phrases ("what's equal to a Topaz 100") all resolve the same way. A
    # global --mfr still overrides the parsed brand when given.
    parsed = []   # list of (display_label, part, mfr)
    for raw in args.parts:
        if xc is not None:
            it = xc.parse_query(raw)
            pt = it.part or raw.strip()
            mfr = args.mfr or it.mfr
            if it.research:           # a spoken "research ..." forces live too
                research = True
        else:
            # fallback: the old two-token split
            pt = raw.strip()
            mfr = args.mfr
            toks = pt.split()
            if mfr is None and len(toks) == 2 and not toks[0][0].isdigit():
                mfr, pt = toks[0], toks[1]
        parsed.append((raw, pt, mfr))

    live_policy = "always" if research else "auto"

    results = {}
    for label, pt, mfr in parsed:
        results[label] = cross_reference(pt, mfr=mfr, use_web=use_web,
                                         live_policy=live_policy, db=db)
        # keep the user's original label/part for display
        results[label]["part"] = pt
        if mfr:
            results[label]["mfr"] = mfr

    # number continuously across all parts so 'reject 2,3' is unambiguous even
    # with multiple parts, and remember the list for confirm/reject-by-number
    all_rows: list[tuple] = []
    next_n = 1
    for label, _, _ in parsed:
        rows = _print_one(results[label], start_n=next_n)
        all_rows.extend(rows)
        next_n += len(rows)
    if all_rows:
        _save_last_results(all_rows)

    # summary line when multiple parts
    if len(parsed) > 1:
        covered = sum(1 for r in results.values() if r["covered"])
        print(f"\n— covered {covered} of {len(parsed)} parts —")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
