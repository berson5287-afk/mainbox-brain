#!/usr/bin/env python3
"""
xref_common.py  -  shared helpers + natural-language parsing for the MaINbox
cross-reference toolkit.
v0.1.0

This is the "brain" layer that both the typed CLI and (later) the voice/chat
layer build on top of. The goal is that ANY way a person expresses a request —
typed terse ("Topaz 100"), typed with flags ("100 --mfr topaz"), or spoken
naturally ("what's equal to a Topaz one hundred") — flows through ONE parser
and produces the same structured intent. Nothing about the request format is
duplicated across the CLI and a voice layer; both call parse_query() here.

Two things live here:

1. Small shared utilities (normalize, manufacturer fuzzy-match) so the same
   logic isn't reimplemented (and allowed to drift) across modules.

2. parse_query(phrase) -> Intent : turns a free-form phrase into a structured
   request (action + part + manufacturer + selection), tolerant of natural
   speech, filler words, and multi-word brands.

Design principle (for voice): everything here must work when SAID OUT LOUD.
No required punctuation, flags, or exact tokens — those are typed conveniences
layered on top, never the only way in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__version__ = "0.1.0"


# --- normalization -----------------------------------------------------------
def normalize(s: str) -> str:
    """Canonical form for comparing part numbers / brands: drop every separator
    and case. 'BR-320' -> 'br320', 'TA 0' -> 'ta0'. This is THE normalize used
    everywhere (part dedup, mfr matching, relevance gating)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# --- manufacturer matching ---------------------------------------------------
def match_mfr_option(mfr: str, options: list[dict]) -> dict | None:
    """Fuzzy-match a user-given manufacturer to a dropdown option.
    options: [{value, label}]. Returns the single confident match or None
    (ambiguous/none -> caller shows a picker). Matching is separator/case
    insensitive and accepts the brand appearing as part of a longer label
    (e.g. 'Cooper' -> 'Eaton - Cooper - Arrow Hart')."""
    if not mfr or not options:
        return None
    want = normalize(mfr)
    if not want:
        return None
    # 1) exact normalized label match
    for o in options:
        if normalize(o.get("label", "")) == want:
            return o
    # 2) the label contains the wanted brand (unique match only)
    contains = [o for o in options if want in normalize(o.get("label", ""))]
    if len(contains) == 1:
        return contains[0]
    # 3) the label is contained in the wanted brand (unique, only if no #2)
    rev = [o for o in options
           if normalize(o.get("label", "")) and
           normalize(o.get("label", "")) in want]
    if not contains and len(rev) == 1:
        return rev[0]
    return None


# --- natural-language parsing ------------------------------------------------
# words that commonly wrap a spoken cross-reference request and carry no
# information — stripped before we try to extract the part/brand.
_FILLER = {
    "what", "whats", "what's", "is", "the", "a", "an", "of", "for", "to",
    "me", "find", "show", "get", "give", "tell", "look", "lookup", "look-up",
    "search", "please", "can", "you", "i", "want", "need", "equal", "equals",
    "equivalent", "equivalents", "cross", "crosses", "crossing",
    "cross-reference", "crossreference", "reference", "ref", "match", "matches",
    "matching", "replacement", "replace", "substitute", "sub", "alternative",
    "alt", "comparable", "version", "part", "number", "sku", "item",
    "do", "does", "have", "any", "there", "that", "this", "with", "from",
    "on", "in", "by", "and", "or", "whats's", "everything", "every", "all",
    "them", "these", "those", "something", "anything", "thats", "its", "me",
    "us", "we", "my", "our", "your", "their", "his", "her",
}

# verbs that signal a CORRECTION action rather than a lookup
_CONFIRM_WORDS = {"confirm", "confirmed", "correct", "right", "good", "yes",
                  "keep", "accept", "approve", "verify", "validated"}
_REJECT_WORDS = {"reject", "rejected", "wrong", "remove", "delete", "no",
                 "bad", "discard", "drop", "incorrect"}

# words that signal "list what sources I can query"
_LIST_WORDS = {"sites", "sources", "vendors", "list", "what can", "available",
               "taught"}

# spoken number words -> int (for "reject the second one", "confirm two")
_NUMBER_WORDS = {
    "one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3,
    "four": 4, "fourth": 4, "five": 5, "fifth": 5, "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "eight": 8, "eighth": 8, "nine": 9, "ninth": 9,
    "ten": 10, "tenth": 10,
}
_ALL_WORDS = {"all", "everything", "every", "both", "them", "these"}


@dataclass
class Intent:
    """Structured result of parsing a phrase. The CLI and voice layer both act
    on this — never on the raw text."""
    action: str = "lookup"          # lookup | confirm | reject | list_sites
    part: str | None = None         # the competitor part number
    mfr: str | None = None          # manufacturer / brand, if stated
    selection: list[int] = field(default_factory=list)  # numbered picks
    select_all: bool = False        # "confirm all"
    research: bool = False           # force live vendor lookup
    site: str | None = None         # explicit site/slug if named
    raw: str = ""                   # original phrase

    def __repr__(self):
        bits = [f"action={self.action!r}"]
        if self.part:
            bits.append(f"part={self.part!r}")
        if self.mfr:
            bits.append(f"mfr={self.mfr!r}")
        if self.selection:
            bits.append(f"selection={self.selection}")
        if self.select_all:
            bits.append("all=True")
        if self.research:
            bits.append("research=True")
        if self.site:
            bits.append(f"site={self.site!r}")
        return "Intent(" + ", ".join(bits) + ")"


def _looks_like_part(token: str) -> bool:
    """A part number token contains at least one digit (e.g. '100', 'BR320',
    'TA-0', '632S'). Pure-alpha tokens are treated as brand words."""
    return bool(re.search(r"\d", token))


def parse_selection(text: str) -> tuple[list[int], bool]:
    """Parse a numbered selection out of a phrase, accepting digits, ranges,
    and SPOKEN forms. Returns (list_of_numbers, select_all).
      '2,3'              -> ([2,3], False)
      '1-3'              -> ([1,2,3], False)
      'two and three'    -> ([2,3], False)
      'the second one'   -> ([2], False)
      'all' / 'everything'-> ([], True)
    """
    t = (text or "").lower()
    if any(w in t.split() for w in _ALL_WORDS) or "all" in t:
        return [], True
    # "the second one", "the third one" — here 'one' is a filler noun, not the
    # number 1. Strip a trailing 'one'/'ones' that follows an ordinal word so it
    # isn't miscounted.
    t = re.sub(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|"
               r"ninth|tenth)\s+ones?\b", r"\1", t)
    nums: set[int] = set()
    # ranges: '1-3' or '1 to 3'
    for a, b in re.findall(r"(\d+)\s*(?:-|to|through|thru)\s*(\d+)", t):
        nums.update(range(min(int(a), int(b)), max(int(a), int(b)) + 1))
    # remove matched ranges so the bare-digit pass doesn't double count edges
    t_wo_ranges = re.sub(r"\d+\s*(?:-|to|through|thru)\s*\d+", " ", t)
    # bare digits
    for d in re.findall(r"\d+", t_wo_ranges):
        nums.add(int(d))
    # spoken number words
    for w in re.split(r"[^a-z]+", t_wo_ranges):
        if w in _NUMBER_WORDS:
            nums.add(_NUMBER_WORDS[w])
    return sorted(nums), False


def parse_query(phrase: str) -> Intent:
    """Turn a free-form phrase into a structured Intent. Handles terse,
    flagged, and spoken-natural forms uniformly.

    Examples that all work:
      "QO130"
      "Topaz 100"
      "Steel City 632S"                       (multi-word brand)
      "what's equal to a Topaz 100"           (spoken)
      "find the Arlington cross for Bridgeport 380"
      "reject 2 and 3"  /  "confirm all"  /  "reject the second one"
      "what sites can you search"
    """
    intent = Intent(raw=phrase or "")
    t = (phrase or "").strip()
    if not t:
        return intent
    low = t.lower()
    words = re.findall(r"[a-z0-9][\w'\-/]*", low)

    # --- research override anywhere in the phrase ------------------------
    if re.search(r"\b(research|fresh|live|latest|re-?check|look again|"
                 r"check online|check the web)\b", low):
        intent.research = True

    # --- list sites ------------------------------------------------------
    if (re.search(r"\b(what|which)\b.*\b(sites|sources|vendors|can you "
                  r"search|do you know)\b", low) or
            re.search(r"\b(list|show)\b.*\b(sites|sources|vendors)\b", low)):
        intent.action = "list_sites"
        return intent

    # --- correction (confirm / reject) -----------------------------------
    # only treat as a correction when a correction verb is present AND there's
    # a number/selection or 'all' — so "is QO130 correct?" doesn't misfire as a
    # bare confirm. We look for the verb as a leading-ish word.
    first_few = set(words[:3])
    has_confirm = bool(first_few & _CONFIRM_WORDS) or \
        bool(re.search(r"\b(confirm|approve|accept|keep)\b", low))
    has_reject = bool(first_few & _REJECT_WORDS) or \
        bool(re.search(r"\b(reject|remove|delete|discard|drop)\b", low))
    if has_confirm or has_reject:
        sel, all_ = parse_selection(low)
        if sel or all_:
            intent.action = "confirm" if has_confirm and not has_reject \
                else "reject"
            intent.selection = sel
            intent.select_all = all_
            return intent
        # a correction verb but no target -> still mark the action so the CLI
        # can prompt "which ones?"
        if has_confirm ^ has_reject:
            intent.action = "confirm" if has_confirm else "reject"
            return intent

    # --- lookup: extract manufacturer + part -----------------------------
    # Work on a copy of the phrase with research keywords removed so they don't
    # leak into the brand. (We already set intent.research above.)
    low_clean = re.sub(r"\b(research|fresh|live|latest|re-?check|look again|"
                       r"check online|check the web)\b", " ", low)

    # Pattern: "<SITE> cross for <BRAND> <PART>" — when the phrase names a
    # target site before 'cross/reference for', that leading word is the site,
    # and the brand is what follows 'for'. Split on 'for' if a cross-word
    # precedes it.
    site_name = None
    mfor = re.search(r"\b([a-z][\w\-]*)\s+(?:cross|reference|crosses|"
                     r"cross-reference)\b.*?\bfor\b\s+(.*)$", low_clean)
    if mfor and mfor.group(1) not in _FILLER:
        site_name = mfor.group(1)
        low_clean = mfor.group(2)   # parse brand+part from the part after 'for'
    elif mfor:
        # the word before 'cross' was filler ('a cross for ...') — not a site;
        # still use the part-after-'for' as the brand+part source
        low_clean = mfor.group(2)

    words_clean = re.findall(r"[a-z0-9][\w'\-/]*", low_clean)
    meaningful = [w for w in words_clean if w not in _FILLER]

    # the part number = the first token containing a digit. The manufacturer =
    # the alpha tokens before it (handles multi-word brands like 'steel city').
    part = None
    mfr_tokens: list[str] = []
    for w in meaningful:
        if _looks_like_part(w) and part is None:
            part = w
        elif part is None:
            mfr_tokens.append(w)

    if part:
        m = re.search(re.escape(part), t, flags=re.IGNORECASE)
        intent.part = m.group(0) if m else part
    if mfr_tokens:
        mfr_join = " ".join(mfr_tokens)
        m = re.search(re.escape(mfr_join), t, flags=re.IGNORECASE)
        intent.mfr = m.group(0) if m else mfr_join
    if site_name:
        intent.site = site_name

    intent.action = "lookup"
    return intent


# --- convenience for callers -------------------------------------------------
def describe_intent(intent: Intent) -> str:
    """A short, speakable description of what we understood — useful for voice
    confirmations ('Looking up the Topaz equivalent of 100')."""
    if intent.action == "list_sites":
        return "Listing the cross-reference sources I can search."
    if intent.action in ("confirm", "reject"):
        what = "all of them" if intent.select_all else (
            "number " + ", ".join(map(str, intent.selection))
            if intent.selection else "(no selection)")
        verb = "Confirming" if intent.action == "confirm" else "Rejecting"
        return f"{verb} {what} from the last results."
    # lookup
    if intent.part and intent.mfr:
        return f"Looking up the {intent.mfr} equivalent of {intent.part}."
    if intent.part:
        return f"Looking up cross-references for {intent.part}."
    return "I didn't catch a part number — what part should I look up?"


if __name__ == "__main__":
    # quick self-demo of the parser
    tests = [
        "QO130",
        "Topaz 100",
        "Steel City 632S",
        "what's equal to a Topaz 100",
        "find the Arlington cross for Bridgeport 380",
        "100 from topaz",
        "reject 2 and 3",
        "confirm all",
        "reject the second one",
        "confirm two",
        "what sites can you search",
        "research Hubbell 257",
        "is there a cross for Ilsco TA-0",
    ]
    print(f"xref_common v{__version__} — parser demo\n")
    for t in tests:
        it = parse_query(t)
        print(f"  {t!r}")
        print(f"      -> {it!r}")
        print(f"      -> {describe_intent(it)}")
        print()
