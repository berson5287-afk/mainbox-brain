"""
RFQ drafter -- turn a request + a chosen vendor/contact into an email.

Plain text body, the way these actually go out. Subject is built from the
items so the vendor's reply threads cleanly (and so your MaINbox grouping can
catch the response later).
"""
from __future__ import annotations

from .models import QuoteRequest, Vendor, Contact, EmailDraft
from . import config


def _line_block(request: QuoteRequest) -> str:
    rows = []
    for it in request.items:
        spec_bits = []
        if it.spec.get("awg") and it.spec.get("conductors"):
            spec_bits.append(f"{it.spec['awg']}/{it.spec['conductors']}")
        elif it.spec.get("awg"):
            spec_bits.append(f"{it.spec['awg']} AWG")
        if it.spec.get("amps"):
            spec_bits.append(f"{it.spec['amps']}A")
        spec = f" ({', '.join(spec_bits)})" if spec_bits else ""
        rows.append(f"  - {it.describe()}{spec}")
    return "\n".join(rows)


def _subject(request: QuoteRequest) -> str:
    if request.items:
        head = request.items[0].product_text
        extra = f" + {len(request.items) - 1} more" if len(request.items) > 1 else ""
        return f"RFQ: {head}{extra} — price & availability"
    return "RFQ: price & availability"


def draft_rfq(request: QuoteRequest, vendor: Vendor,
              contact: Contact | None = None) -> EmailDraft:
    contact = contact or vendor.primary_contact
    to_name = contact.name if contact else vendor.name
    to_email = contact.email if contact else ""

    body = (
        f"Hi {to_name},\n\n"
        f"Could you send pricing and current availability (including lead time) "
        f"on the following?\n\n"
        f"{_line_block(request)}\n\n"
        f"Let me know if you need any clarification on specs. Thanks!\n\n"
        f"{config.SENDER_NAME}\n"
        f"{config.COMPANY_NAME}\n"
        f"{config.SENDER_EMAIL}"
    )
    return EmailDraft(to=to_email, to_name=to_name,
                      subject=_subject(request), body=body)
