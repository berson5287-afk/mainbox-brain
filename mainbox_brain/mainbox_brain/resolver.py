"""
Resolver -- the heart of the brain.

Chain (exactly what we worked out in conversation):

    product spec
      -> category                        (parser)
      -> manufacturers making it         (catalog / line card)
      -> vendors carrying those lines    (vendors.py)
      + vendors you've actually quoted   (Sent Items / graph_client)
      -> ranked, with the contact attached

Scoring favors vendors that BOTH carry the line AND have quoted the category
before. The sent-history signal is weighted highest because it's empirical:
it's what you actually do, not just what a brand sheet implies.
"""
from __future__ import annotations
from typing import Optional

from .models import QuoteRequest, ResolvedVendor, SentRecord, Category
from . import catalog, vendors

# scoring weights
_W_BASE = 1.0
_W_PER_LINE = 2.0          # each matched manufacturer line the vendor carries
_W_SENT_HISTORY = 4.0      # vendor has quoted this category before (empirical)
_W_MULTI_ITEM = 1.5        # bonus per extra item a single vendor can cover


def resolve(
    request: QuoteRequest,
    sent_history: Optional[list[SentRecord]] = None,
) -> list[ResolvedVendor]:
    sent_history = sent_history or []

    # categories present in this request
    req_categories = {it.category for it in request.items if it.category != Category.UNKNOWN}

    # which manufacturers satisfy the request, per category
    manus_by_cat: dict[str, set[str]] = {
        cat: set(catalog.manufacturers_for_category(cat)) for cat in req_categories
    }
    all_req_manus: set[str] = set().union(*manus_by_cat.values()) if manus_by_cat else set()

    # vendor_id -> categories quoted before (from sent items)
    sent_cats_by_vendor: dict[str, set[str]] = {}
    for rec in sent_history:
        if rec.vendor_id:
            sent_cats_by_vendor.setdefault(rec.vendor_id, set()).update(rec.categories)

    resolved: list[ResolvedVendor] = []
    for v in vendors.all_vendors():
        matched = sorted(v.lines & all_req_manus)
        sent_overlap = sent_cats_by_vendor.get(v.vendor_id, set()) & req_categories
        if not matched and not sent_overlap:
            continue

        # which requested items this vendor can cover
        covered = []
        for it in request.items:
            it_manus = manus_by_cat.get(it.category, set())
            if v.lines & it_manus:
                covered.append(it.describe())

        score = _W_BASE
        reasons: list[str] = []
        if matched:
            score += _W_PER_LINE * len(matched)
            reasons.append(f"carries {len(matched)} matching line(s): "
                           + ", ".join(matched[:4]) + ("…" if len(matched) > 4 else ""))
        if sent_overlap:
            score += _W_SENT_HISTORY
            reasons.append("you've quoted this category before (sent history)")
        if len(covered) > 1:
            score += _W_MULTI_ITEM * (len(covered) - 1)
            reasons.append(f"covers {len(covered)} of {len(request.items)} items")

        resolved.append(ResolvedVendor(
            vendor=v,
            contact=v.primary_contact,
            matched_manufacturers=matched,
            covered_items=covered,
            score=round(score, 2),
            reasons=reasons,
        ))

    resolved.sort(key=lambda r: r.score, reverse=True)
    return resolved


def summarize_proposal(request: QuoteRequest, resolved: list[ResolvedVendor]) -> str:
    """Human-facing one-liner, like the rep voice you described."""
    item_str = "; ".join(it.describe() for it in request.items) or request.raw_text
    if not resolved:
        return (f"For {item_str} I don't have a vendor on file. "
                "Add one in vendors.py or tell me who to send it to.")
    names = []
    for r in resolved:
        who = r.contact.name if r.contact else r.vendor.name
        names.append(f"{who} at {r.vendor.name}")
    if len(names) == 1:
        vendor_phrase = names[0]
    else:
        vendor_phrase = ", ".join(names[:-1]) + f" and {names[-1]}"
    return (f"For {item_str} I have {vendor_phrase} as your vendors. "
            "Would you like me to send it to them?")
