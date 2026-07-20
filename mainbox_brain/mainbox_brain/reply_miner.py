"""
Vendor reply miner for MaINbox Brain.  (v0.12 accuracy rewrite)

The missing half of the memory loop.  The sent-history miner records who you
ASKED for pricing; this module mines vendor replies so the brain can later
answer what you actually care about:

    - what price did they quote (and per what unit)?
    - in stock?  where?
    - lead time / ETA?
    - no-bid / discontinued?
    - alternate offered?

Rewrite focus (driven by real vendor replies):
  * BLOCK ASSOCIATION -- vendors write the product on one line and the price on
    the next ("6/3 SO" / "$4/FT" / "STOCK AT THEA").  We walk the reply keeping
    a current-product context and attach price/stock/lead to the right item,
    instead of orphaning prices.
  * UNIT CAPTURE -- /ea, /ft, /M (per thousand), /C (per hundred), /MFT, barrel,
    roll, reel, drum...  A price without its unit is dangerous ($189.92 vs
    $189.92/C is 100x).
  * REAL STOCK LANGUAGE -- "stk NJ", "STOCK AT THEA", "good stock", "all stock",
    not just "in stock".  Plus stock location.
  * TRAP GUARDS -- "MIN $250", "$5000 FREIGHT ALLOWED", "valid for 5 days",
    credit balances, "$.90" with no leading zero, bare decimals like "14174/m".

Conservative by design: stores the evidence line + confidence with every fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from typing import Optional, Any

from .history_miner import strip_quoted_thread, _PUBLIC_DOMAINS
from .parser import parse_request
from .catalog import category_for_text
from .models import Category


# ---------------------------------------------------------------------------
# Data models (public API preserved)
# ---------------------------------------------------------------------------
@dataclass
class ReplyMessage:
    from_email: str
    from_display_name: str = ""
    subject: str = ""
    body: str = ""
    when: Optional[datetime] = None
    message_id: str = ""


@dataclass
class ReplyFact:
    source_line: str
    item: str = ""
    unit_price: Optional[float] = None
    unit: str = ""
    ext_price: Optional[float] = None
    lead_time: str = ""
    eta: str = ""
    availability: str = ""
    stock_location: str = ""
    status: str = "quoted"      # quoted | no_quote | alternate | info
    confidence: float = 0.0
    po_number: str = ""
    direction: str = ""         # cost (we pay a vendor) | sell (we quote a customer) | ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_line": self.source_line,
            "item": self.item,
            "unit_price": self.unit_price,
            "unit": self.unit,
            "ext_price": self.ext_price,
            "lead_time": self.lead_time,
            "eta": self.eta,
            "availability": self.availability,
            "stock_location": self.stock_location,
            "status": self.status,
            "confidence": round(float(self.confidence), 3),
            "po_number": self.po_number,
            "direction": self.direction,
        }


@dataclass
class VendorReplyRecord:
    source_key: str
    vendor_id: str
    vendor_name: str
    from_email: str
    from_name: str = ""
    subject: str = ""
    when: Optional[datetime] = None
    body_excerpt: str = ""
    items: list[str] = field(default_factory=list)
    facts: list[ReplyFact] = field(default_factory=list)
    quote_status: str = "info"
    confidence: float = 0.0
    counterparty_type: str = "vendor"   # vendor | customer | internal | unknown

    def facts_json(self) -> str:
        return json.dumps([f.as_dict() for f in self.facts], ensure_ascii=False)


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------
# American Power purchase-order numbers: always start with P0000 (e.g.
# P000020235). Appears in vendor confirmations/invoices and email bodies.
_PO_NUMBER = re.compile(r"\bP0000\d{2,}\b", re.IGNORECASE)
# also catch a labeled PO whose number format varies slightly
_PO_LABELED = re.compile(
    r"(?:p\.?\s*o\.?\s*(?:number|num|no|#)?|purchase\s+order)\s*[:#]?\s*"
    r"(P0\d{5,})", re.IGNORECASE)


def _po_number(text: str) -> str:
    """First American Power PO number found in the text, normalized upper-case."""
    m = _PO_NUMBER.search(text)
    if m:
        return m.group(0).upper()
    m = _PO_LABELED.search(text)
    if m:
        return m.group(1).upper()
    return ""


_UNITS = (r"ft|lf|foot|feet|ea|each|pc|pcs|piece|pieces|m|mft|c|cwt|lb|lbs|"
          r"roll|rolls|reel|reels|drum|drums|barrel|barrels|bbl|box|boxes|"
          r"spool|spools|set|sets|sheet|sheets|k")
_UNIT_NORM = {
    "each": "ea", "pc": "ea", "pcs": "ea", "piece": "ea", "pieces": "ea",
    "lf": "ft", "foot": "ft", "feet": "ft", "lbs": "lb", "bbl": "barrel",
    "rolls": "roll", "reels": "reel", "drums": "drum", "barrels": "barrel",
    "boxes": "box", "spools": "spool", "sets": "set", "sheets": "sheet",
}

# $.90, $1,469, $189.92  (allows missing leading zero)
_MONEY = re.compile(r"\$\s*(\.?\d[\d,]*(?:\.\d{1,5})?)")
# bare decimal carrying a per-unit, no $:  "14174.00/m", "543/M", "189.92/c"
_BARE_PRICED = re.compile(
    r"(?<![\w.$])(\d[\d,]*(?:\.\d{1,5})?)\s*(?:/\s*|per\s+)(" + _UNITS + r")\b",
    re.IGNORECASE)
_UNIT_AFTER = re.compile(r"(?:/\s*|per\s+)(" + _UNITS + r")\b", re.IGNORECASE)
# "W5053S6 @ 1.88 each" / "@ 4.10 EACH" -- price after @, common from reps
_AT_PRICED = re.compile(r"@\s*(\d[\d,]*(?:\.\d{1,5})?)\s*(" + _UNITS + r")?\b", re.IGNORECASE)
# bare decimal followed by a unit word: "4.75 each", "1.30 EA" (decimal required
# so quantities like "626pc" are not mistaken for prices)
_WORD_PRICED = re.compile(r"(?<![\w.$@])(\d[\d,]*\.\d{1,5})\s+(" + _UNITS + r")\b", re.IGNORECASE)
# a bare unit word immediately following a price: "$62.00 each", "$4 ft"
_UNIT_WORD = re.compile(r"\s*(ea|each|ft|lf|pcs?|pc|roll|reel|drum|barrel|box|spool|m|c)\b", re.IGNORECASE)
# "$828.01 / 100 EA" = per hundred; "/ 1000" = per thousand
_PER_HUNDRED = re.compile(r"/\s*(100|1000|1,000|10000|10,000)\s*(?:ft|lf|ea|each|pc|pcs)?\b", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
_CURRENCY = re.compile(r"\b(?:usd|cad|eur|gbp|us5?d)\b", re.IGNORECASE)
# a number followed by a street suffix is an address, not a price ("@ 84 ST")
_STREET_AFTER = re.compile(
    r"^\s*(?:st|street|ave|avenue|av|blvd|boulevard|rd|road|dr|drive|fl|floor|"
    r"ste|suite|apt|pl|place|way|ct|court|hwy|highway|ln|lane|terr|pkwy)\b",
    re.IGNORECASE)
_ADDR_LINE = re.compile(
    r"\b\d{1,6}\s+\d{0,4}\s*[A-Za-z][\w ]*\b(?:st|street|ave|avenue|blvd|road|rd|"
    r"drive|dr|lane|ln|suite|ste|floor|fl)\b", re.IGNORECASE)
# "Material No.: 266TZ", "Customer Part No.: 27817", "Catalog #: X"
_LABELED_PART = re.compile(r"\b(?:material|catalog|cat|mfr|mfg|part|model|stock)\s*"
    r"(?:no|number|num|#)\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,})", re.IGNORECASE)
_EXT = re.compile(
    r"\b(?:ext(?:ended)?|total|subtotal)\b\s*(?:price)?\s*[:=]?\s*\$?\s*"
    r"(\.?\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)
# words that mean a $ is NOT the item price
_MINORDER = re.compile(
    r"\b(min(?:imum)?|freight|adder|setup|set-?up|restock(?:ing)?|credit|"
    r"leaving|towards?|valid|subject\s+to|deposit|prepay|handling|weight|pounds|lbs?\b|subtotal|sub-total|grand\s+total|\btotal\b|net\s+wt|gross)\b", re.IGNORECASE)

_NO_QUOTE = re.compile(
    r"\b(no\s*quote|no\s*bid|cannot\s+quote|can'?t\s+quote|can\s*not\s+quote|"
    r"unable\s+to\s+quote|do\s+not\s+quote|not\s+quoting|pass\s+on\s+this|"
    r"we\s+(?:will\s+)?pass\b|decline|discontinued|obsolete|"
    r"no\s+longer\s+(?:available|made|manufactured)|end\s+of\s+life|eol)\b",
    re.IGNORECASE)
_ALTERNATE = re.compile(
    r"\b(alternate|alternative|substitute|substitution|equivalent|equal|"
    r"in\s+lieu|instead\s+of|cross(?:\s|-)?reference|cross\b|or\s+equal)\b",
    re.IGNORECASE)

_OUT_STOCK = re.compile(
    r"\b(no\s+stock|out\s+of\s+stock|not\s+in\s+stock|0\s+(?:in\s+)?stock|"
    r"back\s*order(?:ed)?|backorder|made\s+to\s+order|nothing\s+in\s+stock|oos)\b",
    re.IGNORECASE)
_FACTORY_STOCK = re.compile(
    r"\b(factory\s+stock|stock\s+at\s+(?:the\s+)?factory|factory\s+has|"
    r"mfr\s+stock|mill\s+ship)\b", re.IGNORECASE)
_IN_STOCK = re.compile(
    r"\b((?:good|limited|ample|full|some|partial|plenty|decent)\s+(?:of\s+)?stock|"
    r"in\s+stock|on\s+hand|have\s+(?:it|them|some|stock)|stock\s+available|"
    r"available\s+(?:now|from\s+stock)|all\s+stock|\bstk\b|\bstock\b|"
    r"\d+\s+(?:in\s+)?stock)\b", re.IGNORECASE)
_STOCK_LOC = re.compile(
    r"\b(?:stock|stk)\s+(?:at\s+|in\s+|@\s*)?([A-Z]{2}\b|[A-Z][a-zA-Z]{2,})",
    re.IGNORECASE)
_LOC_STOP = {"here", "now", "available", "them", "this", "our", "good", "all",
             "soon", "today", "some", "yes", "limited", "from", "and", "the",
             "subject", "prior", "sale", "stock"}

# validity must not be read as lead time
_VALID = re.compile(r"\b(?:valid|good)\s+(?:for|till|until|thru|through)\b[^.\n]*",
                    re.IGNORECASE)
_ETA_DATE = re.compile(
    r"\b(?:eta|ships?|ship\s*date|delivery|lead\s*time\s+is|avail(?:able)?)\s*"
    r"(?:is|:|=|-|on|by)?\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", re.IGNORECASE)
_LEAD_RANGE = re.compile(
    r"\b(\d{1,2}\s*(?:-|to|–|—)\s*\d{1,2}\s*(?:business\s+)?"
    r"(?:days?|weeks?|wks?|mos?|months?))\b", re.IGNORECASE)
_LEAD_SINGLE = re.compile(
    r"\b(\d{1,3}\s*(?:business\s+)?(?:days?|weeks?|wks?|mos?|months?))\b",
    re.IGNORECASE)
_LEAD_FUZZY = re.compile(
    r"\b((?:a\s+)?few\s+(?:days?|weeks?|months?)|couple\s+(?:of\s+)?"
    r"(?:days?|weeks?|months?)|next\s+(?:day|week)|same\s+day|asap)\b", re.IGNORECASE)
_PRICE_CUE = re.compile(
    r"\$|\bnet\b|\bunit\s+price\b|\bprice[ds]?\b|\bquote[ds]?\b|"
    r"(?:/\s*|per\s+)(?:" + _UNITS + r")\b|\bcost\b", re.IGNORECASE)

# email scaffolding / signature noise to strip before reading a line as product
_SCAFFOLD = re.compile(
    r"^\s*(?:to|from|cc|bcc|sent|subject|date|importance)\s*:.*$"
    r"|\b\d{1,2}:\d{2}\s*(?:am|pm)?\b"
    r"|\b(?:total|subtotal|tax|grand\s+total|amount\s+due|po\s*#|bid\s+s?\d|"
    r"quote\s*#?\s*\w*\d|invoice|order\s*#)\b.*"
    r"|https?://\S+|www\.\S+|mailto:\S+"
    r"|\b(?:thanks?|thank\s+you|regards|best\s+regards|sincerely|cheers|"
    r"sent\s+from|inside\s+sales|sales\s+manager|account\s+manager)\b.*",
    re.IGNORECASE)
_LEAD_QTY = re.compile(
    r"^\s*\d[\d,]*\s*(?:k|ft|lf|ea|each|pcs?|pc|m|c|rolls?|reels?|drums?|"
    r"boxes?|spools?|'|\u201d|\")?\s*[-\u2013\u2014]\s+"
    r"|^\s*\d[\d,]*\s+(?:k|ft|lf|ea|each|pcs?|rolls?|reels?|drums?|boxes?|"
    r"spools?)\b\s*[-\u2013\u2014:]?\s*", re.IGNORECASE)

# product identity: a part number, a wire spec, or a known category keyword
_PARTNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/.]{2,}")
_SPEC = re.compile(r"\d{1,2}\s*/\s*\d{1,2}(?:\s*/\s*\d{1,2})*|\b\d{1,2}-\d[a-zA-Z]?c?\b")


def _domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def _vendor_id_from_domain(domain: str) -> str:
    return domain.split(".", 1)[0].lower()


def _company_name_from_domain(domain: str) -> str:
    return _vendor_id_from_domain(domain).replace("-", " ").replace("_", " ").title()


def _clean_number(value: str) -> float | None:
    try:
        v = value.strip()
        if v.startswith("."):
            v = "0" + v
        return float(v.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _source_key(msg: ReplyMessage) -> str:
    if msg.message_id:
        return msg.message_id.strip()
    raw = "|".join([
        (msg.from_email or "").lower(),
        msg.when.isoformat(timespec="seconds") if msg.when else "",
        msg.subject or "",
        (msg.body or "")[:500],
    ])
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def _normalize_line(line: str) -> str:
    line = re.sub(r"[\t\r]+", " ", line or "")
    line = re.sub(r"\s{2,}", " ", line)
    return line.strip(" -\u2013\u2014\u2022\t")


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _is_partnum(tok: str) -> bool:
    if _PHONE.fullmatch(tok):
        return False
    core = tok.replace("-", "").replace("/", "").replace(".", "")
    if len(core) < 4 or not core.isalnum():
        return False
    if core.isdigit():
        return 6 <= len(core) != 10        # 10-digit run is almost always a phone
    return any(c.isdigit() for c in core) and any(c.isalpha() for c in core)


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------
def _addr_num(line: str, end: int) -> bool:
    """True if the number ending at `end` is followed by a street suffix
    (e.g. '@ 84 ST' is an address, not '@ $84')."""
    return bool(_STREET_AFTER.match(line[end:end + 12]))


def _price(line: str) -> tuple[float | None, str, float | None]:
    ext = None
    mext = _EXT.search(line)
    if mext:
        ext = _clean_number(mext.group(1))

    best = None       # (value, unit)
    # 1) $-amounts, skipping min-order/freight/credit context, preferring /unit
    for m in _MONEY.finditer(line):
        lo = max(0, m.start() - 16)
        ctx = line[lo:m.end() + 18]
        if _MINORDER.search(ctx):
            continue
        if ext is not None and _clean_number(m.group(1)) == ext:
            continue
        if _addr_num(line, m.end()):
            continue
        val = _clean_number(m.group(1))
        if val is None or val <= 0:
            continue
        tail = line[m.end():m.end() + 14]
        ph = _PER_HUNDRED.match(tail.strip()) or _PER_HUNDRED.search(tail)
        if ph:
            unit = "m" if ph.group(1).replace(",", "") in ("1000", "10000") else "c"
        else:
            um = _UNIT_AFTER.search(tail[:6]) or _UNIT_WORD.match(tail.lstrip())
            unit = (um.group(1).lower() if um else "")
        cand = (val, unit)
        # prefer the first candidate that carries a unit
        if best is None:
            best = cand
        elif not best[1] and unit:
            best = cand
    # 2) "@ 1.88 each" style
    if best is None:
        for m in _AT_PRICED.finditer(line):
            lo = max(0, m.start() - 16)
            if _MINORDER.search(line[lo:m.end() + 8]) or _addr_num(line, m.end()):
                continue
            val = _clean_number(m.group(1))
            if val is not None and val > 0:
                best = (val, (m.group(2) or "").lower())
                break
    # 3) bare decimal with a per-unit (no $), e.g. "14174.00/m"
    if best is None:
        for m in _BARE_PRICED.finditer(line):
            lo = max(0, m.start() - 16)
            if _MINORDER.search(line[lo:m.end() + 4]) or _addr_num(line, m.end()):
                continue
            val = _clean_number(m.group(1))
            if val is not None and val > 0:
                best = (val, m.group(2).lower())
                break
    # 4) bare decimal followed by a unit word, e.g. "4.75 each"
    if best is None:
        for m in _WORD_PRICED.finditer(line):
            lo = max(0, m.start() - 16)
            if _MINORDER.search(line[lo:m.end() + 4]) or _addr_num(line, m.end()):
                continue
            val = _clean_number(m.group(1))
            if val is not None and val > 0:
                best = (val, m.group(2).lower())
                break

    if best is None:
        return None, "", ext
    value, unit = best
    if not unit:
        u = _UNIT_AFTER.search(line)
        if u:
            unit = u.group(1).lower()
    unit = _UNIT_NORM.get(unit, unit)
    return value, unit, ext


_BACKORDER_INSTRUCTION = re.compile(
    r"\b(?:nothing|none|no|not|never|do\s+not|don'?t|cannot|can'?t|please|plz|will)\b"
    r"[^.\n]*\bback\s*order", re.IGNORECASE)
# stock terms in quotes are referenced as a concept, not asserted as status
# (e.g. PO boilerplate: email "no stock" quantities)
_QUOTED_STOCK = re.compile(
    r"[\"'\u201c\u201d\u2018\u2019]\s*(?:no\s*stock|out\s*of\s*stock|n/?s|"
    r"back\s*order\w*|made\s+to\s+order)\s*[\"'\u201c\u201d\u2018\u2019]",
    re.IGNORECASE)


def _availability(line: str) -> tuple[str, str]:
    # instructions/quoted terms are not stock statements
    chk = _QUOTED_STOCK.sub(" ", line)
    chk = _BACKORDER_INSTRUCTION.sub(" ", chk)
    status = ""
    if _OUT_STOCK.search(chk):
        status = "out_of_stock"
    elif _FACTORY_STOCK.search(chk):
        status = "factory_stock"
    elif _IN_STOCK.search(chk):
        status = "in_stock"
    loc = ""
    if status in ("in_stock", "factory_stock"):
        ml = _STOCK_LOC.search(chk)
        if ml:
            cand = ml.group(1).strip()
            if cand.lower() not in _LOC_STOP:
                loc = cand if cand.isupper() else cand.title()
    return status, loc


def _lead_time(line: str) -> tuple[str, str]:
    s = _VALID.sub(" ", line)          # drop "valid for 5 days" so it isn't lead
    eta = ""
    me = _ETA_DATE.search(s)
    if me:
        eta = me.group(1).strip()
    lead = ""
    for pat in (_LEAD_RANGE, _LEAD_SINGLE, _LEAD_FUZZY):
        m = pat.search(s)
        if m:
            cand = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;-\u2013\u2014")
            if cand and not re.fullmatch(r"\$?[0-9,.]+", cand):
                lead = cand
                break
    return lead, eta


def _strip_nonproduct(line: str) -> str:
    s = _SCAFFOLD.sub(" ", line)
    s = _ADDR_LINE.sub(" ", s)
    s = _PHONE.sub(" ", s)
    s = _CURRENCY.sub(" ", s)
    s = _VALID.sub(" ", s)
    s = _MONEY.sub(" ", s)
    s = _PER_HUNDRED.sub(" ", s)
    s = _AT_PRICED.sub(" ", s)
    s = _BARE_PRICED.sub(" ", s)
    s = _WORD_PRICED.sub(" ", s)
    s = _UNIT_AFTER.sub(" ", s)
    s = _STOCK_LOC.sub(" ", s)
    for pat in (_OUT_STOCK, _FACTORY_STOCK, _IN_STOCK, _ETA_DATE,
                _LEAD_RANGE, _LEAD_SINGLE, _LEAD_FUZZY, _NO_QUOTE):
        s = pat.sub(" ", s)
    s = re.sub(r"\b(?:p\s*&\s*a|p/a|price\s+and\s+availability|pricing|quote)\b",
               " ", s, flags=re.IGNORECASE)
    s = _LEAD_QTY.sub(" ", s)
    s = re.sub(r"\b(?:ea|each|pcs?|lf|rolls?|reels?|drums?|barrels?|boxes?|spools?)\b",
               " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:approx(?:imately)?|aro|est|estimated|lead\s*time|ship\s*date|"
               r"delivery|eta|in\s+stk|stk)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:re|fw|fwd)\s*:\s*", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[\u201c\u201d\u2018\u2019]", '"', s)
    s = re.sub(r"\s*\|\s*", " ", s)            # flatten leftover table cell separators
    s = re.sub(r"\s{2,}", " ", s).strip(" -\u2013\u2014:;,.\t\"|")
    return s


def _has_identity(residue: str) -> bool:
    if not residue:
        return False
    if _SPEC.search(residue):
        return True
    if category_for_text(residue) != Category.UNKNOWN:
        return True
    return any(_is_partnum(t) for t in _PARTNUM.findall(residue))


def _has_substance(residue: str) -> bool:
    """Enough product-ish text to serve as an item label on a price line."""
    return bool(residue) and len(residue) >= 3 and any(c.isalpha() for c in residue)


def _subject_item(subject: str) -> str:
    res = _strip_nonproduct(_normalize_line(subject or ""))
    return res if _has_substance(res) else ""


# ---------------------------------------------------------------------------
# Block-aware fact extraction
# ---------------------------------------------------------------------------
class _Acc:
    __slots__ = ("item", "lines", "price", "unit", "ext", "lead", "eta",
                 "avail", "loc", "no_quote", "alternate")

    def __init__(self, item: str) -> None:
        self.item = item
        self.lines: list[str] = []
        self.price: float | None = None
        self.unit = ""
        self.ext: float | None = None
        self.lead = ""
        self.eta = ""
        self.avail = ""
        self.loc = ""
        self.no_quote = False
        self.alternate = False

    def has_payload(self) -> bool:
        return (self.price is not None or bool(self.lead) or bool(self.eta)
                or bool(self.avail) or self.no_quote or self.alternate)

    def to_fact(self) -> ReplyFact:
        if self.no_quote:
            status = "no_quote"
        elif self.price is not None:
            status = "quoted"
        elif self.alternate:
            status = "alternate"
        else:
            status = "info"
        line = " / ".join(dict.fromkeys(self.lines))[:200]
        conf = 0.2
        if self.price is not None:
            conf += 0.30
        if self.unit:
            conf += 0.15
        if self.lead or self.eta:
            conf += 0.15
        if self.avail:
            conf += 0.10
        if self.item and _has_identity(self.item):
            conf += 0.15
        if self.no_quote or self.alternate:
            conf += 0.20
        return ReplyFact(
            source_line=line, item=self.item.strip()[:120],
            unit_price=self.price, unit=self.unit, ext_price=self.ext,
            lead_time=self.lead, eta=self.eta, availability=self.avail,
            stock_location=self.loc, status=status, confidence=min(conf, 0.98))


def _extract_facts(text: str, subject_item: str = "") -> list[ReplyFact]:
    facts: list[ReplyFact] = []
    context_item = subject_item
    acc: _Acc | None = None

    def flush() -> None:
        nonlocal acc
        if acc is not None and acc.has_payload():
            facts.append(acc.to_fact())
        acc = None

    for raw in re.split(r"[\n\r]+", text or ""):
        line = _normalize_line(raw)
        if not line or len(line) > 240:
            continue

        price, unit, ext = _price(line)
        lead, eta = _lead_time(line)
        avail, loc = _availability(line)
        nq = bool(_NO_QUOTE.search(line))
        alt = bool(_ALTERNATE.search(line)) and price is None
        has_fact = (price is not None or bool(lead) or bool(eta)
                    or bool(avail) or nq or alt)

        # labeled part-number line ("Material No.: 266TZ", "Customer Part No.: X"):
        # enrich the CURRENT item rather than splitting the block
        if not has_fact:
            mlab = _LABELED_PART.search(line)
            if mlab:
                pn = mlab.group(1).strip()
                if acc is not None and acc.item:
                    if pn.lower() not in acc.item.lower():
                        acc.item = f"{acc.item} {pn}".strip()
                else:
                    context_item = pn
                    acc = _Acc(pn)
                continue

        residue = _strip_nonproduct(line)
        # the product label this line carries (inline rows), if any
        line_item = residue if _has_substance(residue) else ""
        # does this line introduce a NEW product context?
        is_header = bool(line_item) and _has_identity(residue)
        # a PRICED line with its own description is a self-contained line item,
        # even without a recognizable part number ("2\" WATERTIGHT HUB $828/100EA")
        inline_priced = price is not None and bool(line_item)

        if is_header and not inline_priced and (
                acc is None or _norm_key(residue) != _norm_key(context_item)):
            flush()
            context_item = residue

        if has_fact:
            if inline_priced:
                flush()
                acc = _Acc(line_item)
            elif acc is None or (is_header and _norm_key(residue) != _norm_key(acc.item)):
                flush()
                acc = _Acc(line_item or context_item)
            if line_item and not acc.item:
                acc.item = line_item
            acc.lines.append(line)
            if price is not None and acc.price is None:
                acc.price, acc.unit = price, unit or acc.unit
            if ext is not None and acc.ext is None:
                acc.ext = ext
            if lead and not acc.lead:
                acc.lead = lead
            if eta and not acc.eta:
                acc.eta = eta
            if avail and not acc.avail:
                acc.avail, acc.loc = avail, loc or acc.loc
            acc.no_quote = acc.no_quote or nq
            acc.alternate = acc.alternate or alt
        elif is_header:
            # product-only header: hold context; open an acc so a following
            # bare no-quote/price line attaches to it
            flush()
            acc = _Acc(residue)

    flush()
    return facts


# ---------------------------------------------------------------------------
# Item list (kept for search hay / display), now cleaner
# ---------------------------------------------------------------------------
def _items_from_text(subject: str, body: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    sub = _subject_item(subject)
    if sub:
        seen.add(sub.lower())
        out.append(sub)
    for raw in re.split(r"[\n\r]+", body or ""):
        line = _normalize_line(raw)
        if not line:
            continue
        res = _strip_nonproduct(line)
        if not _has_substance(res) or not _has_identity(res):
            continue
        low = res.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(res[:120])
    return out[:25]


# ---------------------------------------------------------------------------
# Vendor resolution (unchanged behavior)
# ---------------------------------------------------------------------------
def _vendor_name_pair(store, vid: str):
    try:
        row = store.db.execute("SELECT vendor_id, name FROM vendors WHERE vendor_id=? LIMIT 1",
                               (vid,)).fetchone()
        if row:
            return row[0], row[1]
    except Exception:
        pass
    return (vid, vid.title())


def _alias_vendor(store, from_email: str, from_name: str = ""):
    """Resolve a sender to a vendor via user-registered aliases.

    A dotted, space-free alias (e.g. bizzaro.com) matches the email domain;
    any other alias (e.g. Bizzaro, B&B, Brindisi) matches the sender's
    display name.
    """
    try:
        aliases = store.aliases()
    except Exception:
        return None
    if not aliases:
        return None
    email = (from_email or "").lower()
    domain = _domain(email)
    name = (from_name or "").lower()
    name_words = set(re.split(r"\W+", name))
    for alias, vid in aliases.items():
        if not alias or not vid:
            continue
        if "." in alias and " " not in alias:
            if domain and (domain == alias or email.endswith("@" + alias)):
                return _vendor_name_pair(store, vid)
        elif len(alias) >= 3:
            if alias in name:
                return _vendor_name_pair(store, vid)
        elif alias in name_words:        # short alias -> whole-word match
            return _vendor_name_pair(store, vid)
    return None


def _known_vendor_lookup(store, from_email: str, from_name: str = "") -> tuple[str, str] | None:
    if store is None:
        return None
    email = (from_email or "").strip().lower()
    domain = _domain(email)
    if not email or not domain:
        return _alias_vendor(store, from_email, from_name)
    try:
        row = store.db.execute(
            "SELECT v.vendor_id, v.name FROM contacts c JOIN vendors v ON v.vendor_id=c.vendor_id "
            "WHERE lower(c.email)=? LIMIT 1", (email,)).fetchone()
        if row:
            return row[0], row[1]
        row = store.db.execute(
            "SELECT v.vendor_id, v.name FROM contacts c JOIN vendors v ON v.vendor_id=c.vendor_id "
            "WHERE lower(substr(c.email, instr(c.email,'@')+1))=? LIMIT 1", (domain,)).fetchone()
        if row and domain not in _PUBLIC_DOMAINS:
            return row[0], row[1]
        row = store.db.execute(
            "SELECT v.vendor_id, v.name FROM records r JOIN vendors v ON v.vendor_id=r.vendor_id "
            "WHERE lower(r.to_email)=? LIMIT 1", (email,)).fetchone()
        if row:
            return row[0], row[1]
        row = store.db.execute(
            "SELECT v.vendor_id, v.name FROM records r JOIN vendors v ON v.vendor_id=r.vendor_id "
            "WHERE lower(substr(r.to_email, instr(r.to_email,'@')+1))=? LIMIT 1", (domain,)).fetchone()
        if row and domain not in _PUBLIC_DOMAINS:
            return row[0], row[1]
    except Exception:
        return None
    return _alias_vendor(store, from_email, from_name)


def is_known_vendor_sender(store, from_email: str, from_name: str = "") -> bool:
    return _known_vendor_lookup(store, from_email, from_name) is not None


def _own_domain() -> str:
    try:
        from . import config
        if "@" in config.SENDER_EMAIL:
            return _domain(config.SENDER_EMAIL)
    except Exception:
        pass
    return ""


def _norm_cust(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _customer_match(store, from_email: str, from_name: str = "") -> str:
    """Display name if the sender matches the customer registry, else ''.
    Separator-insensitive: registry 'Bender Electric' matches benderelectric.com.
    Domain match is a prefix test (so 'tore' won't match 'store.com'); name match
    is substring on the normalized display name.
    """
    if store is None:
        return ""
    try:
        names = store.customers()
    except Exception:
        return ""
    dom_norm = _norm_cust(_domain((from_email or "").lower()))
    name_norm = _norm_cust(from_name)
    for disp in names:
        k = _norm_cust(disp)
        if len(k) < 4 or (disp or "").strip().startswith("--"):
            continue
        if (dom_norm and dom_norm.startswith(k)) or (name_norm and name_norm.startswith(k)):
            return disp
    return ""


def classify_counterparty(store, from_email: str, from_name: str = "",
                          unknown_default: str = "unknown") -> str:
    """Who is this sender to us? vendor (we buy from them) | customer (we sell
    to them) | internal (our own domain) | unknown. Drives cost-vs-sell tagging.

    Vendor registry wins (we have far more vendor signal), then our own domain,
    then the customer registry. unknown_default lets the SALES-mailbox pass treat
    otherwise-unrecognized senders as customers (that inbox's non-vendor,
    non-internal mail is overwhelmingly contractors sending RFQs/POs), while the
    vendor mailbox leaves them 'unknown'.
    """
    domain = _domain((from_email or "").lower())
    if domain and domain == _own_domain():
        return "internal"
    if store is not None and is_known_vendor_sender(store, from_email, from_name):
        return "vendor"
    if _customer_match(store, from_email, from_name):
        return "customer"
    return unknown_default


_DIRECTION_BY_TYPE = {"vendor": "cost", "customer": "sell", "internal": "", "unknown": ""}


def _vendor_for_sender(store, from_email: str, from_name: str = "") -> tuple[str, str]:
    email = (from_email or "").strip().lower()
    domain = _domain(email)
    known = _known_vendor_lookup(store, email, from_name)
    if known:
        return known
    if not domain:
        return email, from_name or email
    if domain in _PUBLIC_DOMAINS:
        return email, from_name or email
    return _vendor_id_from_domain(domain), from_name or _company_name_from_domain(domain)


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------
def parse_vendor_reply(msg: ReplyMessage, store=None, vendor_only: bool = True,
                       customer_default: bool = False) -> VendorReplyRecord | None:
    email = (msg.from_email or "").strip().lower()
    if not email or "@" not in email:
        return None
    domain = _domain(email)
    own = set()
    try:
        from . import config
        if "@" in config.SENDER_EMAIL:
            own.add(_domain(config.SENDER_EMAIL))
    except Exception:
        pass
    if domain in own:
        return None
    unk = "customer" if customer_default else "unknown"
    ctype = (classify_counterparty(store, email, msg.from_display_name, unknown_default=unk)
             if store is not None else "vendor")
    if ctype == "internal":
        return None                      # our own outbound isn't a reply record
    if vendor_only and ctype != "vendor":
        return None                      # vendor pass skips customer/unknown

    own_words = strip_quoted_thread(msg.body or "")
    clean_body = re.sub(r"\s+", " ", own_words).strip()
    subj_item = _subject_item(msg.subject or "")
    items = _items_from_text(msg.subject or "", own_words)
    facts = _extract_facts(own_words, subject_item=subj_item)

    # PO number applies to the whole message; subject often carries it too
    po = _po_number(f"{msg.subject or ''} {own_words}")
    if po:
        for f in facts:
            if not f.po_number:
                f.po_number = po

    if not facts and _NO_QUOTE.search(own_words):
        m = _NO_QUOTE.search(own_words)
        facts.append(ReplyFact(source_line=_normalize_line(m.group(0)),
                               item=subj_item, status="no_quote", confidence=0.6))

    if not facts and not items:
        return None

    vendor_id, vendor_name = (_vendor_for_sender(store, email, msg.from_display_name)
                              if store else (email, msg.from_display_name or email))
    # for a customer sender, prefer the registered customer display name
    if ctype == "customer" and store is not None:
        cust = _customer_match(store, email, msg.from_display_name)
        if cust:
            vendor_name = cust
    # tag every fact with price direction (cost from a vendor, sell to a customer)
    direction = _DIRECTION_BY_TYPE.get(ctype, "")
    for f in facts:
        if not f.direction:
            f.direction = direction
    statuses = [f.status for f in facts]
    if "quoted" in statuses:
        quote_status = "quoted"
    elif "alternate" in statuses:
        quote_status = "alternate"
    elif "no_quote" in statuses:
        quote_status = "no_quote"
    else:
        quote_status = "info"
    conf = max([f.confidence for f in facts] or [0.2 if items else 0.0])
    return VendorReplyRecord(
        source_key=_source_key(msg), vendor_id=vendor_id, vendor_name=vendor_name,
        from_email=email, from_name=msg.from_display_name or "",
        subject=msg.subject or "", when=msg.when, body_excerpt=clean_body[:600],
        items=items, facts=facts, quote_status=quote_status, confidence=conf,
        counterparty_type=ctype)


def mine_replies(messages: list[ReplyMessage], store=None, vendor_only: bool = True,
                 customer_default: bool = False) -> list[VendorReplyRecord]:
    out: list[VendorReplyRecord] = []
    seen: set[str] = set()
    for msg in messages:
        rec = parse_vendor_reply(msg, store=store, vendor_only=vendor_only,
                                 customer_default=customer_default)
        if rec is None or rec.source_key in seen:
            continue
        seen.add(rec.source_key)
        out.append(rec)
    out.sort(key=lambda r: r.when or datetime.min, reverse=True)
    return out
