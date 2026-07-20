#!/usr/bin/env python3
"""
rfq_match.py  -  match an inbound vendor email to an RFQ (and to a vendor on it).
v0.1.0

This is the *producer* half of automatic reply matching. It's a pure module:
no file IO, no network, no Outlook. Give it an email (subject, body, sender) and
a list of candidate RFQ dicts, and it tells you which RFQ ref(s) the email is
about and which vendor it's from. The voice server wraps this over HTTP at
POST /api/rfq/reply; MaINbox's inbox hook forwards new mail to that endpoint.

Keeping the logic here (pure + tested) rather than inside the server or inside
MaINbox means the same matcher can be reused anywhere and its behavior is
pinned by unit tests.

Matching signals, strongest first:
  1. RFQ ref in the subject      (e.g. "RE: RFQ VR-20260707-001")
  2. RFQ ref in the body tag     (e.g. "[Ref VR-20260707-001 ...]") — forwards
                                  often strip the subject, so the body tag we
                                  stamp on every outgoing RFQ is the backstop.
Vendor attribution, strongest first:
  a. sender email exactly matches a vendor on the RFQ
  b. sender *domain* matches a vendor's domain (different mailbox, same vendor)
  c. the RFQ has exactly one vendor -> attribute to it (low confidence)
  d. otherwise unattributed (ref is known, sender isn't) — still a real reply,
     the caller records it against the RFQ without flipping a specific vendor.

We never guess an RFQ from the vendor alone (a vendor can have many open RFQs);
no ref in the mail means no match.
"""

from __future__ import annotations

import re
import hashlib

__version__ = "0.1.0"

# ref format produced by the server's _next_ref(): VR-YYYYMMDD-NNN
_REF_RE = re.compile(r"\bVR-(\d{8})-(\d{3})\b", re.IGNORECASE)


def extract_refs(*texts: str) -> list[str]:
    """All distinct RFQ refs found across the given texts, upper-cased,
    order preserved (first occurrence wins)."""
    seen, out = set(), []
    for t in texts:
        for m in _REF_RE.finditer(t or ""):
            ref = f"VR-{m.group(1)}-{m.group(2)}".upper()
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def _ref_in(text: str, ref: str) -> bool:
    return ref.upper() in [r.upper() for r in extract_refs(text or "")]


def _domain(email: str) -> str:
    email = (email or "").strip().lower()
    return email.split("@", 1)[1] if "@" in email else ""


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def msg_key(ref: str, sender: str, subject: str) -> str:
    """Stable short id for one (ref, sender, subject) so a re-forwarded copy of
    the same email is recognized and not double-recorded."""
    raw = f"{ref.upper()}|{_norm(sender)}|{(subject or '').strip().lower()}"
    return "m" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def attribute_vendor(sender: str, rfq: dict) -> tuple[str, str, float]:
    """(vendor_email, method, confidence_factor). vendor_email is '' when the
    sender can't be tied to a specific vendor on this RFQ."""
    vendors = [v.get("email", "") for v in rfq.get("vendors", [])]
    s = _norm(sender)
    for em in vendors:                       # a. exact
        if _norm(em) == s and s:
            return em, "exact", 1.0
    sd = _domain(sender)
    if sd:                                    # b. domain
        for em in vendors:
            if _domain(em) == sd:
                return em, "domain", 0.85
    if len(vendors) == 1 and vendors[0]:      # c. sole vendor
        return vendors[0], "sole-vendor", 0.7
    return "", "unmatched-sender", 0.6        # d. unattributed


def match_email(subject: str, body: str, sender: str,
                rfqs: list[dict]) -> dict:
    """Match one email against candidate RFQ dicts (each needs at least
    'ref' and 'vendors'). Returns:
      {matched: bool,
       matches: [{ref, vendor, method, via, confidence, msg_key}],
       reason: str}
    Only refs present in BOTH the email and `rfqs` produce matches.
    """
    by_ref = {r.get("ref", "").upper(): r for r in rfqs if r.get("ref")}
    subj_refs = set(extract_refs(subject))
    all_refs = extract_refs(subject, body)
    matches = []
    for ref in all_refs:
        rfq = by_ref.get(ref)
        if not rfq:
            continue                          # ref not among candidates
        via = "subject" if ref in subj_refs else "body"
        base = 0.95 if via == "subject" else 0.85
        vendor, method, factor = attribute_vendor(sender, rfq)
        matches.append({
            "ref": ref,
            "vendor": vendor,
            "method": method,
            "via": via,
            "confidence": round(base * factor, 3),
            "msg_key": msg_key(ref, sender, subject),
        })
    if matches:
        return {"matched": True, "matches": matches, "reason": "ref matched"}
    if all_refs:
        return {"matched": False, "matches": [],
                "reason": "ref(s) found but none match an open RFQ: "
                          + ", ".join(all_refs)}
    return {"matched": False, "matches": [],
            "reason": "no RFQ ref in subject or body"}


if __name__ == "__main__":
    # tiny self-demo
    demo = [{"ref": "VR-20260707-001",
             "vendors": [{"email": "quotes@graybar.com"},
                         {"email": "rfq@rexel.com"}]}]
    for subj, body, frm in [
        ("RE: RFQ VR-20260707-001", "here is our quote", "quotes@graybar.com"),
        ("Re: your request", "regarding [Ref VR-20260707-001]", "rfq@rexel.com"),
        ("quote", "[Ref VR-20260707-001]", "someone@unknown.com"),
        ("no ref here", "just a hello", "quotes@graybar.com"),
    ]:
        print(subj, "->", match_email(subj, body, frm, demo))
