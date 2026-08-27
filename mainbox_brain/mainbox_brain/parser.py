"""
Parser -- natural language quote request -> structured LineItems.

Regex-first so it works with no LLM. If an LLM is supplied it's used to crack
messy multi-item requests into clean JSON, which we then validate; on any
doubt we fall back to the regex pass. Same degradation contract as everything
else.

Examples it handles today:
    "Can I get price and availability for 10,000ft of 12/2 MC?"
    "500ft of 3/4 EMT and (2) 200A panels"
    "need 1000 ft 12 awg thhn, 50 1/2in emt connectors"
"""
from __future__ import annotations
import re
from typing import Optional

from .models import QuoteRequest, LineItem, Category
from . import catalog
from .llm import LLMClient, extract_json

# qty + optional unit, e.g. "10,000ft", "500 ft", "1000ft", "50"
_QTY_UNIT = re.compile(
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit>ft|feet|foot|'|m|meters?|ea|each|pcs?|pieces?|rolls?|reels?|boxes?|box)?\b",
    re.IGNORECASE,
)
# leading "(2)" style quantities
_PAREN_QTY = re.compile(r"^\(?\s*(?P<qty>\d+)\s*\)?\s+(?P<rest>[a-zA-Z].*)$")
# conductor/gauge spec like "12/2", and bare gauge like "12 awg"
_AWG_CONDUCTORS = re.compile(r"\b(?P<awg>\d{1,2})\s*/\s*(?P<cond>\d)\b")
_AWG = re.compile(r"\b(?P<awg>\d{1,4}|\d/0|\d{3,4}\s*kcmil)\s*(?:awg|ga\b|gauge)", re.IGNORECASE)
_AMPS = re.compile(r"\b(?P<amps>\d{2,4})\s*a(?:mp|mps)?\b", re.IGNORECASE)

_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\bplus\b|\n)\s*", re.IGNORECASE)

# Collapse thousands separators *inside* numbers so "10,000ft" isn't split on
# the comma and isn't parsed as "10".
_NUM_COMMA = re.compile(r"(?<=\d),(?=\d)")

# Strip a leading request clause as a whole BEFORE splitting, so the "and" in
# "price and availability for" doesn't get treated as an item separator.
_BOILERPLATE = re.compile(
    r"^\s*(?:hi|hey|hello)?[,\s]*"
    r"(?:(?:can i get|could i get|could you (?:get|send|quote)|i need|we need|need|"
    r"please|pls|get me|get|got|want|quote me|quote|send me|i'?m looking for|looking for)\s+){0,3}"
    r"(?:price\s*(?:&|and)\s*availability|availability\s*(?:&|and)\s*price|"
    r"pricing|price|availability|a\s*quote|p\s*&\s*a)?\s*(?:for|on)?\s*",
    re.IGNORECASE,
)
_LEAD_NOISE = re.compile(r"^(?:of|x|for|on)\s+", re.IGNORECASE)
_UNIT_NORMAL = {"feet": "ft", "foot": "ft", "'": "ft", "each": "ea",
                "pc": "ea", "pcs": "ea", "piece": "ea", "pieces": "ea"}


def _normalize_unit(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    u = u.lower()
    return _UNIT_NORMAL.get(u, u)


def _parse_one(fragment: str) -> Optional[LineItem]:
    raw = fragment.strip().strip("?.! ")
    if not raw:
        return None
    frag = _LEAD_NOISE.sub("", raw).strip()

    qty: Optional[float] = None
    unit: Optional[str] = None
    product = frag

    m = _PAREN_QTY.match(frag)
    if m:
        qty = float(m.group("qty"))
        product = m.group("rest").strip()
    else:
        m = _QTY_UNIT.match(frag)
        if m and m.group("qty"):
            qty = float(m.group("qty").replace(",", ""))
            unit = _normalize_unit(m.group("unit"))
            product = frag[m.end():].strip()

    product = re.sub(r"^(?:of|x)\s+", "", product, flags=re.IGNORECASE).strip()
    if not product:
        product = frag

    category = catalog.category_for_text(product)

    spec: dict = {}
    nn = _AWG_CONDUCTORS.search(product)
    if nn:
        if Category.group_of(category) == "wire_cable":
            spec["awg"], spec["conductors"] = nn.group("awg"), nn.group("cond")
        else:
            # e.g. "3/4 EMT" -> trade size, not gauge
            spec["trade_size"] = f"{nn.group('awg')}/{nn.group('cond')}"
    else:
        ma = _AWG.search(product)
        if ma:
            spec["awg"] = ma.group("awg")
    amp = _AMPS.search(product)
    if amp:
        spec["amps"] = amp.group("amps")

    return LineItem(raw=raw, product_text=product, quantity=qty,
                    unit=unit, category=category, spec=spec)


def _parse_regex(text: str) -> list[LineItem]:
    cleaned = _BOILERPLATE.sub("", text, count=1)
    cleaned = _NUM_COMMA.sub("", cleaned)
    items: list[LineItem] = []
    for frag in _SPLIT.split(cleaned):
        item = _parse_one(frag)
        if item and item.product_text:
            items.append(item)
    return items


_LLM_SYSTEM = (
    "You extract electrical product line items from a buyer's request. "
    "Return ONLY a JSON array. Each element: "
    '{"quantity": number|null, "unit": string|null, "product": string}. '
    "Do not invent items. No prose, no code fences."
)


def parse_request(text: str, llm: Optional[LLMClient] = None) -> QuoteRequest:
    items: list[LineItem] = []

    if llm is not None:
        raw = llm.complete(f"Request: {text}", system=_LLM_SYSTEM)
        parsed = extract_json(raw) if raw else None
        if isinstance(parsed, list) and parsed:
            for el in parsed:
                if not isinstance(el, dict) or not el.get("product"):
                    continue
                product = str(el["product"]).strip()
                qty = el.get("quantity")
                item = LineItem(
                    raw=product,
                    product_text=product,
                    quantity=float(qty) if isinstance(qty, (int, float)) else None,
                    unit=_normalize_unit(el.get("unit")),
                    category=catalog.category_for_text(product),
                    spec=_parse_one(product).spec if _parse_one(product) else {},
                )
                items.append(item)

    if not items:                      # LLM absent, empty, or malformed
        items = _parse_regex(text)

    return QuoteRequest(raw_text=text, items=items)
