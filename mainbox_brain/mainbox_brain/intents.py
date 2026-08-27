"""
Intent routing and grounded answering for MaINbox Brain.

This layer decides whether the user is asking a question, teaching the brain
something, or starting an RFQ. Question answers are intentionally grounded in
SQLite history. When an LLM is available, it may polish the wording, but it is
not allowed to invent facts beyond the evidence block.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional

from . import vendors
from . import catalog_lookup

INTENT_HISTORY = "history_query"
INTENT_PRICE = "price_query"
INTENT_LEADTIME = "leadtime_query"
INTENT_CONTACTS = "contacts_query"
INTENT_VENDORS_FOR = "vendors_for_product"
INTENT_ADD_CONTACT = "add_contact"
INTENT_SUBSTITUTE = "substitute_query"
INTENT_RESEARCH = "research_query"
INTENT_CUSTOMER_ORDERS = "customer_orders"
INTENT_QUOTE = "quote_request"

_CONTACTS_RE = re.compile(
    r"^\s*(?:list\s+|show\s+|who\s+(?:are|is)\s+(?:the\s+)?)?contacts?\s+(?:at|for|@)\s+(?P<v>.+?)\s*\??\s*$"
    r"|^\s*who\s+do\s+i\s+(?:talk|speak)\s+to\s+at\s+(?P<v2>.+?)\s*\??\s*$",
    re.IGNORECASE)
_LEADTIME_RE = re.compile(r"\blead\s*time\b|\beta\b|\bhow\s+long\b.*\b(?:for|on)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"\b(?:pay|paid|price[ds]?|cost|how much|last quote|quoted|quote|no\s*quote|no\s*bid)\b", re.IGNORECASE)
# verbs that mean 'start the send-an-RFQ flow' rather than 'answer me a price'
_SEND_VERBS = re.compile(
    r"\b(send|sent|email|fire|blast|shoot|request|rfq|reach\s+out|"
    r"get\s+(?:me\s+)?(?:a\s+)?(?:new\s+)?(?:quote|price|pricing|p&a)|"
    r"quote\s+(?:this|it|me|out|\d))", re.IGNORECASE)
_HISTORY_RE = re.compile(
    r"\bwho\b.*\b(?:vendor|from|buy|bought|got|get|order(?:ed)?|sen[dt])\b"
    r"|\bwhere\s+did\s+i\b|\blast\s+(?:vendor|time|place|rfq|quote)\b|\bwhen\s+did\s+i\b",
    re.IGNORECASE)
_VENDORS_FOR_RE = re.compile(
    r"^\s*(?:list|show|which|who)\b.*?\b(?:vendors?|carries|carry|sells?|quotes?|suppliers?)\b"
    r"|^\s*(?:list\s+)?(?P<p>[\w\s/\"'.\-]+?)\s+(?:vendors?|suppliers?)\s*\??\s*$",
    re.IGNORECASE)
_ADD_CONTACT_RE = re.compile(
    r"^\s*(?:add|save|remember)\s+(?P<name>[A-Za-z][\w'\-]+)\s+"
    r"(?P<email>[\w.+\-]+@[\w.\-]+\.\w+)"
    r"(?:\s+(?:as|for|to)\s+(?P<tag>.+?))?\s*$",
    re.IGNORECASE)
_QUESTIONY = re.compile(r"^\s*(?:who|what|when|where|how|which|did|do|have|has|can|could|should)\b"
                        r"|\?\s*$", re.IGNORECASE)

_Q_STOP = {
    "who", "what", "when", "where", "how", "which", "did", "do", "does", "have",
    "has", "was", "is", "are", "were", "the", "a", "an", "i", "we", "my", "our",
    "last", "vendor", "vendors", "supplier", "suppliers", "from", "for", "of", "on", "to", "at", "in",
    "get", "got", "buy", "bought", "order", "ordered", "pay", "paid", "price",
    "prices", "priced", "cost", "much", "long", "lead", "time", "eta", "it", "that",
    "this", "again", "recently", "place", "go", "went", "sent", "send", "quote", "quoted", "rfq",
    "availability", "available", "stock", "anyone", "anybody", "someone", "vendor", "no", "bid", "no-bid",
    "purchase", "purchased", "orders", "costs", "po", "pricing", "latest", "current", "recent", "previous",
    "and", "or", "with", "any", "some", "me", "us", "give", "tell", "show", "find", "look", "check",
    "please", "pls", "p&a", "p", "availabilty", "avail", "lead-time", "leadtime",
}
_TOKEN = re.compile(r"[a-z0-9/\"'\.\-]+", re.IGNORECASE)


@dataclass
class Intent:
    kind: str
    product: str = ""
    vendor_text: str = ""
    name: str = ""
    email: str = ""
    tag: str = ""
    clarify: str = ""        # set to a colliding customer name when 'we/I' is ambiguous


def is_question(text: str) -> bool:
    return bool(_QUESTIONY.search(text or ""))


# "research <item>" / "look it up online" -> hit the LLM for cross-references.
# Kept separate from the substitute scan so the phone can wire a button to it
# and the console can accept a plain 'yes' after the offer (see ask loop).
_RESEARCH_RE = re.compile(
    r"\b(research|look\s*up|search\s+online|find\s+online|google)\b", re.IGNORECASE)
# "substitute/alternate/equivalent/cross-ref/replacement for X", "what can I
# use instead of X", "sub for X"
_SUBSTITUTE_RE = re.compile(
    r"\b(substitut\w*|alternat\w*|equivalent\w*|cross[\s-]?ref\w*|interchang\w*|"
    r"replacement|comparable)\b|\binstead\s+of\b|\bsub\s+for\b", re.IGNORECASE)
# customer purchase-history questions: "what did <customer> order/buy/pay for",
# "<customer> purchase history", "what did we sell <customer>"
_CUST_ORDER_RE = re.compile(
    r"\b(?:order(?:ed|s)?|bought|buy|buying|purchas\w+|sold|sell|pay|paid)\b.*\bhistory\b"
    r"|\bhistory\b.*\b(?:order|purchas\w+)\b"
    r"|\bwhat\s+(?:did|has|have|does)\b.*\b(?:order(?:ed)?|bought|buy|purchas\w+|pay|paid)\b"
    r"|\bwhat\s+(?:did|have)\s+we\s+sell\b"
    r"|\bwhat\s+(?:did|has)\b.*\bbuy\b", re.IGNORECASE)


def _extract_customer_and_product(text: str) -> tuple[str, str]:
    """Pull a customer name (and optional product) out of a purchase-history
    question.  Customer is the phrase tied to the order verb; product is a
    quoted phrase or a 'for X' tail."""
    t = text or ""
    product = ""
    m = re.search(r'"([^"]+)"', t)
    if m:
        product = m.group(1).strip()
    # "what did <customer> order/buy/pay for"
    m = re.search(r"\bwhat\s+(?:did|has|have|does)\s+(.+?)\s+"
                  r"(?:order|ordered|buy|bought|purchas\w+|pay|paid|get|got)\b", t, re.I)
    cust = m.group(1).strip() if m else ""
    if not cust:
        m = re.search(r"\bsell\s+(?:to\s+)?(.+?)(?:\?|$|\bfor\b)", t, re.I)
        cust = m.group(1).strip() if m else ""
    if not cust:
        m = re.search(r"^(.*?)(?:'s)?\s+(?:purchase|order|buying)\s+history", t, re.I)
        cust = m.group(1).strip() if m else ""
    cust = re.sub(r"^(the|for|from|our customer|customer|show me|tell me|list)\s+", "", cust, flags=re.I).strip()
    # drop time/filler words so "we last" -> "we" (first-person), "Bender lately"
    # -> "Bender", etc.
    cust = re.sub(r"\b(last|lately|recently|ever|just|already|previously|before|"
                  r"again|usually|typically|normally|historically)\b", "", cust, flags=re.I)
    cust = re.sub(r"\s+", " ", cust).strip()
    cust = re.sub(r"[?.!]+$", "", cust).strip()
    pm = re.search(r"\bfor\s+(.+?)(?:\?|$)", t, re.I)
    if pm and not product and "history" not in pm.group(1).lower():
        product = pm.group(1).strip()
    # strip trailing time words from the product ("... last", "... recently")
    product = re.sub(r"\b(last|recently|lately|again)\s*[?.!]*$", "", product, flags=re.I).strip()
    return cust, product


_FIRST_PERSON = {"we", "us", "i", "our", "me", "my", "you", "ya"}


def _customer_name_collision(store, pronoun: str) -> str:
    """If a registered customer's name actually BEGINS with the pronoun word
    (e.g. customer 'We Are Good Electric' vs the word 'we'), return that name so
    we can ask which the user meant.  A name like 'Welsbach' does NOT collide --
    its first word is 'welsbach', not 'we'."""
    if store is None:
        return ""
    p = (pronoun or "").lower().strip()
    try:
        names = store.customers()
    except Exception:
        return ""
    for name in names:
        words = re.findall(r"[a-z]+", (name or "").lower())
        if words and words[0] == p:
            return name
    return ""
_SUB_STRIP_RE = re.compile(
    r"\b(find|get|show|give|me|a|an|the|any|some|please|pls|whats?|what\s+is|"
    r"can\s+i\s+use|use|to|for|of|substitut\w*|sub|alternat\w*|alternativ\w*|"
    r"equivalent\w*|cross[\s-]?ref\w*|interchang\w*|replacement|comparable|"
    r"instead|other|options?|something|else|online|research)\b", re.IGNORECASE)


def _strip_to_product(text: str, strip_re: re.Pattern) -> str:
    """Pull the part/description out of a substitute/research request, keeping
    any quoted phrase exact."""
    from .store import parse_quoted_phrases
    phrases, _free = parse_quoted_phrases(text or "")
    if phrases:
        return '"' + phrases[0] + '"'
    out = strip_re.sub(" ", text or "")
    out = re.sub(r"[?\.!,]+", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def extract_product(text: str) -> str:
    # if the user quoted a phrase, honor it exactly (drives substring search).
    # Inch-mark aware: '"3/4" red emt"' is ONE phrase, the 4" is an inch.
    from .store import parse_quoted_phrases
    phrases, _free = parse_quoted_phrases(text or "")
    if phrases:
        return '"' + phrases[0] + '"'
    kept = [t for t in _TOKEN.findall(text)
            if t.lower().strip("\"'.") not in _Q_STOP and len(t.strip("\"'.")) > 0]
    return " ".join(kept).strip()


def classify(text: str, llm=None, store=None) -> Intent:
    if llm is not None:
        it = _classify_llm(text, llm)
        if it is not None:
            return it
    m = _ADD_CONTACT_RE.match(text or "")
    if m:
        return Intent(INTENT_ADD_CONTACT, name=m.group("name"),
                      email=m.group("email"), tag=(m.group("tag") or "").strip())
    # explicit 'research X' / 'look up X online' -> LLM cross-references
    if _RESEARCH_RE.search(text or ""):
        prod = _strip_to_product(text, _SUB_STRIP_RE)
        if prod:
            return Intent(INTENT_RESEARCH, product=prod)
    # customer purchase history: "what did <customer> order", "<cust> history"
    if _CUST_ORDER_RE.search(text or ""):
        cust, prod = _extract_customer_and_product(text)
        if cust and re.search(r"[a-z]", cust, re.I):
            if cust.lower() in _FIRST_PERSON:
                # "what did WE order" -> the user's own (American Power) buying,
                # which is a vendor/cost question -> fall through. BUT if a real
                # customer's name begins with that word, it's ambiguous: ask.
                collision = _customer_name_collision(store, cust)
                if collision:
                    return Intent(INTENT_CUSTOMER_ORDERS, vendor_text=cust,
                                  product=prod, clarify=collision)
                # else: fall through to vendor/cost handling
            else:
                return Intent(INTENT_CUSTOMER_ORDERS, vendor_text=cust, product=prod)
    # 'substitute/alternate/equivalent/cross-ref for X'
    if _SUBSTITUTE_RE.search(text or ""):
        prod = _strip_to_product(text, _SUB_STRIP_RE)
        if prod:
            return Intent(INTENT_SUBSTITUTE, product=prod)
    if _HISTORY_RE.search(text) and re.search(r"\blast\b|\bwhen\b", text, re.IGNORECASE):
        return Intent(INTENT_HISTORY, product=extract_product(text))
    m = _VENDORS_FOR_RE.match(text or "")
    if m:
        prod = m.group("p") if m.groupdict().get("p") else extract_product(
            re.sub(r"\b(list|show|which|who|vendors?|suppliers?|carries|carry|sells?|quotes?)\b",
                   " ", text, flags=re.IGNORECASE))
        prod = re.sub(r"\b(list|show|vendors?|suppliers?)\b", " ", prod or "",
                      flags=re.IGNORECASE).strip()
        if prod:
            return Intent(INTENT_VENDORS_FOR, product=prod)
    m = _CONTACTS_RE.match(text or "")
    if m:
        return Intent(INTENT_CONTACTS, vendor_text=(m.group("v") or m.group("v2") or "").strip())
    if _LEADTIME_RE.search(text) and is_question(text):
        return Intent(INTENT_LEADTIME, product=extract_product(text))
    if _PRICE_RE.search(text) and is_question(text):
        return Intent(INTENT_PRICE, product=extract_product(text))
    if _HISTORY_RE.search(text):
        return Intent(INTENT_HISTORY, product=extract_product(text))
    # statements like 'last price of X' / 'price of 12/2 mc' are still price
    # QUESTIONS unless the user used a send-ish verb ('get me a quote on X',
    # 'send an rfq for X') — only those should start the send-RFQ flow.
    if not _SEND_VERBS.search(text):
        if _LEADTIME_RE.search(text):
            return Intent(INTENT_LEADTIME, product=extract_product(text))
        if _PRICE_RE.search(text):
            return Intent(INTENT_PRICE, product=extract_product(text))
    return Intent(INTENT_QUOTE)


# ---------------------------------------------------------------------------
# Grounded answer helpers
# ---------------------------------------------------------------------------
def _fmt_date(value: str | None) -> str:
    if not value:
        return "date unknown"
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(value)[:19]).strftime("%m-%d-%Y")
    except ValueError:
        return str(value)[:10]


def _age_note(when: str | None) -> str:
    """Flag stale data so an old quote isn't presented as if it were current."""
    if not when:
        return ""
    try:
        from datetime import datetime as _dt
        days = (_dt.now() - _dt.fromisoformat(str(when)[:10])).days
    except ValueError:
        return ""
    if days >= 240:
        return f" — heads up, that's about {days // 30} months old, worth re-quoting"
    if days >= 120:
        return f" (about {days // 30} months old)"
    return ""


def _evidence_lines(hits: list[dict], limit: int = 5) -> list[str]:
    out = []
    for idx, h in enumerate(hits[:limit], 1):
        when = _fmt_date(h.get("when"))
        item = (h.get("item") or "").strip()
        vendor = h.get("vendor") or "?"
        to = h.get("to") or "?"
        score = int(float(h.get("score") or 0) * 100)
        out.append(f"{idx}. {when} — {vendor} <{to}> — {item} ({score}% match)")
    return out




def _reply_detail(h: dict, *, want: str = "") -> str:
    bits = []
    if h.get("unit_price") is not None:
        unit = f"/{h.get('unit')}" if h.get("unit") else ""
        bits.append(f"price ${h['unit_price']:g}{unit}")
    if h.get("ext_price") is not None:
        bits.append(f"extended ${h['ext_price']:g}")
    if h.get("availability"):
        bits.append(str(h["availability"]).replace("_", " "))
    if h.get("lead_time"):
        bits.append(f"lead time {h['lead_time']}")
    if h.get("eta"):
        bits.append(f"ETA {h['eta']}")
    if h.get("fact_status") in {"no_quote", "alternate"}:
        bits.append(str(h["fact_status"]).replace("_", " "))
    if not bits:
        bits.append(h.get("status") or "reply found")
    return "; ".join(bits)


# ---------------------------------------------------------------------------
# v0.9 "show me the reference": every hit that contributes to a price / lead
# time / stock answer is recorded here (thread-local, so concurrent /ask calls
# never mix) so the caller can hand the phone structured source cards --
# vendor, contact, subject, date, the fact, and the email's source_key -- in
# addition to the prose answer.
# ---------------------------------------------------------------------------
import threading as _threading
_SRC = _threading.local()


def sources_reset() -> None:
    _SRC.items = []


def sources_get() -> list[dict]:
    return list(getattr(_SRC, "items", []) or [])


def _note_source(h: dict, role: str = "") -> None:
    if not isinstance(h, dict):
        return
    items = getattr(_SRC, "items", None)
    if items is None:
        _SRC.items = items = []
    key = h.get("source_key") or ""
    # one card per email (the same message can supply several facts)
    for it in items:
        if key and it.get("key") == key:
            if role and role not in it.get("roles", []):
                it["roles"].append(role)
            return
    items.append({
        "key": key,
        "vendor": h.get("vendor") or "",
        "from": h.get("from") or "",
        "from_name": h.get("from_name") or "",
        "subject": h.get("subject") or "",
        "when": h.get("when") or "",
        "item": h.get("item") or "",
        "line": h.get("line") or "",
        "detail": _reply_detail(h),
        "po_number": h.get("po_number") or "",
        "roles": [role] if role else [],
    })


def _reply_evidence_lines(hits: list[dict], limit: int = 5, *, want: str = "") -> list[str]:
    out = []
    for idx, h in enumerate(hits[:limit], 1):
        when = _fmt_date(h.get("when"))
        vendor = h.get("vendor") or "?"
        contact = (h.get("from_name") or "").strip()
        po = h.get("po_number") or ""
        line = (h.get("line") or h.get("subject") or "").strip()
        detail = _reply_detail(h, want=want)
        conf = int(float(h.get("fact_confidence") or h.get("confidence") or 0) * 100)
        # date — Vendor — Contact — PO# — detail   (matches reply-find layout)
        segs = [when, vendor]
        if contact:
            segs.append(contact)
        if po:
            segs.append(po)
        segs.append(detail)
        out.append(f"{idx}. " + " — ".join(segs) + f" — evidence: {line} ({conf}% confidence)")
        _note_source(h, "evidence")
    return out


def _filter_reply_hits(hits: list[dict], what: str) -> list[dict]:
    if what == "price":
        preferred = [h for h in hits if h.get("unit_price") is not None or h.get("ext_price") is not None]
    elif what == "lead time":
        preferred = [h for h in hits if h.get("lead_time") or h.get("eta")]
    else:
        preferred = hits
    return preferred or hits

def _product_label(product: str) -> str:
    return product.strip().strip('"') or "that item"


# v0.8.3: electrical pricing-unit glossary — C and M are Roman-numeral
# counts (per 100 / per 1,000), NEVER metric lengths.
_UNIT_GLOSS = {"c": "per 100", "m": "per 1,000", "e": "each", "ea": "each",
               "mft": "per 1,000 ft", "cft": "per 100 ft",
               "kft": "per 1,000 ft"}


def _price_str(h: dict) -> str:
    if h.get("unit_price") is None:
        return "price n/a"
    u = (h.get("unit") or "").strip()
    unit = f"/{u}" if u else ""
    gloss = _UNIT_GLOSS.get(u.lower().lstrip("/"))
    if gloss:
        unit += f" ({gloss})"
    return f"${h['unit_price']:g}{unit}"


def _grab(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return (m.group(1).strip(" .,-") if m else "")


def _quote_context(h: dict, customers: list[str] | None = None) -> str:
    """Best-effort 'for <customer> (job: <job>)' for a quote.

    Sources, in order: a registered customer/job name that appears in the
    quote's subject/line; explicit labels (Customer:/Job:/Contractor:); and a
    detected location (City ST ZIP). Stays quiet when nothing is found, so it
    never invents a customer."""
    text = " ".join([h.get("subject", ""), h.get("line", ""), h.get("item", "")])
    who = ""
    # 1) registered customer/job names (reliable -- user taught these)
    for name in (customers or []):
        if name and re.search(r"\b" + re.escape(name) + r"\b", text, re.IGNORECASE):
            who = name
            break
    # 2) explicit labels
    if not who:
        who = (_grab(text, r"(?:customer|sold\s*to|bill\s*to)\s*[:#]\s*([A-Za-z0-9][\w &.,'\-]{2,40})")
               or _grab(text, r"(?:contractor|gc)\s*[:#]\s*([A-Za-z0-9][\w &.,'\-]{2,40})"))
    job = _grab(text, r"(?:job\s*name|project|job)\s*[:#]\s*([A-Za-z0-9][\w &.,'\-]{2,40})")
    # 3) a location like "Brooklyn NY 11201" or "NY 10962" (case-sensitive:
    #    must be real Capitalized place + 2-letter state, not lowercase words)
    mloc = (re.search(r"([A-Z][A-Za-z]+(?:[ .][A-Z][A-Za-z]+)*,?\s+[A-Z]{2}\s+\d{5})", text)
            or re.search(r"\b([A-Z]{2}\s+\d{5})\b", text))
    loc = mloc.group(1).strip(" .,-") if mloc else ""
    bits = []
    if who:
        bits.append(who)
    if job and job.lower() not in who.lower():
        bits.append(f"job: {job}")
    if loc and loc.lower() not in " ".join(bits).lower():
        bits.append(loc)
    return (" for " + " / ".join(bits)) if bits else ""


def _price_answer(store, product: str, label: str, reply_hits: list[dict]) -> str:
    """Multi-source price answer: most recent quote (with customer/job when
    present), most recent purchase-order price, and the catalog price file."""
    priced = [h for h in reply_hits
              if h.get("unit_price") is not None and h.get("direction") != "sell"]
    quotes = [h for h in priced if not h.get("po_number")]
    orders = [h for h in priced if h.get("po_number")]

    def best(hits):
        # "what did we LAST pay/quote" -> recency is what the user expects.
        # Pick the MOST RECENT among the near-top-scoring matches, so a clean
        # older line can't bury a recent good quote (e.g. a 6/2 Thea wire quote
        # losing to a 4/24 line that merely scores a hair higher).
        if not hits:
            return None
        top = max(round(float(h.get("score") or 0), 3) for h in hits)
        band = [h for h in hits if round(float(h.get("score") or 0), 3) >= top - 0.25]
        return max(band, key=lambda h: h.get("when") or "")

    lq, lo = best(quotes), best(orders)
    # Guard against the PO line reporting a different product than the quote.
    # '12 str' matches both '#12 Str CU THHN' (wire, /mft) and a 'FLEX STR CONN'
    # (connector, /c) -- same query words, different items.  If the best quote
    # and best PO share no DISTINCTIVE words (beyond the query terms), they're
    # different products, so the PO line is misleading -> drop it.
    if lq and lo:
        qtok = set(re.findall(r"[a-z0-9]+", (product or "").lower()))
        _gen = {"in", "ea", "the", "for", "of", "ft", "pc", "pcs", "box", "cu"}

        def _distinct(hit):
            txt = (hit.get("item") or hit.get("source_line") or "").lower()
            return set(re.findall(r"[a-z]+", txt)) - qtok - _gen
        dq, do = _distinct(lq), _distinct(lo)
        if dq and do and not (dq & do):
            lo = None

    cat = catalog_lookup.best(product) if catalog_lookup.available() else None
    try:
        customers = store.customers() if hasattr(store, "customers") else []
    except Exception:
        customers = []

    lines = []
    if lq:
        _note_source(lq, "last quote")
    if lo:
        _note_source(lo, "last PO")
    if lq:
        seg = (f"Last quoted{_quote_context(lq, customers)} on {_fmt_date(lq.get('when'))} "
               f"from {lq.get('vendor')} at {_price_str(lq)}")
        stock = (lq.get("availability") or "").replace("_", " ").strip()
        if stock:
            seg += f", {stock}"
        lines.append(seg + f"{_age_note(lq.get('when'))}{_item_snip(lq)}.")
    if lo:
        lines.append(f"Last purchase-order price was {_fmt_date(lo.get('when'))} "
                     f"at {_price_str(lo)} (PO {lo.get('po_number')}){_item_snip(lo)}.")
    if cat and cat.price_net:
        unit = f"/{cat.pricing_unit}" if cat.pricing_unit else ""
        eff = cat.effective_date or "undated"
        desc = (f"{cat.manufacturer} {cat.description}").strip()
        lines.append(f"Price file ({eff}): ${cat.price_net:g}{unit} — {desc[:50]}.")

    if not lines:
        rfq_hits = store.find(product, limit=5)
        if rfq_hits:
            return (f"I don't have a quoted, ordered, or catalog price for {label!r} yet. "
                    "Where you previously asked about it:\n"
                    + "\n".join(_evidence_lines(rfq_hits, 5))
                    + "\n\nRun the reply miner on your vendor emails, and set "
                      "MAINBOX_CATALOG_DB to your catalog, so I can store real prices.")
        return (f"I don't have a quoted, ordered, or catalog price for {label!r}. "
                "Send a fresh RFQ, or point MAINBOX_CATALOG_DB at your product catalog.")

    out = [f"Pricing for {label!r}:"] + lines

    # If a vendor sent a price-increase notice AFTER the date of the price
    # we're reporting, warn — that quote/PO price may already be stale.
    try:
        # v0.8.3: compare notices against the NEWEST price shown, not each
        # line separately — a 2025 notice is old news next to a 2026 PO and
        # must not be framed as affecting current pricing.
        newest_when = max([(x.get("when") or "")[:10]
                           for x in (lq, lo) if x] or [""])
        for src in (lq, lo):
            if not src or not hasattr(store, "increase_notices"):
                continue
            vid = src.get("vendor_id") or ""
            when = newest_when
            if not vid or not when:
                continue
            notices = store.increase_notices(vendor_id=vid, after=when)
            if notices:
                n0 = notices[0]
                more = f" (+{len(notices)-1} more)" if len(notices) > 1 else ""
                out.append(f"⚠ Heads up: {n0['vendor']} sent a price-increase notice on "
                           f"{_fmt_date(n0['when'])} — after the price above "
                           f"({n0['subject'][:60]!r}){more}. Worth re-quoting.")
                break
    except Exception:
        pass

    if priced:
        out.append("\nEvidence:")
        # most recent first -- for a "last paid/quoted" question the newest
        # relevant hits are what the user is looking for
        out.extend(_reply_evidence_lines(
            sorted(priced, key=lambda h: (h.get("when") or "",
                                          round(float(h.get("score") or 0), 3)),
                   reverse=True), 4))
    notes = []
    if not orders:
        notes.append("no purchase-order price on record yet — re-mine with PO extraction to fill that line")
    if not catalog_lookup.available():
        notes.append("catalog not connected — set MAINBOX_CATALOG_DB for a price-file figure")
    if notes:
        out.append("\n(" + "; ".join(notes) + ".)")
    return "\n".join(out)


def _polish_answer(question: str, grounded_answer: str, llm=None) -> str:
    """Optional style pass. The model can rewrite, not add facts."""
    if llm is None or not question or not grounded_answer:
        return grounded_answer
    system = (
        "You are MaINbox Brain, a helpful procurement assistant for an electrical supply company. "
        "Rewrite the grounded answer so it sounds natural, human, and useful. "
        "Do NOT add facts, vendors, prices, dates, lead times, or contact names that are not in the grounded answer. "
        "If the grounded answer says data is missing, keep that limitation clear. "
        # v0.8.3: electrical pricing units + recency discipline
        "PRICING UNITS: '/C' means per 100 units, '/M' means per 1,000 units "
        "(Roman numerals), '/MFT' means per 1,000 feet, '/E' or '/EA' means each. "
        "NEVER read '/M' as meters or '/C' as Celsius — this is electrical "
        "distribution pricing, not metric measurement. Keep the unit exactly as "
        "written (e.g. '$625/M (per 1,000)'). "
        "RECENCY: the grounded answer already chose the operative (most recent) "
        "price — lead with it. Present older dates or price-increase notices "
        "only as history, never as the current price or a current concern "
        "unless the grounded answer itself flags them. "
        "Start directly with the answer — no meta openers like 'Here's a "
        "rewritten version'. "
        "Use a confident but honest tone. Keep it brief: 2-5 short paragraphs or bullets."
    )
    prompt = f"User question:\n{question}\n\nGrounded answer/evidence:\n{grounded_answer}\n\nRewrite only using those facts."
    text = llm.complete(prompt, system=system)
    return text.strip() if text else grounded_answer


def answer_history(store, product: str, *, question: str = "", llm=None) -> str:
    if not product:
        return "Which product should I look up? For example: “who was the last vendor I got 12/2 MC from?”"
    rfq_hits = store.find(product, limit=6)
    reply_hits = store.find_replies(product, limit=6) if hasattr(store, "find_replies") else []
    label = _product_label(product)
    if not rfq_hits and not reply_hits:
        ans = (f"I don’t see a past RFQ or vendor reply for {label!r} in the history I’ve mined yet.\n\n"
               "That means I don’t have enough evidence to name a prior vendor safely. "
               "Send it through the brain once, or mine more reply history, and I’ll store the trail for next time.")
        return _polish_answer(question, ans, llm)

    lines = []
    if reply_hits:
        top = reply_hits[0]
        lines.append(f"The strongest vendor-reply match I found for {label!r} came from {top.get('vendor')} <{top.get('from')}> on {_fmt_date(top.get('when'))}{_age_note(top.get('when'))}.")
        lines.append("\nVendor reply evidence:")
        lines.extend(_reply_evidence_lines(reply_hits, 4))
    if rfq_hits:
        top = rfq_hits[0]
        prefix = "\nThe last outgoing RFQ match" if reply_hits else "The last outgoing RFQ match"
        lines.append(f"{prefix} went to {top['vendor']} <{top['to']}> on {_fmt_date(top.get('when'))}.")
        lines.append("\nOutgoing RFQ evidence:")
        lines.extend(_evidence_lines(rfq_hits, 4))
    lines.append("\nI’d use the vendor-reply evidence first when it exists, because it shows who actually responded, not just who was asked.")
    return _polish_answer(question, "\n".join(lines), llm)


def _find_with_fallback(store, product: str, limit: int = 15) -> tuple[list[dict], str]:
    """Exact search first. If a quoted/part-number query finds nothing, relax
    to word-overlap and SAY SO, instead of silently showing unrelated items."""
    if not hasattr(store, "find_replies"):
        return [], ""
    hits = store.find_replies(product, limit=limit)
    if hits:
        return hits, ""
    from .store import parse_quoted_phrases
    phrases, _free = parse_quoted_phrases(product)
    relaxed = " ".join(phrases).strip() if phrases else product.strip()
    relaxed = relaxed.replace('"', " ").strip()
    if not relaxed:
        return [], ""
    # retry with relaxed any-overlap matching (the strict pass requires ALL
    # words; this pass surfaces near-misses and is clearly labeled)
    loose = store.find_replies(relaxed, limit=limit, require_all=False)
    if loose:
        # keep only the best-overlap tier: items matching ALL the words
        # ('3/4 IN Red S.S. EMT Conn') shouldn't be diluted by recent
        # two-of-three matches ('RED WASH', 'EMT COMPCOUPLING')
        top = max(float(h.get("score") or 0) for h in loose)
        loose = [h for h in loose if float(h.get("score") or 0) >= top - 0.2]
        shown = '"' + " ".join(phrases) + '"' if phrases else repr(product)
        note = (f"No exact match for {shown} — "
                f"here are the closest similar items instead:")
        return loose, note
    return [], ""


def answer_price_or_leadtime(store, product: str, what: str, *, question: str = "", llm=None) -> str:
    if not product:
        return f"Which product should I check the {what} for?"
    label = _product_label(product)
    reply_hits, fb_note = _find_with_fallback(store, product, limit=15)

    if what == "price":
        body = _price_answer(store, product, label, reply_hits)
        if fb_note:
            body = fb_note + "\n" + body
        return _polish_answer(question, body, llm)

    reply_hits = _filter_reply_hits(reply_hits, what)
    if reply_hits:
        top = reply_hits[0]
        age = _age_note(top.get("when"))
        ans = [fb_note] if fb_note else []
        ans.append(f"I found mined vendor-reply evidence for {label!r}.")
        if what == "price" and top.get("unit_price") is not None:
            unit = f"/{top.get('unit')}" if top.get("unit") else ""
            po = f", PO {top['po_number']}" if top.get("po_number") else ""
            ans.append(f"Best match: {top.get('vendor')} — ${top['unit_price']:g}{unit} on {_fmt_date(top.get('when'))}{po}{age}.")
        elif what == "lead time" and (top.get("lead_time") or top.get("eta")):
            lt = top.get("lead_time") or top.get("eta")
            ans.append(f"Best match: {top.get('vendor')} — lead {lt} on {_fmt_date(top.get('when'))}{age}.")
        elif what == "lead time":
            # matched the product but no lead time is on record — say so plainly
            ans = [f"I don't have a confirmed lead time for {label!r} in mined replies — "
                   f"but I do have these quotes/prices for it (no lead time stated):"]
        else:
            ans.append(f"Best match: {top.get('vendor')} replied on {_fmt_date(top.get('when'))}{age}: {_reply_detail(top, want=what)}.")
        ans.append("\nEvidence I found:")
        ans.extend(_reply_evidence_lines(reply_hits, 5, want=what))
        ans.append("\nI’d still sanity-check the original quote if the order depends on exact net pricing, expiration, freight, or quantity breaks.")
        return _polish_answer(question, "\n".join(ans), llm)

    rfq_hits = store.find(product, limit=5)
    ans = (f"I don’t have a confirmed {what} for {label!r} in mined vendor replies yet. "
           "I don’t want to make up a number or ETA.\n")
    if rfq_hits:
        ans += ("\nWhat I can confirm is where you previously asked about it:\n" +
                "\n".join(_evidence_lines(rfq_hits, 5)) +
                "\n\nBest next step: re-check that vendor, or run the reply miner on your received vendor emails so I can store the actual quoted facts.")
    else:
        ans += (f"\nI also don’t see a matching outgoing RFQ for {label!r}. Best next step: send a fresh RFQ and let the brain store the trail.")
    return _polish_answer(question, ans, llm)


def answer_contacts(store, vendor_text: str, *, question: str = "", llm=None) -> str:
    from .conversation import _strict_lookup
    v, _ = _strict_lookup(vendor_text)
    if v is None:
        v = vendors.find_vendor_by_name(vendor_text)
    if v is None:
        ans = (f"I couldn’t find a vendor matching {vendor_text!r} in the learned registry. "
               "Try the company name or domain, or teach me the contact with: add Name email@vendor.com for Vendor.")
        return _polish_answer(question, ans, llm)
    lines = [f"For {v.name}, I’d start with the most-used contact first:"]
    for idx, c in enumerate(v.contacts[:8], 1):
        marker = "primary" if idx == 1 else "backup"
        lines.append(f"{idx}. {c.name} <{c.email}> — {marker}")
    if len(v.contacts) > 1:
        lines.append("I ranked these by the contact order/usage stored in the brain.")
    return _polish_answer(question, "\n".join(lines), llm)


_LLM_ROUTER_SYSTEM = (
    "You route a procurement assistant request to ONE intent. Return ONLY JSON: "
    '{"intent": "quote_request|history_query|price_query|leadtime_query|'
    'contacts_query|vendors_for_product|add_contact", '
    '"product": string, "vendor": string, "name": string, "email": string, "tag": string}. '
    "quote_request = user wants pricing/availability SENT to vendors. "
    "history/price/leadtime = user asks about the past. "
    "vendors_for_product = who sells/carries X. add_contact = user teaches a "
    "new contact. Unfilled fields = empty string. No prose."
)
_VALID_KINDS = {INTENT_QUOTE, INTENT_HISTORY, INTENT_PRICE, INTENT_LEADTIME,
                INTENT_CONTACTS, INTENT_VENDORS_FOR, INTENT_ADD_CONTACT}


def _classify_llm(text: str, llm) -> Optional[Intent]:
    from .llm import extract_json
    raw = llm.complete(f"Request: {text}", system=_LLM_ROUTER_SYSTEM)
    parsed = extract_json(raw) if raw else None
    if not isinstance(parsed, dict) or parsed.get("intent") not in _VALID_KINDS:
        return None
    # v0.9.1: the router liked to call "set up a draft for Mark" an
    # add_contact with no address -> "Saved Mark <>". No email, no contact.
    if parsed["intent"] == INTENT_ADD_CONTACT and "@" not in str(parsed.get("email") or ""):
        return None
    return Intent(parsed["intent"],
                  product=str(parsed.get("product") or ""),
                  vendor_text=str(parsed.get("vendor") or ""),
                  name=str(parsed.get("name") or ""),
                  email=str(parsed.get("email") or ""),
                  tag=str(parsed.get("tag") or ""))


def answer_vendors_for(store, product: str, *, question: str = "", llm=None) -> str:
    rfq_hits = store.find(product, limit=25)
    reply_hits = store.find_replies(product, limit=25) if hasattr(store, "find_replies") else []
    label = _product_label(product)
    if not rfq_hits and not reply_hits:
        ans = (f"I don’t have RFQ or reply history for {label!r} yet, so I can’t safely say who you usually use for it.\n\n"
               "Name the vendor once or send an RFQ through the brain, and I’ll learn that sourcing path for next time.")
        return _polish_answer(question, ans, llm)

    by_vendor: dict[str, dict] = {}
    for h in rfq_hits:
        name = h.get("vendor") or "?"
        d = by_vendor.setdefault(name, {"rfqs": 0, "replies": 0, "to": h.get("to") or "?", "when": "", "best_item": "", "reply_detail": ""})
        d["rfqs"] += 1
        if (h.get("when") or "") >= d["when"]:
            d["when"], d["to"], d["best_item"] = h.get("when") or "", h.get("to") or "?", h.get("item") or ""
    for h in reply_hits:
        name = h.get("vendor") or "?"
        d = by_vendor.setdefault(name, {"rfqs": 0, "replies": 0, "to": h.get("from") or "?", "when": "", "best_item": "", "reply_detail": ""})
        d["replies"] += 1
        if (h.get("when") or "") >= d["when"]:
            d["when"], d["to"], d["best_item"] = h.get("when") or "", h.get("from") or "?", h.get("item") or h.get("line") or ""
            d["reply_detail"] = _reply_detail(h)
    ranked = sorted(by_vendor.items(), key=lambda kv: (kv[1]["replies"] > 0, kv[1]["replies"], kv[1]["rfqs"], kv[1]["when"]), reverse=True)
    lines = [f"For {label!r}, these are the vendors I found in your history:"]
    for idx, (name, d) in enumerate(ranked[:8], 1):
        evidence = []
        if d["replies"]:
            evidence.append(f"{d['replies']} vendor reply fact(s)")
        if d["rfqs"]:
            evidence.append(f"{d['rfqs']} outgoing RFQ(s)")
        detail = f"; {d['reply_detail']}" if d.get("reply_detail") else ""
        lines.append(f"{idx}. {name} <{d['to']}> — {', '.join(evidence)}, last {_fmt_date(d['when'])}{detail}; example: {d['best_item']}")
    lines.append("I’d start with vendors that have reply facts first, because those show they actually responded with useful information.")
    return _polish_answer(question, "\n".join(lines), llm)


def do_add_contact(store, intent: Intent) -> str:
    import json as _json
    if "@" not in (intent.email or ""):
        return (f"To save {intent.name.title() or 'that contact'} I need an email "
                f"address — say e.g. \"add {intent.name.title() or 'Name'} name@vendor.com\".")
    intent.tag = re.sub(r"^(?:the|my|our)\s+|\s*contact\s*$", "",
                        intent.tag.strip(), flags=re.IGNORECASE).strip()
    domain = intent.email.split("@", 1)[1].lower() if "@" in intent.email else ""
    vid = domain.split(".", 1)[0] if domain else ""
    from . import vendors as vmod
    v = vmod.VENDORS.get(vid)
    payload = _json.dumps({"name": intent.name.title(), "email": intent.email.lower(),
                           "vendor_id": vid, "tag": intent.tag})
    store.add_correction("add_contact", intent.email.lower(), payload)
    if v:
        from .models import Contact
        if intent.email.lower() not in {c.email.lower() for c in v.contacts}:
            v.contacts.append(Contact(intent.name.title(), intent.email.lower()))
        where = f" at {v.name}"
    else:
        where = f" (vendor {vid!r} is not in the registry yet; saved for when it is)"
    tagnote = f" as your {intent.tag} contact" if intent.tag else ""
    return f"Saved {intent.name.title()} <{intent.email.lower()}>{tagnote}{where}."


def _item_snip(h) -> str:
    """Short 'what was quoted' tail for a reply fact, minus any [filename] tag."""
    it = (h.get("item") or h.get("line") or "").strip()
    it = re.sub(r"^\[[^\]]*\]\s*", "", it)
    return f" — {it[:48]}" if it else ""


def _mined_alternates(store, product: str, limit: int = 4) -> list[str]:
    """Vendors who have actually quoted this item -- a real-world 'who can
    supply it' signal alongside the catalog cross-references."""
    hits = store.find_replies(product, limit=20)
    by_vendor: dict[str, dict] = {}
    for h in hits:
        v = h.get("vendor") or "?"
        if v not in by_vendor:
            by_vendor[v] = h
    lines = []
    for v, h in list(by_vendor.items())[:limit]:
        price = _price_str(h) if h.get("unit_price") is not None else ""
        snip = _item_snip(h)
        when = _fmt_date(h.get("when"))
        lines.append(f"  - {v}{(' — ' + price) if price else ''}{(' on ' + when) if when else ''}{snip}")
    return lines


def _research_offer(product: str) -> str:
    return (f"\nWant me to research equivalents online? Reply "
            f"\"research {product}\" and I'll suggest manufacturer "
            f"cross-references (AI-generated — verify before ordering).")


def answer_substitute(store, product: str, *, question: str = "", llm=None) -> str:
    """Find cross-manufacturer equivalents from the catalog, plus vendors who've
    quoted the item. Offers an LLM research step when the catalog comes up short.
    """
    product = (product or "").strip().strip('"')
    if not product:
        return "Which item should I find a substitute for? (e.g. '3/4 EMT compression connector')"

    out: list[str] = [f"Substitutes for '{product}':"]

    # v0.44: tier 1 -- curated, correctable cross-reference store. Keyed by part
    # number (breaker cross-refs with equivalence type + safety caveat), so it
    # hits on a real part number and stays quiet on free-text descriptions.
    try:
        from . import cross_reference
        xref = cross_reference.resolve(product)
    except Exception:
        xref = {"found": False, "equivalents": []}
    if xref.get("found"):
        out.append("\nCurated cross-references (your confirmed/seeded data):")
        for e in xref["equivalents"][:8]:
            state = "confirmed" if e.status == "confirmed" else "suggested"
            out.append(f"  - {e.display()} [{e.equiv_type}, {state}, "
                       f"conf {e.confidence:.0%}]")
            if e.caveat:
                out.append(f"      ! {e.caveat}")

    from . import catalog_lookup
    have_catalog = catalog_lookup.available()
    candidates = []
    if have_catalog:
        res = catalog_lookup.substitutes(product, limit=8)
        candidates = res.get("candidates") or []
        anchor = res.get("anchor")
        if anchor:
            net = (f"${anchor['price_net']:g}/{anchor.get('pricing_unit') or 'ea'}"
                   if anchor.get("price_net") else "no catalog net")
            out.append(f"Matched to: {anchor['manufacturer']} {anchor['part_number']} "
                       f"— {anchor['description'][:48]} ({net})")

    if candidates:
        out.append("\nCross-manufacturer options from your catalog:")
        for c in candidates:
            net = (f"${c['price_net']:g}/{c.get('pricing_unit') or 'ea'}"
                   if c.get("price_net") else "no net price")
            tag = " (same mfr)" if c.get("same_mfr") else ""
            out.append(f"  - {c['manufacturer']} {c['part_number']} — "
                       f"{c['description'][:46]} — {net}{tag}")

    alt = _mined_alternates(store, product)
    if alt:
        out.append("\nVendors who've quoted this item:")
        out.extend(alt)

    top_match = max((c.get("match", 0.0) for c in candidates), default=0.0)
    # v0.44: a confirmed/seeded curated hit is already a solid answer, so only
    # nudge to online research when the curated store AND the catalog are thin.
    curated_hit = bool(xref.get("found"))
    if not candidates and not curated_hit:
        if have_catalog:
            out.append("\nNo close cross-references in your catalog.")
        else:
            out.append("\n(No catalog connected — set MAINBOX_CATALOG_DB to enable "
                       "catalog cross-referencing.)")
        out.append(_research_offer(product))
    elif not curated_hit and (len(candidates) < 3 or top_match < 0.7):
        # thin or only loosely-matching results (your catalog is free-text, so
        # a generic word like 'cable' can match the wrong product) -- verify and
        # offer the research path
        if top_match < 0.7:
            out.append("\n(These are loose matches — verify the type/size before using.)")
        out.append(_research_offer(product))

    return "\n".join(out)


def research_substitute(product: str, *, llm=None) -> str:
    """Research cross-references from the LIVE web, then synthesize with the LLM.

    v0.44: replaced the model-memory-only lookup with real retrieval via
    web_research.gather() (SearXNG -> fetch -> extract). The LLM now synthesizes
    grounded in actual web sources and cites them; synthesis still routes through
    the LLM tier router so every model call goes through one place. Degrades to
    model knowledge if the search backend or its deps are unavailable. Always
    flagged to-be-verified.
    """
    product = (product or "").strip().strip('"')
    if not product:
        return "What item should I research equivalents for?"
    if llm is None:
        return ("Online research needs the LLM, which isn't reachable right now "
                "(check the Ollama host). I can still scan your catalog and quotes "
                "for substitutes in the meantime.")

    # tier 1 -- real web retrieval. Lazy import so intents.py stays importable
    # without httpx/trafilatura; any failure (SearXNG down, deps missing, fetch
    # errors) drops cleanly to the model-knowledge fallback below.
    sources = []
    try:
        from .web_research import gather, build_context
        sources = gather(f"{product} cross reference equivalent replacement")
    except Exception:
        sources = []

    if sources and any(s.text for s in sources):
        context, used = build_context(sources)
        system = (
            "You are an electrical-distribution product expert helping a "
            "procurement specialist find substitute/cross-reference parts. Use "
            "ONLY the numbered web sources provided. List up to 5 likely cross-"
            "manufacturer equivalents as manufacturer + part number + a short "
            "note, citing the source number [n] for each. Electrical industry "
            "only. If the sources don't support a cross-reference, say so plainly "
            "rather than invent.")
        prompt = (f"Item: {product}\n\nWeb sources:\n\n{context}\n\n"
                  f"List supported cross-manufacturer equivalents, each with a [n] "
                  f"citation to the source it came from.")
        ans = llm.complete(prompt, system=system)
        if ans:
            srcs = "\n".join(f"  [{i}] {s.url}" for i, s in enumerate(used, 1))
            return ("Researched equivalents (from live web sources — VERIFY "
                    "before ordering):\n\n"
                    f"{ans.strip()}\n\nSources:\n{srcs}\n\n"
                    "Cross-check against your catalog and a current vendor quote "
                    "before you commit to any of these.")

    # fallback -- model knowledge only (original behavior), clearly labeled
    system = (
        "You are an electrical-distribution product expert helping a procurement "
        "specialist find substitute/cross-reference parts. Given an item, list up "
        "to 5 likely cross-manufacturer equivalents as catalog/part numbers with "
        "manufacturer and a short note. Electrical industry only (conduit, "
        "fittings, wire, strut, gear, etc.). If unsure, say so rather than invent. "
        "Be concise: one line per part, no preamble.")
    prompt = (f"Item: {product}\n"
              f"List likely cross-manufacturer equivalents (manufacturer + part "
              f"number + 3-6 word note). If you don't recognize it, say so.")
    ans = llm.complete(prompt, system=system)
    if not ans:
        return ("Couldn't reach the web search backend or the LLM just now. Your "
                "catalog/quote scan above is the reliable source; check the "
                "SearXNG and Ollama hosts and try again.")
    return ("Researched equivalents (web search unavailable — AI-generated from "
            "model knowledge, VERIFY before ordering, no live web lookup):\n\n"
            f"{ans.strip()}\n\n"
            "Cross-check against your catalog and a current vendor quote before "
            "you commit to any of these.")


def _norm_co(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_CO_SUFFIX = re.compile(
    r"(utility|electrical|electric|elec|incorporated|inc|corporation|corp|llc|"
    r"company|supply|group|enterprises|construction|contracting|contractors|"
    r"mechanical|builders|associates|industries|systems|services|co)$")


def _co_key(s: str) -> str:
    """Company 'core' for matching: normalized, with a trailing industry suffix
    removed so 'Allan Briteway Utility' and 'Allan Briteway' compare equal."""
    n = _norm_co(s)
    m = _CO_SUFFIX.search(n)
    if m and m.start() >= 3:
        return n[:m.start()]
    return n


def _resolve_customer(store, query: str) -> tuple[str, list[str]]:
    """Resolve a (possibly abbreviated/misspelled) customer name to a known
    customer COMPANY.

    Records are grouped by email domain, so every person at ej1899.com is the
    single customer 'EJ' -- typing 'EJ' finds the company, not a list of its
    employees.  Returns (resolved_name, alternatives): one company clearly
    matches -> resolved_name set; several DIFFERENT companies match ->
    alternatives lists them (by company) to ask which.
    """
    import difflib
    from .reply_miner import _company_name_from_domain
    q, qk = _norm_co(query), _co_key(query)
    if not q:
        return "", []

    # build one entry per company (keyed by domain core, or by name if no domain)
    companies: dict = {}
    idents = store.customer_identities() if hasattr(store, "customer_identities") else []
    for name, dom in idents:
        if dom:
            core = _co_key(dom.split(".")[0])
            disp = _company_name_from_domain(dom)
        else:
            core = _co_key(name)
            disp = name
        if not core:
            continue
        c = companies.setdefault(core, {"disp": disp, "domain": dom, "names": set()})
        if name:
            c["names"].add(name)
    try:                                   # registry customers (may have no domain)
        for rn in store.customers():
            core = _co_key(rn)
            if core:
                companies.setdefault(core, {"disp": rn, "domain": "", "names": set()})["names"].add(rn)
    except Exception:
        pass

    scored = []
    for core, c in companies.items():
        cands = list(c["names"]) + ([c["domain"]] if c["domain"] else []) + [core, c["disp"]]
        best = 0.0
        for cand in cands:
            nn, nk = _norm_co(cand), _co_key(cand)
            if not nn:
                continue
            if (q == nn or q in nn or nn in q or nn.startswith(q) or q.startswith(nn)
                    or (qk and nk and (qk == nk or qk in nk or nk.startswith(qk)))):
                best = 1.0
                break
            best = max(best, difflib.SequenceMatcher(None, q, nn).ratio(),
                       difflib.SequenceMatcher(None, qk, nk).ratio())
        if best >= 0.72:
            scored.append((best, core, c["disp"]))
    if not scored:
        return "", []
    scored.sort(key=lambda x: (-x[0], len(x[2])))
    if len(scored) == 1:
        return scored[0][2], []
    return "", [disp for _, _, disp in scored[:5]]


def answer_customer_orders(store, customer: str, product: str = "", *,
                           question: str = "", llm=None, clarify: str = "") -> str:
    """What a customer has ordered/requested from us (sell side), mined from
    their POs/quotes.  Lists POs with line items + sell prices."""
    if clarify:
        # "we/I" is ambiguous because a customer's name begins with that word
        return (f"Just to be sure — by \"{customer}\" do you mean the customer "
                f"\"{clarify}\", or your own (American Power) purchases?\n"
                f"  • For {clarify}'s orders: ask \"what did {clarify} order\".\n"
                f"  • For your own buying: ask \"what did we pay for <item>\".")
    customer = (customer or "").strip().strip('"')
    product = (product or "").strip().strip('"')

    # resolve a possibly-misspelled customer to a known one ("allan brightway"
    # -> "Allan Briteway"); ask if several different customers could match
    resolved, alts = _resolve_customer(store, customer) if customer else ("", [])
    if alts:
        opts = "\n".join(f"  • {a}" for a in alts)
        return (f"I have a few customers that could match \"{customer}\" — which "
                f"did you mean?\n{opts}\nAsk again with the full name.")
    note = ""
    lookup = customer
    if resolved and _norm_co(resolved) != _norm_co(customer):
        note = f" (matched \"{customer}\")"
        lookup = resolved
    elif resolved:
        lookup = resolved

    orders = store.customer_orders(customer=lookup, product=product, limit=15)
    if not orders:
        who = f" from {lookup or customer}" if (lookup or customer) else ""
        whatp = f" for '{product}'" if product else ""
        return (f"I don't have any orders{who}{whatp} on record yet. Customer "
                f"orders come from mining the sales mailbox — if this customer "
                f"ordered recently, a refresh may pick it up.")
    title = (lookup or customer) + note or "customers"
    if product:
        title += f" — '{product}'"
    out = [f"Orders from {title}:"]
    for o in orders[:8]:
        po = ""
        m = re.match(r"PO\s+(\S+)", o.get("subject", ""))
        if m:
            po = f"PO {m.group(1)}  "
        when = _fmt_date(o.get("when"))
        hdr = o.get("body_excerpt", "")
        qref = re.search(r"quote\s+(\S+)", hdr)
        tot = re.search(r"total\s+([\d,]+\.\d{2})", hdr)
        meta = []
        if qref and qref.group(1) not in ("", "total"):
            meta.append(f"quote {qref.group(1)}")
        if tot:
            meta.append(f"${tot.group(1)}")
        line = f"\n{po}{when}" + (f" ({', '.join(meta)})" if meta else "") + ":"
        out.append(line)
        facts = [f for f in o.get("facts", []) if f.get("unit_price") is not None]
        for f in facts[:6]:
            out.append(f"  • {f.get('item','')[:46]} @ ${f.get('unit_price'):g}")
        if not facts:
            for it in o.get("items", [])[:5]:
                out.append(f"  • {it[:46]}")
    return "\n".join(out)


def handle(intent: Intent, store, *, question: str = "", llm=None) -> Optional[str]:
    """Returns reply text for question intents, None for quote_request."""
    if intent.kind == INTENT_CONTACTS:
        return answer_contacts(store, intent.vendor_text, question=question, llm=llm)
    if intent.kind == INTENT_HISTORY:
        return answer_history(store, intent.product, question=question, llm=llm)
    if intent.kind == INTENT_PRICE:
        return answer_price_or_leadtime(store, intent.product, "price", question=question, llm=llm)
    if intent.kind == INTENT_LEADTIME:
        return answer_price_or_leadtime(store, intent.product, "lead time", question=question, llm=llm)
    if intent.kind == INTENT_VENDORS_FOR:
        return answer_vendors_for(store, intent.product, question=question, llm=llm)
    if intent.kind == INTENT_ADD_CONTACT:
        return do_add_contact(store, intent)
    if intent.kind == INTENT_SUBSTITUTE:
        return answer_substitute(store, intent.product, question=question, llm=llm)
    if intent.kind == INTENT_RESEARCH:
        return research_substitute(intent.product, llm=llm)
    if intent.kind == INTENT_CUSTOMER_ORDERS:
        return answer_customer_orders(store, intent.vendor_text, intent.product,
                                      question=question, llm=llm, clarify=intent.clarify)
    return None


# ---------------------------------------------------------------------------
# Conversational layer: hold a back-and-forth so the phone/voice app feels like
# a conversation -- the brain asks a clarifying question, the user answers in
# plain words, and the original request resumes.  Also tracks the last product
# and customer so follow-ups ("what's the lead time?") carry context.
# ---------------------------------------------------------------------------

# intents that answer about a specific product; an empty product -> "which?"
_PRODUCT_INTENTS = {INTENT_HISTORY, INTENT_PRICE, INTENT_LEADTIME,
                    INTENT_VENDORS_FOR, INTENT_SUBSTITUTE, INTENT_RESEARCH}
# a reply that opens with one of these is a fresh question, not an answer to a
# pending clarification
_NEW_QUERY_RE = re.compile(
    r"^(what|who|whom|how|where|when|which|why|do|does|did|is|are|was|were|can|"
    r"could|would|should|tell|show|list|find|give|look|search)\b", re.I)
_AFFIRM_RE = re.compile(r"^(y|yes|yeah|yep|yup|sure|ok|okay|do it|please|go ahead|sounds good)\b", re.I)
# a "product" that's really a pronoun/filler ("what's the lead time?") means the
# user is referring back to what we were just discussing
_PRONOUN_PRODUCT = {"it", "that", "this", "them", "those", "these", "one", "ones",
                    "the", "what", "whats", "that one", "this one", "same"}


def _is_pronoun_product(p: str) -> bool:
    pl = re.sub(r"[^a-z0-9 ]", "", (p or "").lower()).strip()
    return pl in _PRONOUN_PRODUCT or len(pl) <= 1


class InfoSession:
    """Stateful conversational front-end for the question-answering intents.

    Call answer(text) per user turn.  Returns the brain's reply, or None when
    the turn is not an info query (so a caller can fall through to the quote
    pipeline).  Holds context between turns: a pending clarification (so an
    answer like "red emt" resumes the original "what did we pay for ___"), the
    last product/customer (so "what's the lead time?" knows what about), and a
    pending research offer (so "yes" runs it).
    """

    def __init__(self, store, llm=None):
        self.store = store
        self.llm = llm
        self.last_product = ""
        self.last_customer = ""
        self.pending = None        # ("product", intent) | ("customer", alts, intent)
        self.pending_research = ""
        self.last_sources: list[dict] = []   # v0.9: emails behind the last answer
        try:
            from .knowledge import Knowledge
            self.know = Knowledge(store.db)
        except Exception:
            self.know = None

    # -- public ------------------------------------------------------------
    def answer(self, text: str) -> Optional[str]:
        sources_reset()
        out = self._answer(text)
        srcs = sources_get()
        if out is not None and srcs:
            self.last_sources = srcs
        return out

    def _answer(self, text: str) -> Optional[str]:
        raw = (text or "").strip()
        if not raw:
            return None

        # learning: did the user just teach or correct something?
        taught = self._maybe_learn(raw)
        if taught is not None:
            return taught

        # "yes" right after a research offer
        if self.pending_research and _AFFIRM_RE.match(raw):
            out = self._research(self.pending_research)
            self.pending_research = ""
            return out

        # resolve a pending clarification, unless the user clearly changed topic
        if self.pending is not None:
            resumed = self._resolve_pending(raw)
            if resumed is not None:
                return resumed
            self.pending = None     # treat as a fresh query

        it = classify(raw, self.llm, self.store)

        # follow-up with no real product but a remembered one ("what's the
        # price?", "what's the lead time?")
        if it.kind in _PRODUCT_INTENTS and self.last_product \
                and (not it.product or _is_pronoun_product(it.product)):
            it.product = self.last_product

        clar = self._needs_clarification(it, raw)
        if clar is not None:
            return clar

        # research queries reuse a cached result before re-deriving
        if it.kind == INTENT_RESEARCH and it.product:
            return self._research(it.product)

        out = handle(it, self.store, question=raw, llm=self.llm)
        if out is None:
            return None
        self._remember(it, out)
        # if we couldn't actually answer, remember the gap and offer to research
        out = self._augment_if_unanswered(it, raw, out)
        return out

    def reset(self) -> None:
        self.pending = None
        self.pending_research = ""

    # -- internals ---------------------------------------------------------
    def _needs_clarification(self, it: Intent, raw: str) -> Optional[str]:
        if it.kind in _PRODUCT_INTENTS and not it.product:
            what = ("lead time" if it.kind == INTENT_LEADTIME else
                    "price" if it.kind == INTENT_PRICE else "item")
            self.pending = ("product", it)
            return f"Which product should I check the {what} for?"
        if it.kind == INTENT_CUSTOMER_ORDERS and it.vendor_text and not it.clarify:
            resolved, alts = _resolve_customer(self.store, it.vendor_text)
            if alts:
                self.pending = ("customer", alts, it)
                opts = "\n".join(f"  • {a}" for a in alts)
                return (f"I have a few customers that could match "
                        f"\"{it.vendor_text}\" — which did you mean?\n{opts}")
        return None

    def _resolve_pending(self, raw: str) -> Optional[str]:
        kind = self.pending[0]
        # a clearly new question abandons the pending one
        if _NEW_QUERY_RE.match(raw) and len(raw.split()) >= 3:
            return None

        if kind == "product":
            _, it = self.pending
            product = raw.strip().strip('"').strip("'").strip()
            if not product:
                return None
            it.product = product
            self.pending = None
            out = handle(it, self.store, question=product, llm=self.llm)
            self._remember(it, out or "")
            return out if out is not None else f"I couldn't find anything for {product!r}."

        if kind == "customer":
            _, alts, it = self.pending
            choice = self._match_choice(raw, alts)
            if not choice:
                return None
            it.vendor_text = choice
            self.pending = None
            out = handle(it, self.store, question=raw, llm=self.llm)
            self._remember(it, out or "")
            return out
        return None

    @staticmethod
    def _match_choice(raw: str, alts: list) -> str:
        import difflib
        r = _norm_co(raw)
        if not r:
            return ""
        best, best_sc = "", 0.0
        for a in alts:
            na = _norm_co(a)
            if r in na or na in r or na.startswith(r) or _co_key(raw) == _co_key(a):
                return a
            sc = difflib.SequenceMatcher(None, r, na).ratio()
            if sc > best_sc:
                best, best_sc = a, sc
        return best if best_sc >= 0.6 else ""

    def _remember(self, it: Intent, answer: str) -> None:
        if it.kind in _PRODUCT_INTENTS and it.product:
            self.last_product = it.product
        if it.kind == INTENT_CUSTOMER_ORDERS and it.vendor_text:
            self.last_customer = it.vendor_text
        # if we just offered to research a substitute, remember it for "yes"
        self.pending_research = (it.product if it.kind == INTENT_SUBSTITUTE
                                 and "research " in (answer or "").lower() else "")

    # -- learning ----------------------------------------------------------
    def _maybe_learn(self, raw: str) -> Optional[str]:
        """Detect and apply natural-language teaching/corrections."""
        if self.know is None:
            return None
        from .knowledge import parse_teaching
        act = parse_teaching(raw)
        if not act:
            return None
        if act[0] == "alias":
            _, term, canon = act
            self.know.learn_alias(term, canon)
            # apply immediately to this session's searches
            try:
                self.store._synonyms[re.sub(r"[^a-z0-9/\-]", "", term.lower())] = \
                    re.sub(r"\s+", " ", canon.lower()).strip()
            except Exception:
                pass
            return f'Got it — I\'ll treat "{term}" as "{canon}" from now on.'
        if act[0] == "fact":
            _, topic, stmt = act
            self.know.learn_fact(topic, stmt, source="user")
            return f"Noted — I'll remember that. ({topic})"
        if act[0] == "forget":
            _, target = act
            removed = self.know.forget_fact(target) or self.know.forget_alias(target)
            return (f'Forgotten "{target}".' if removed
                    else f'I didn\'t have anything learned about "{target}".')
        return None

    def _research(self, product: str) -> str:
        """Cache research results so they're reused and consistent next time."""
        if self.know is not None:
            hit = self.know.lookup_fact("research " + product)
            if hit:
                _, answer, source, when = hit
                return f"{answer}\n\n(remembered from earlier research on {when[:10]})"
        out = research_substitute(product, llm=self.llm)
        if self.know is not None and out and "isn't reachable" not in out \
                and "What item" not in out:
            self.know.learn_fact("research " + product, out, source="llm")
        return out

    def _augment_if_unanswered(self, it: Intent, raw: str, out: str) -> str:
        """When the brain has no real answer: surface any taught fact, log the
        gap, and (for product questions) offer to research."""
        low = (out or "").lower().replace("\u2019", "'")
        unanswered = any(s in low for s in (
            "i don't have", "don't have a", "no exact match", "couldn't find",
            "i couldn't", "don't want to make up", "isn't enough"))
        if not unanswered or self.know is None:
            return out
        # a taught fact on this topic?
        topic = it.product or it.vendor_text or raw
        fact = self.know.lookup_fact(topic)
        if fact:
            _, answer, source, when = fact
            tag = "you told me" if source == "user" else f"researched {when[:10]}"
            return f"{out}\n\nFrom what I've learned ({tag}): {answer}"
        self.know.log_gap(raw, tried=it.kind)
        if it.kind in _PRODUCT_INTENTS and it.product and self.llm is not None:
            self.pending_research = it.product
            return out + f"\n\nWant me to research \"{it.product}\" online? (say yes)"
        return out
