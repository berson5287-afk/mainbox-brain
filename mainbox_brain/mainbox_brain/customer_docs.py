"""Mine customer documents (POs, quotes, RFQ takeoffs) attached to sales-mailbox
email.

Where vendor replies tell us our COST, these tell us our SELL price and what each
customer is buying or asking us to quote.  The data is in the ATTACHMENTS -- the
email body is usually just a cover note -- so this works off the extracted
attachment text.  Two layers:

  - document-level header: PO#, quote#, total, job  (robust across formats)
  - line items: qty / description / unit price        (sell-tagged)

Line-item prices use a "prices carry decimals" + qty*unit~=extended check, which
picks the UNIT price (not the extended total) across the varied PO formats seen
in real data (Kojo / Bender / Forest / ...).
"""
from __future__ import annotations
import re
from typing import Optional

from .reply_miner import ReplyFact
from .attachment_miner import extract_text

_NUM = re.compile(r"\d[\d,]*\.\d+|\d[\d,]*")
# lines that are charges/totals, not product line items
_NOISE = re.compile(
    r"\b(tax|freight|shipping|subtotal|sub\s*total|handling|discount|surcharge|"
    r"deposit|restocking)\b|^\s*total\b", re.I)
_UOM_LEAD = re.compile(r"^(EA|FT|LF|PR|BX|CS|RL|CT|PK|M|C|E)\s+", re.I)

# document-level header fields. PO numbers always contain a digit, which lets us
# reject false matches like "ORDERED" (from "ORDERED BY") or "WER" (from "POWER").
_PO_RE = re.compile(
    r"(?<![A-Za-z])(?:customer\s+)?(?:P\.?O\.?\s*#?|purchase\s+order\s*#?|order\s*(?:no\.?|#))"
    r"\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)", re.I)
_QUOTE_RE = re.compile(
    r"\b(?:quote|quotation|bid)\s*#?\s*[:.]?\s*([A-Z]?\d[A-Z0-9\-]{3,})", re.I)
_TOTAL_RE = re.compile(r"\btotal\b\s*[:$]?\s*\$?\s*([\d,]+\.\d{2})", re.I)
_JOB_RE = re.compile(
    r"\bjob\s*(?:#|id|ref(?:erence)?|name)?\s*[:.]?\s*([A-Za-z][^\n,]{2,46})", re.I)


def _to_num(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _decs(tok: str) -> int:
    return len(tok.split(".")[1]) if "." in tok else 0


def _alpha(s: str) -> int:
    return sum(c.isalpha() for c in s)


def parse_line_items(text: str) -> list[dict]:
    """Extract [{qty, desc, unit, ext}] from a customer PO/quote.

    Strategy per line: the unit price and extended total carry decimals (part
    numbers and item codes are integers), so price candidates are decimal
    numbers.  We pick (unit, ext) among them with unit<=ext and a quantity token
    (any number) such that qty*unit ~= ext.  unit is the factor with more
    decimal places (a price), qty the other.  Description = the most
    letter-dense run between the matched number spans.
    """
    items: list[dict] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw.strip())
        if len(line) < 8 or _NOISE.search(line):
            continue
        toks = [(_to_num(m.group()), m.start(), m.end(), m.group())
                for m in _NUM.finditer(line)]
        toks = [t for t in toks if t[0] is not None]
        if len(toks) < 3:
            continue
        price_idx = [i for i, t in enumerate(toks) if "." in t[3]]
        if len(price_idx) < 2:
            continue
        best = None
        for ui in price_idx:
            for ki in price_idx:
                if ui == ki:
                    continue
                unit, ext = toks[ui][0], toks[ki][0]
                if not (0 < unit <= ext):
                    continue
                for qi in range(len(toks)):
                    if qi in (ui, ki):
                        continue
                    qty = toks[qi][0]
                    if qty <= 0 or ext <= 0:
                        continue
                    if abs(qty * unit - ext) / ext > 0.02:
                        continue
                    if _decs(toks[ui][3]) < _decs(toks[qi][3]):
                        continue           # unit should look more like a price
                    if best is None or ext > best[2]:
                        best = (qty, unit, ext, qi, ui, ki)
        if not best:
            continue
        qty, unit, ext, qi, ui, ki = best
        spans = sorted([(toks[qi][1], toks[qi][2]), (toks[ui][1], toks[ui][2]),
                        (toks[ki][1], toks[ki][2])])
        segs, prev = [], 0
        for a, b in spans:
            segs.append(line[prev:a])
            prev = b
        segs.append(line[prev:])
        # prefer the description segment right after the quantity; else most alpha
        after_qty = ""
        for a, b in [(toks[qi][1], toks[qi][2])]:
            tail = line[b:]
            after_qty = re.split(r"\d[\d,]*\.\d+", tail)[0]
        desc = after_qty if _alpha(after_qty) >= 3 else max(segs, key=_alpha)
        desc = re.sub(r"^[\s\d.,*/#%:-]+", "", desc).strip()
        desc = _UOM_LEAD.sub("", desc).strip(" .$*/#-")
        if _alpha(desc) >= 3:
            items.append({"qty": qty, "desc": desc[:60], "unit": unit, "ext": ext})
    return items


def extract_header(text: str, subject: str = "") -> dict:
    """Document-level fields that are reliable regardless of line-item layout.
    PO#, quote#, and job are often cleanest in the email subject, so try it
    first and fall back to the document body."""
    def first(rx, *sources) -> str:
        for src in sources:
            m = rx.search(src or "")
            if m:
                return m.group(1).strip(" .:#")
        return ""
    return {
        "po_number": first(_PO_RE, subject, text),
        "quote_number": first(_QUOTE_RE, subject, text),
        "total": first(_TOTAL_RE, text),
        "job": first(_JOB_RE, subject, text),
    }


def looks_like_customer_doc(text: str, header: dict) -> bool:
    """A customer PO/quote names American Power as the seller/vendor or carries a
    PO+quote/total structure.  Used to avoid mining random attachments (specs,
    certs, signatures)."""
    t = (text or "").lower()
    if "american power" in t and ("purchase order" in t or "quote" in t or "p.o" in t):
        return True
    return bool(header.get("po_number") and (header.get("total") or header.get("quote_number")))


def mine_customer_document(path: str, subject: str = "") -> tuple[dict, list[ReplyFact], str]:
    """Return (header, sell-tagged facts, note) for one customer attachment."""
    text, note = extract_text(path)
    if not text:
        return {}, [], note
    header = extract_header(text, subject)
    if not looks_like_customer_doc(text, header):
        return header, [], note
    items = parse_line_items(text)
    facts: list[ReplyFact] = []
    po = header.get("po_number", "")
    for it in items:
        snip = f"{it['qty']:g} x {it['desc']} @ ${it['unit']:g}"
        facts.append(ReplyFact(
            source_line=snip, item=it["desc"], unit_price=it["unit"], unit="ea",
            ext_price=it.get("ext"), status="ordered", confidence=0.8,
            po_number=po, direction="sell"))
    return header, facts, note


def mine_customer_records(records: list[dict], store, attachments_dir: str,
                          limit: Optional[int] = None) -> list:
    """Build sell-tagged customer records from raw sales-inbox email dicts.

    For each email classified as a customer, mine its attachments (POs/quotes)
    into sell-priced line items.  The email body is a cover note, so we do NOT
    mine it for facts -- the data is in the attachments.  Records with no usable
    attachment data are skipped (so cover-note-only mail doesn't add noise).

    Returns VendorReplyRecord objects with counterparty_type='customer'.
    """
    import os
    from datetime import datetime
    from .reply_miner import (VendorReplyRecord, classify_counterparty,
                              _customer_match, _source_key, ReplyMessage,
                              _company_name_from_domain, _domain)

    out = []
    seen: set[str] = set()
    for r in records:
        frm = r.get("from", "")
        name = r.get("from_name", "")
        if classify_counterparty(store, frm, name, unknown_default="customer") != "customer":
            continue
        atts = r.get("attachments") or []
        if not atts:
            continue
        header, facts = {}, []
        for att in atts:
            path = os.path.join(attachments_dir, os.path.basename(att))
            if not os.path.exists(path):
                continue
            try:
                h, fs, _ = mine_customer_document(path, r.get("subject", ""))
            except Exception:
                continue
            facts.extend(fs)
            if any(h.values()) and not header.get("po_number"):
                header = h
        if not facts and not header.get("po_number"):
            continue                      # cover note only -> skip

        # customer identity: a registered name if we have one, else the company
        # from the email domain (searchable), not the individual sender's name
        cust = _customer_match(store, frm, name)
        if not cust:
            dom = _domain(frm)
            cust = _company_name_from_domain(dom) if dom else (name or frm)
        when = None
        if r.get("when"):
            try:
                when = datetime.fromisoformat(r["when"])
            except ValueError:
                when = None
        msg = ReplyMessage(from_email=frm, from_display_name=name,
                           subject=r.get("subject", ""), body="", when=when,
                           message_id=r.get("message_id", ""))
        key = _source_key(msg)
        if key in seen:
            continue
        seen.add(key)
        subj = r.get("subject", "")
        if header.get("po_number"):
            subj = f"PO {header['po_number']} - {subj}"[:160]
        items_list = [f.item for f in facts][:40]
        rec = VendorReplyRecord(
            source_key=key, vendor_id="", vendor_name=cust,
            from_email=frm, from_name=name, subject=subj, when=when,
            body_excerpt=(f"quote {header.get('quote_number','')} "
                          f"total {header.get('total','')} job {header.get('job','')}").strip(),
            items=items_list, facts=facts,
            quote_status="ordered" if facts else "info",
            confidence=0.8 if facts else 0.4, counterparty_type="customer")
        out.append(rec)
        if limit and len(out) >= limit:
            break
    return out
