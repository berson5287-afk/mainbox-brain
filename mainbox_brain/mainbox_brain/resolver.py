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
from . import catalog, vendors, material

# scoring weights
_W_BASE = 1.0
_W_PER_LINE = 2.0          # each matched manufacturer line the vendor carries
_W_SENT_HISTORY = 4.0      # vendor has quoted this category before (empirical)
_W_MULTI_ITEM = 1.5        # bonus per extra item a single vendor can cover
_MIN_SENT_RECORDS = 2      # RFQs in a category before history alone qualifies a vendor
_W_SENT_GROUP = 2.0        # v0.11: same GROUP (e.g. any raceway) quoted before -- weaker than exact
_W_VENDOR_MAP = 3.5        # v0.11: curated vendor map (mined from 10.9K sent mails) says they carry it
_W_VENDOR_MAP_GROUP = 1.5  # ... or at least the same group


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

    # vendor_id -> {category: how many RFQs you've sent them for it}
    # v0.9.1: counted, not just present. A single mis-tagged RFQ (an "IMC
    # conduit" line once read as MC cable) used to drag a conduit house into
    # every MC-cable proposal with the full empirical weight.
    sent_counts_by_vendor: dict[str, dict[str, int]] = {}
    for rec in sent_history:
        if rec.vendor_id:
            d = sent_counts_by_vendor.setdefault(rec.vendor_id, {})
            for c in rec.categories:
                d[c] = d.get(c, 0) + 1
    sent_cats_by_vendor: dict[str, set[str]] = {
        vid: {c for c, n in d.items() if n >= _MIN_SENT_RECORDS}
        for vid, d in sent_counts_by_vendor.items()
    }

    # v0.11: group-level views of the request, for the fallbacks below
    req_groups = {material.group_of(c) for c in req_categories} - {""}
    sent_groups_by_vendor: dict[str, set[str]] = {
        vid: {material.group_of(c) for c, n in d.items() if n >= _MIN_SENT_RECORDS} - {""}
        for vid, d in sent_counts_by_vendor.items()
    }

    resolved: list[ResolvedVendor] = []
    for v in vendors.all_vendors():
        matched = sorted(v.lines & all_req_manus)
        sent_overlap = sent_cats_by_vendor.get(v.vendor_id, set()) & req_categories
        group_overlap = sent_groups_by_vendor.get(v.vendor_id, set()) & req_groups
        vmap = material.vendor_categories(v.vendor_id)
        map_exact = {c for c in req_categories if vmap.get(c, 0) >= _MIN_SENT_RECORDS}
        map_group = {g for g in req_groups
                     if sum(n for c, n in vmap.items() if material.group_of(c) == g) >= _MIN_SENT_RECORDS}
        if not matched and not sent_overlap and not map_exact:
            # weak signals alone (same group only) never qualify a vendor on
            # their own -- they only rank vendors that already qualify
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
        elif group_overlap:
            score += _W_SENT_GROUP
            reasons.append("you've quoted this material family before")
        if map_exact:
            score += _W_VENDOR_MAP
            reasons.append("vendor map: they get your RFQs for this material")
        elif map_group:
            score += _W_VENDOR_MAP_GROUP
            reasons.append("vendor map: they get your RFQs for this family")
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
