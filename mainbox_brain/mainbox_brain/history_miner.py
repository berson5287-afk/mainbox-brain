"""
History miner -- builds the vendor registry FROM your Sent Items.

This is the zero-config flip: instead of hand-maintaining vendors.py, the app
scans outgoing mail, figures out who you ask for prices, what categories you
ask them about, and emits both:

    1. a learned vendor registry  (dict[str, Vendor])  -- who + contacts
    2. SentRecords                                     -- the empirical signal

The hard problem this module exists to solve: your Sent Items contain TWO
kinds of price emails going opposite directions --

    RFQs you send TO VENDORS        ("can you send pricing on...")
    quotes you send TO CUSTOMERS    ("please find our quote attached...")

Suggesting a customer as a vendor is the worst failure this feature can have,
so classification is deliberately conservative:

    - keyword classifier first (deterministic, explainable)
    - optional LLM only for the ambiguous middle
    - a confidence floor: a learned vendor is only *suggested* once it has
      been seen >= MIN_SIGHTINGS times (repeat behavior = real vendor)

Pipeline:  messages -> classify -> cluster by domain -> extract categories
           -> LearnedVendor registry + SentRecords
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import Vendor, Contact, SentRecord, Category
from .parser import parse_request
from .llm import LLMClient

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIN_SIGHTINGS = 2          # vendor must appear in >= N outgoing RFQs to be suggested
RECENCY_HALF_LIFE_DAYS = 180  # for "last couple of times" style weighting

# Free/public mail domains never identify a company by domain alone.
_PUBLIC_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com",
    "icloud.com", "msn.com", "live.com", "comcast.net", "verizon.net",
}

# ---------------------------------------------------------------------------
# Quoted-thread stripping (the big real-data lesson)
#
# ~83% of real sent bodies contain quoted reply chains. The text below the
# divider is the OTHER party's voice -- classifying on it credits customers'
# "please quote" lines to the user. Everything (classification, categories,
# greeting) must run on the user's own words only: the text ABOVE the first
# reply marker.
# ---------------------------------------------------------------------------
_THREAD_MARKERS = re.compile(
    r"-+\s*Original Message\s*-+"
    r"|_{10,}"                                   # Outlook divider line
    r"|^\s*From:\s.+$"                            # embedded header block
    r"|^\s*On .{,80}wrote:\s*$",                  # "On <date>, <who> wrote:"
    re.IGNORECASE | re.MULTILINE,
)


def strip_quoted_thread(body: str) -> str:
    """Return only the sender's own words (text above the first reply marker)."""
    if not body:
        return ""
    m = _THREAD_MARKERS.search(body)
    return body[:m.start()] if m else body

# ---------------------------------------------------------------------------
# Direction classification (the safety-critical step)
# ---------------------------------------------------------------------------
RFQ_TO_VENDOR = "rfq_to_vendor"
QUOTE_TO_CUSTOMER = "quote_to_customer"
UNKNOWN = "unknown"

# Asking for price = vendor-bound
_VENDOR_CUES = [
    "price and availability", "price & availability", "p&a", "p & a",
    "can you quote", "could you quote", "please quote", "quote me",
    "send pricing", "send me pricing", "your pricing", "your price on",
    "what's your lead time", "what is your lead time", "lead time on",
    "do you stock", "do you have stock", "availability on", "best price on",
    "cost on", "your cost", "rfq",
]
# Giving price = customer-bound
_CUSTOMER_CUES = [
    "please find our quote", "attached is our quote", "quote attached",
    "our quote", "pricing below", "price below", "prices below",
    "valid for 30 days", "valid for 15 days", "quote is valid",
    "per your request, pricing", "happy to offer", "we can offer",
    "your price is", "unit price:", "thank you for the opportunity",
    "thank you for your inquiry", "lead time is", "we have stock",
    "in stock and ready",
]

_LLM_SYSTEM = (
    "You classify an OUTGOING email from an electrical distributor's salesperson. "
    "Is the sender ASKING for prices (rfq_to_vendor) or GIVING prices "
    "(quote_to_customer)? Reply with exactly one word: rfq_to_vendor, "
    "quote_to_customer, or unknown."
)


def classify_direction(subject: str, body: str,
                       llm: Optional[LLMClient] = None) -> str:
    text = f"{subject}\n{body}".lower()
    vendor_hits = sum(1 for c in _VENDOR_CUES if c in text)
    customer_hits = sum(1 for c in _CUSTOMER_CUES if c in text)

    if vendor_hits and not customer_hits:
        return RFQ_TO_VENDOR
    if customer_hits and not vendor_hits:
        return QUOTE_TO_CUSTOMER
    if vendor_hits and customer_hits:
        # mixed language -> only trust a clear margin
        if vendor_hits >= customer_hits + 2:
            return RFQ_TO_VENDOR
        if customer_hits >= vendor_hits + 2:
            return QUOTE_TO_CUSTOMER

    if llm is not None:
        raw = llm.complete(f"Subject: {subject}\n\n{body[:1500]}", system=_LLM_SYSTEM)
        if raw:
            word = raw.strip().lower().split()[0].strip(".,")
            if word in {RFQ_TO_VENDOR, QUOTE_TO_CUSTOMER}:
                return word
    return UNKNOWN


# ---------------------------------------------------------------------------
# Message shape the miner consumes (Graph maps onto this trivially)
# ---------------------------------------------------------------------------
@dataclass
class SentMessage:
    to_email: str
    to_display_name: str = ""
    subject: str = ""
    body: str = ""
    when: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Entity extraction helpers
# ---------------------------------------------------------------------------
_GREETING = re.compile(
    r"^\s*(?:hi|hey|hello|good\s+(?:morning|afternoon|evening)|dear)\s+([A-Za-z][a-zA-Z'\-]+)",
    re.IGNORECASE | re.MULTILINE,
)

# Words that follow "Hi ..." but are not names ("Hi just following up", "Hi guys")
_NOT_NAMES = {
    "just", "all", "guys", "team", "there", "everyone", "everybody", "sales",
    "customer", "buy", "again", "thanks", "good", "sorry", "quick", "any",
    "wanted", "following", "checking", "hope", "please", "can", "could", "pipe",
}


def _clean_name(candidate: str) -> str:
    """Validate a would-be contact name; return '' if it's garbage."""
    name = candidate.strip().strip("'\"”’, ")
    if not name or "@" in name or not name.isalpha():
        return ""
    if name.lower() in _NOT_NAMES or len(name) < 2 or len(name) > 20:
        return ""
    return name.title()


def _domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def _vendor_id_from_domain(domain: str) -> str:
    return domain.split(".", 1)[0]


def _company_name_from_domain(domain: str) -> str:
    return _vendor_id_from_domain(domain).replace("-", " ").title()


def _contact_name(msg: SentMessage) -> str:
    m = _GREETING.search(msg.body or "")
    if m:
        name = _clean_name(m.group(1))
        if name:
            return name
    if msg.to_display_name:
        first = msg.to_display_name.replace(",", " ").split()
        if first:
            name = _clean_name(first[0])
            if name:
                return name
    local = msg.to_email.split("@", 1)[0]
    name = _clean_name(re.split(r"[._\-]", local)[0])
    return name or local.title()


_CHATTER = re.compile(
    r"\b(i|we|you|please|thanks|thank|asap|tomorrow|today|yesterday|sorry|"
    r"hope|let me|can you|could you|would|attached|good news|fyi|"
    r"still|know|didn|don|doesn|won|that|those|these|getting|waiting)\b", re.IGNORECASE)


def _looks_like_product(text: str, category: str) -> bool:
    """Keep lines that look like product specs, drop conversation fragments."""
    if not text or not (2 < len(text) < 120):
        return False
    if _CHATTER.search(text):
        return False
    # a real line item nearly always carries a digit (qty, size, part no)
    # or resolved to a known category
    return category != Category.UNKNOWN or any(ch.isdigit() for ch in text)


def _categories_and_items_of(msg: SentMessage) -> tuple[set[str], list[str]]:
    req = parse_request(f"{msg.subject}\n{msg.body}")
    cats = {it.category for it in req.items if it.category != Category.UNKNOWN}
    items = [it.product_text.strip() for it in req.items
             if _looks_like_product(it.product_text.strip(), it.category)]
    return cats, items


# ---------------------------------------------------------------------------
# The miner
# ---------------------------------------------------------------------------
@dataclass
class MinedVendor:
    vendor: Vendor
    sightings: int = 0
    last_seen: Optional[datetime] = None
    categories: set[str] = field(default_factory=set)

    @property
    def confident(self) -> bool:
        return self.sightings >= MIN_SIGHTINGS


@dataclass
class MiningResult:
    vendors: dict[str, MinedVendor] = field(default_factory=dict)
    records: list[SentRecord] = field(default_factory=list)
    skipped_customer_quotes: int = 0
    skipped_unknown: int = 0
    skipped_own_domain: int = 0
    excluded_customers: list[str] = field(default_factory=list)  # by ratio rule

    def confident_registry(self) -> dict[str, Vendor]:
        """Only vendors past the confidence floor -- safe to suggest."""
        return {vid: mv.vendor for vid, mv in self.vendors.items() if mv.confident}

    def describe_for(self, categories: set[str], limit: int = 3) -> str:
        """'last couple of times you sent this to X and Y' -- literally true."""
        hits = [mv for mv in self.vendors.values()
                if mv.confident and (mv.categories & categories)]
        hits.sort(key=lambda mv: mv.last_seen or datetime.min, reverse=True)
        hits = hits[:limit]
        if not hits:
            return ""
        parts = []
        for mv in hits:
            c = mv.vendor.primary_contact
            who = f"{c.name} at {mv.vendor.name}" if c else mv.vendor.name
            parts.append(who)
        if len(parts) == 1:
            joined = parts[0]
        else:
            joined = ", ".join(parts[:-1]) + f" and {parts[-1]}"
        return f"The last couple of times you sent this to {joined}."


def mine(messages: list[SentMessage],
         llm: Optional[LLMClient] = None,
         own_domains: Optional[set[str]] = None,
         exclude_addresses: Optional[set[str]] = None) -> MiningResult:
    """Learn the vendor registry from sent messages.

    own_domains: recipients at these domains are skipped entirely (internal
        mail). Defaults to the domain of config.SENDER_EMAIL plus anything in
        the MAINBOX_OWN_DOMAINS env var (comma-separated).
    exclude_addresses: specific addresses to skip (e.g. your personal gmail).
        Also reads MAINBOX_EXCLUDE_ADDRESSES env var.
    """
    import os
    from . import config

    if own_domains is None:
        own_domains = set()
        if "@" in config.SENDER_EMAIL:
            own_domains.add(_domain(config.SENDER_EMAIL))
        own_domains |= {d.strip().lower() for d in
                        os.environ.get("MAINBOX_OWN_DOMAINS", "").split(",") if d.strip()}
    if exclude_addresses is None:
        exclude_addresses = {a.strip().lower() for a in
                             os.environ.get("MAINBOX_EXCLUDE_ADDRESSES", "").split(",")
                             if a.strip()}

    result = MiningResult()
    customer_hits: dict[str, int] = {}   # vendor_id -> quote-to-customer count

    for msg in messages:
        domain = _domain(msg.to_email)
        if not domain:
            continue
        if domain in own_domains or msg.to_email.lower() in exclude_addresses:
            result.skipped_own_domain += 1
            continue

        # The real-data lesson: judge only the sender's own words.
        own_words = strip_quoted_thread(msg.body)
        direction = classify_direction(msg.subject, own_words, llm)

        if domain in _PUBLIC_DOMAINS:
            vid = msg.to_email.lower()
        else:
            vid = _vendor_id_from_domain(domain)

        if direction == QUOTE_TO_CUSTOMER:
            result.skipped_customer_quotes += 1
            customer_hits[vid] = customer_hits.get(vid, 0) + 1
            continue
        if direction == UNKNOWN:
            result.skipped_unknown += 1
            continue

        company = (_contact_name(msg) or msg.to_email) if domain in _PUBLIC_DOMAINS \
            else _company_name_from_domain(domain)

        stripped_msg = SentMessage(msg.to_email, msg.to_display_name,
                                   msg.subject, own_words, msg.when)
        cats, item_texts = _categories_and_items_of(stripped_msg)
        name = _contact_name(stripped_msg)
        contact = Contact(name=name, email=msg.to_email.lower())

        mv = result.vendors.get(vid)
        if mv is None:
            mv = MinedVendor(vendor=Vendor(vendor_id=vid, name=company,
                                           contacts=[contact],
                                           notes="learned from sent history"))
            result.vendors[vid] = mv
        else:
            known = {c.email for c in mv.vendor.contacts}
            if contact.email not in known:
                mv.vendor.contacts.append(contact)

        mv.sightings += 1
        mv.categories |= cats
        if msg.when and (mv.last_seen is None or msg.when > mv.last_seen):
            mv.last_seen = msg.when

        result.records.append(SentRecord(
            to_email=msg.to_email.lower(), vendor_id=vid,
            categories=cats, when=msg.when, items=item_texts,
        ))

    # Ratio post-filter: if you GIVE a domain quotes as often as you ASK it
    # for them, it's a customer -- drop it from the registry entirely.
    for vid, mv in list(result.vendors.items()):
        cust = customer_hits.get(vid, 0)
        if cust >= 2 and cust >= mv.sightings:
            result.excluded_customers.append(mv.vendor.name)
            result.records = [r for r in result.records if r.vendor_id != vid]
            del result.vendors[vid]

    return result
