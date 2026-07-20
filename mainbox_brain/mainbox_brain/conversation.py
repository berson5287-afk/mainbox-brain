"""
Conversation -- the confirm flow you described, as a small state machine.

    "Can I get price and availability for 10,000ft of 12/2 MC?"
       -> brain: "I have Mark at Brazil and Thea at PipeAndWire. Send to them?"
    user: "yes" | "just Mark" | "both plus Acme" | "no"
       -> brain: "Send now, or create drafts?"
    user: "draft" | "send now"
       -> brain drafts/sends, done.

The reply parsers are regex/keyword based (work offline) and accept the
natural phrasings you gave: all of them, a subset by name, or a subset plus
extra vendors. An optional LLM can be slotted into _parse_selection for
messier input later.
"""
from __future__ import annotations
import re
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional

from .models import QuoteRequest, ResolvedVendor, SentRecord, Vendor, Contact
from . import resolver, rfq, vendors
from .graph_client import MailClient


class State(Enum):
    START = auto()
    AWAIT_VENDOR_CHOICE = auto()
    AWAIT_DELIVERY_CHOICE = auto()
    DONE = auto()


@dataclass
class Turn:
    """What the brain says back, plus whether it expects another reply."""
    message: str
    done: bool = False


_YES = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "do it", "send it", "go"}
_NO = {"no", "n", "nope", "nevermind", "never mind", "cancel", "stop"}
_ALL = {"both", "all", "everyone", "all of them", "both of them"}
_ADD_RE = re.compile(r"\b(?:plus|and also|also|add|include)\b", re.IGNORECASE)
_AT_RE = re.compile(r"([a-z][a-z'\-\.]+)\s+(?:at|@|from)\s+([a-z][a-z'\-\. &]+)", re.IGNORECASE)
_NAME_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\bplus\b|\bwith\b|\balso\b|\+|&)\s*", re.IGNORECASE)
_AFFIRM_TOKENS = {"yes", "yeah", "yep", "sure", "ok", "okay", "y", "all",
                  "both", "everyone", "them", "of", "to", "it", "send", "go", "do"}
_QUALIFIERS = {"just", "only", "send", "to", "the", "please", "it", "them"}
_DRAFT_RE = re.compile(r"\b(?:as\s+)?drafts?\b", re.IGNORECASE)
# "send to mark" is ROUTING, not delivery -- only send-without-"to" counts
_SEND_RE = re.compile(r"\bsend\b(?!\s+(?:to|it\s+to|them\s+to)\b)(?:\s+(?:it|them|now))?|\bnow\b",
                      re.IGNORECASE)


def _extract_delivery(text: str) -> tuple[str | None, str]:
    """Pull delivery intent out of a reply. 'yes, drafts' -> ('draft', 'yes,').
    Draft wins if both appear ('send drafts' means create drafts)."""
    mode = None
    if _DRAFT_RE.search(text):
        mode = "draft"
        text = _DRAFT_RE.sub(" ", text)
        text = re.sub(r"\bsend\b|\bcreate\b|\bmake\b", " ", text, flags=re.IGNORECASE)
    elif _SEND_RE.search(text):
        mode = "send"
        text = _SEND_RE.sub(" ", text)
    return mode, text
_TOKEN = re.compile(r"[a-z0-9'\-]+")


# generic words that must never identify a vendor ("thea" must not prefix-match
# the "The" in "The Okonite Co."; "supply" appears in half the registry)
_GENERIC = {"the", "co", "inc", "llc", "corp", "and", "of", "usa", "company",
            "group", "assoc", "associates", "electric", "electrical", "supply",
            "supplies", "sales", "enterprises", "international", "brothers"}


def _tokens_match(a: str, b: str) -> bool:
    """Prefix-tolerant token equality: 'mark' matches 'markh', 'steph' matches
    'stephanie'. Requires >=4 chars so 'the' can't claim 'thea'; tokens under
    2 chars never match (the stray 'a' in a sentence must not select
    'A_Collora')."""
    if len(a) < 2 or len(b) < 2:
        return False
    if a in _GENERIC or b in _GENERIC:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 4 and longer.startswith(shorter)


def _strict_lookup(text: str):
    """Whole-word vendor/contact lookup. 'just' must never match 'Justin'.
    Returns (vendor, contact_or_None) or (None, None); ambiguous -> (None, None)."""
    tokens = {t for t in _TOKEN.findall(text.lower())} - _QUALIFIERS
    if not tokens:
        return None, None
    tokens -= _GENERIC
    if not tokens:
        return None, None
    matches = []
    for v in vendors.all_vendors():
        v_tokens = set(_TOKEN.findall(v.vendor_id.lower()))
        v_tokens |= set(_TOKEN.findall(v.name.lower()))
        v_tokens -= _GENERIC
        contact_hit = None
        for c in v.contacts:
            c_tokens = set(_TOKEN.findall(c.name.lower()))
            if c_tokens & tokens:
                contact_hit = c
                break
        if (v_tokens & tokens) or contact_hit:
            matches.append((v, contact_hit))
    if len(matches) == 1:
        return matches[0]
    return None, None


class QuoteConversation:
    def __init__(self, mail: MailClient,
                 sent_history: Optional[list[SentRecord]] = None,
                 store=None) -> None:
        self.mail = mail
        self.sent_history = sent_history if sent_history is not None else mail.recent_sent()
        self.store = store          # optional: enables recall-fallback proposals
        self.state = State.START
        self.request: Optional[QuoteRequest] = None
        self.proposed: list[ResolvedVendor] = []
        self.selected: list[ResolvedVendor] = []
        self._ambiguous = None
        self._auto_drafted = False

    # -- entry ---------------------------------------------------------------
    def start(self, request: QuoteRequest) -> Turn:
        self.request = request
        self.proposed = resolver.resolve(request, self.sent_history)[:5]  # top 5

        # Recall fallback: when category-based resolution finds nothing (or the
        # category is unknown), search past RFQ lines for similar product text
        # and propose the vendors you actually sent it to. This is the bridge
        # between the quote flow and `find`.
        if not self.proposed and self.store is not None:
            self.proposed = self._recall_proposals(request)

        self.state = State.AWAIT_VENDOR_CHOICE
        if not self.proposed:
            return Turn("I don't have a vendor on file for that. "
                        "Tell me who to send it to (e.g. 'Mark at Brazill and Thea') "
                        "or say 'no' to drop it.")
        return Turn(resolver.summarize_proposal(request, self.proposed))

    def _recall_proposals(self, request: QuoteRequest) -> list[ResolvedVendor]:
        seen: dict[str, ResolvedVendor] = {}
        for it in request.items:
            for hit in self.store.find(it.product_text, limit=6):
                v = vendors.find_vendor_by_name(hit["vendor"]) or \
                    vendors.get_vendor(hit["vendor"].lower())
                if v is None or v.vendor_id in seen:
                    continue
                contact = next((c for c in v.contacts
                                if c.email.lower() == hit["to"].lower()),
                               v.primary_contact)
                seen[v.vendor_id] = ResolvedVendor(
                    vendor=v, contact=contact,
                    reasons=[f"you sent a similar line before: {hit['item'][:60]!r}"
                             + (f" on {hit['when']}" if hit["when"] else "")])
        return list(seen.values())[:5]

    # -- drive ---------------------------------------------------------------
    def handle(self, user_text: str) -> Turn:
        if self.state == State.AWAIT_VENDOR_CHOICE:
            return self._handle_vendor_choice(user_text)
        if self.state == State.AWAIT_DELIVERY_CHOICE:
            return self._handle_delivery_choice(user_text)
        # escape hatch: auto-drafted by learned default, but the user wanted
        # this one sent -- "send now" right after still works
        if self._auto_drafted and _extract_delivery(user_text)[0] == "send":
            self._auto_drafted = False
            return self._execute("send")
        return Turn("This request is already wrapped up.", done=True)

    # -- step 1: who --------------------------------------------------------
    def _handle_vendor_choice(self, text: str) -> Turn:
        low = text.strip().lower()
        if low in _NO:
            self.state = State.DONE
            return Turn("Okay, I won't send anything.", done=True)

        # overanswer handling: if the reply already says how to deliver
        # ("just mark, send now" / "yes - drafts"), never ask again
        delivery, remainder = _extract_delivery(text)

        self._ambiguous = None
        selected, extras = self._parse_selection(remainder if remainder.strip() else text)
        if self._ambiguous:
            name, options = self._ambiguous
            opts = " or ".join(self._who(r) for r in options)
            return Turn(f"Which one for {name!r} — {opts}?")
        if not selected and not extras:
            return Turn("Didn't catch who to send to. Say 'yes' for all, name a "
                        "vendor or contact ('Stephanie at Warshaw'), or 'no' to drop it.")

        self.selected = selected + extras
        who = ", ".join(self._who(r) for r in self.selected)

        if delivery is not None:
            return self._execute(delivery)

        # low-risk default: if you've consistently chosen drafts, stop asking
        if self.store is not None:
            learned = self.store.learned_delivery_default()
            if learned == "draft":
                turn = self._execute("draft")
                self._auto_drafted = True
                return Turn(turn.message + " (your usual — say 'send now' if you "
                            "want these sent immediately instead)", done=True)

        self.state = State.AWAIT_DELIVERY_CHOICE
        return Turn(f"Got it — {who}. Send now, or create drafts?")

    def _parse_selection(self, text: str) -> tuple[list[ResolvedVendor], list[ResolvedVendor]]:
        low = text.strip().lower()
        extras: list[ResolvedVendor] = []
        claimed: set[str] = set()
        select_all = False
        leftover: list[str] = []

        # chunk by separators (and/plus/with/commas) so "yes plus nicole at ammo"
        # becomes ["yes", "nicole at ammo"] -- an affirmation can never be
        # swallowed by a name match again
        for chunk in _NAME_SPLIT.split(low):
            chunk = chunk.strip(" .!?")
            if not chunk:
                continue
            tokens = set(_TOKEN.findall(chunk))
            if tokens and tokens <= _AFFIRM_TOKENS:      # "yes", "yes both", "ok all"
                select_all = True
                continue
            if chunk in {"just"}:
                continue
            # FIRST: the proposal is the context the user is answering about,
            # so match it before the global registry, prefix-tolerant
            # ("mark" must select the proposed Markh, not some global Mark)
            prop_hits = []
            chunk_tokens = {t for t in _TOKEN.findall(chunk)} - _QUALIFIERS
            for r in self.proposed:
                if r.vendor.vendor_id in claimed:
                    continue
                v_tokens = set(_TOKEN.findall(r.vendor.vendor_id.lower()))
                v_tokens |= set(_TOKEN.findall(r.vendor.name.lower()))
                named = None
                for c in r.vendor.contacts:
                    if any(_tokens_match(ct, qt) for ct in _TOKEN.findall(c.name.lower())
                           for qt in chunk_tokens):
                        named = c
                        break
                if named or any(_tokens_match(vt, qt) for vt in v_tokens
                                for qt in chunk_tokens):
                    prop_hits.append((r, named))
            if len(prop_hits) == 1:
                r, named = prop_hits[0]
                if named is not None and named is not r.contact:
                    r = ResolvedVendor(vendor=r.vendor, contact=named,
                                       matched_manufacturers=r.matched_manufacturers,
                                       covered_items=r.covered_items,
                                       score=r.score, reasons=r.reasons)
                extras.append(r)
                claimed.add(r.vendor.vendor_id)
                continue
            if len(prop_hits) > 1:
                self._ambiguous = (chunk, [r for r, _ in prop_hits[:3]])
                leftover.append(chunk)
                continue

            m = _AT_RE.search(chunk)
            if m:                                   # "Stephanie at Warshaw"
                who, where = m.group(1).strip(), m.group(2).strip()
                v, _ = _strict_lookup(where)
                if v and v.vendor_id not in claimed:
                    contact = next((c for c in v.contacts
                                    if c.name.lower().startswith(who)),
                                   v.primary_contact)
                    extras.append(ResolvedVendor(vendor=v, contact=contact,
                                                 reasons=["you named this contact"]))
                    claimed.add(v.vendor_id)
                continue
            # bare vendor/contact name -- whole-word only
            v, named_contact = _strict_lookup(chunk)
            if v and v.vendor_id not in claimed:
                if any(r.vendor.vendor_id == v.vendor_id for r in self.proposed):
                    leftover.append(chunk)           # selects from the proposal below
                else:
                    extras.append(ResolvedVendor(
                        vendor=v, contact=named_contact or v.primary_contact,
                        reasons=["you named this vendor"]))
                    claimed.add(v.vendor_id)
                continue
            leftover.append(chunk)

        if select_all:
            return list(self.proposed), extras
        low = " ".join(leftover)

        # subset by name/contact mentioned in the proposal (whole-word match:
        # "just markh" must NOT also select an unrelated "Mark")
        words = set(re.findall(r"[a-z0-9&'\-]+", low))
        chosen: list[ResolvedVendor] = []
        for r in self.proposed:
            if r.vendor.vendor_id in claimed:
                continue
            names = [r.vendor.vendor_id, r.vendor.name.lower()]
            names += [c.name.lower() for c in r.vendor.contacts]
            tokens: set[str] = set()
            for n in names:
                tokens |= set(re.findall(r"[a-z0-9&'\-]+", n))
            if tokens & words:
                named = next((c for c in r.vendor.contacts
                              if set(re.findall(r"[a-z0-9&'\-]+", c.name.lower())) & words),
                             None)
                if named is not None:
                    r = ResolvedVendor(vendor=r.vendor, contact=named,
                                       matched_manufacturers=r.matched_manufacturers,
                                       covered_items=r.covered_items,
                                       score=r.score, reasons=r.reasons)
                chosen.append(r)
        return chosen, extras

    # -- step 2: how --------------------------------------------------------
    def _handle_delivery_choice(self, text: str) -> Turn:
        mode, _ = _extract_delivery(text)
        if mode is None:
            return Turn("Send now, or create drafts? (say 'send' or 'draft')")
        return self._execute(mode)

    def _execute(self, mode: str) -> Turn:
        results = []
        for r in self.selected:
            draft = rfq.draft_rfq(self.request, r.vendor, r.contact)
            if mode == "draft":
                self.mail.create_draft(draft)
                results.append(f"draft for {self._who(r)}")
            else:
                self.mail.send_email(draft)
                results.append(f"sent to {self._who(r)}")
        if self.store is not None:
            try:
                self.store.record_delivery_choice(mode)
            except Exception:
                pass
        self.state = State.DONE
        verb = "Created" if mode == "draft" else "Sent"
        return Turn(f"{verb}: " + "; ".join(results) + ".", done=True)

    @staticmethod
    def _who(r: ResolvedVendor) -> str:
        return f"{r.contact.name} ({r.vendor.name})" if r.contact else r.vendor.name
