"""
Load and mine vendor reply exports.

Typical desktop flow:

    py export_replies_outlook.py 12 received_export.json
    py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db

Default behavior is vendor-only: messages are mined only when the sender matches
your learned vendor list from mainbox.db by exact email or known company domain.
Use --all-senders only for troubleshooting.
    py -m mainbox_brain.ask --db mainbox.db

JSON record shape (list or JSONL):
    {"from": "mark@brazill.com", "from_name": "Mark", "subject": "RE: RFQ",
     "body": "12/2 MC is $0.72/ft, in stock", "when": "2026-06-10T10:00:00"}
"""
from __future__ import annotations
__version__ = "0.51"

import json
import sys
from datetime import datetime
from pathlib import Path

from .reply_miner import ReplyMessage, mine_replies, _source_key
from .store import Store, DEFAULT_DB


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _from_record(rec: dict) -> ReplyMessage | None:
    frm = (rec.get("from") or rec.get("from_email") or rec.get("sender") or "").strip()
    if not frm or "@" not in frm:
        return None
    return ReplyMessage(
        from_email=frm,
        from_display_name=rec.get("from_name", "") or rec.get("from_display_name", "") or rec.get("sender_name", ""),
        subject=rec.get("subject", "") or "",
        body=rec.get("body", "") or "",
        when=_parse_when(rec.get("when") or rec.get("received") or rec.get("date")),
        message_id=rec.get("message_id", "") or rec.get("id", ""),
    )


def _read_records(path: str | Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    if text.lstrip().startswith("["):
        return json.loads(text)
    recs = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            recs.append(json.loads(line))
    return recs


def load_json(path: str | Path) -> list[ReplyMessage]:
    out = []
    for rec in _read_records(path):
        msg = _from_record(rec)
        if msg:
            out.append(msg)
    return out


def _raw_by_source_key(path: str | Path) -> dict[str, dict]:
    """Map each export record to the source_key mine_replies will assign it,
    so attachment lists can be reattached to the mined VendorReplyRecord."""
    index: dict[str, dict] = {}
    for rec in _read_records(path):
        msg = _from_record(rec)
        if msg is None:
            continue
        index[_source_key(msg)] = rec
    return index


def enrich_with_attachments(replies, raw_index, base_dir, store=None, vendor_only=True):
    """Mine attachments and merge their facts into reply records.

    Attachments are first-class: a vendor email whose body had nothing mineable
    but carries a real PDF/Excel quote still becomes a record here.
    Returns (n_enriched, n_attachment_facts, n_created, notes)."""
    from .attachment_miner import mine_record_attachments
    from .reply_miner import (is_known_vendor_sender, _vendor_for_sender,
                              VendorReplyRecord)
    by_key = {r.source_key: r for r in replies}
    enriched = added = created = 0
    notes = []
    for key, rec in raw_index.items():
        if not rec.get("attachments"):
            continue
        facts, file_notes = mine_record_attachments(rec, base_dir)
        notes.extend(file_notes)
        if not facts:
            continue
        rep = by_key.get(key)
        if rep is None:
            frm = (rec.get("from") or rec.get("from_email") or "").strip().lower()
            if vendor_only and store is not None and not is_known_vendor_sender(store, frm):
                continue
            vid, vname = (_vendor_for_sender(store, frm, rec.get("from_name", ""))
                          if store else (frm, rec.get("from_name") or frm))
            msg = _from_record(rec)
            rep = VendorReplyRecord(
                source_key=key, vendor_id=vid, vendor_name=vname, from_email=frm,
                from_name=rec.get("from_name", ""), subject=rec.get("subject", ""),
                when=msg.when if msg else None, body_excerpt="(attachment-only quote)",
                items=[], facts=[])
            replies.append(rep)
            by_key[key] = rep
            created += 1
        rep.facts.extend(facts)
        added += len(facts)
        enriched += 1
        statuses = [f.status for f in rep.facts]
        rep.quote_status = ("quoted" if "quoted" in statuses else
                            "alternate" if "alternate" in statuses else
                            "no_quote" if "no_quote" in statuses else "info")
        rep.confidence = max([f.confidence for f in rep.facts] or [0.2])
    return enriched, added, created, notes


def load_folder(path: str | Path) -> list[ReplyMessage]:
    out: list[ReplyMessage] = []
    for f in sorted(Path(path).glob("*.txt")):
        lines = f.read_text(encoding="utf-8-sig").splitlines()
        frm, subject, body_start = "", "", 0
        for i, line in enumerate(lines[:6]):
            low = line.lower()
            if low.startswith("from:"):
                frm = line.split(":", 1)[1].strip()
                body_start = max(body_start, i + 1)
            elif low.startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
                body_start = max(body_start, i + 1)
        if frm:
            out.append(ReplyMessage(from_email=frm, subject=subject,
                                    body="\n".join(lines[body_start:]).strip()))
    return out


def load_any(path: str | Path) -> list[ReplyMessage]:
    p = Path(path)
    if p.is_dir():
        return load_folder(p)
    if p.suffix.lower() in {".json", ".jsonl"}:
        return load_json(p)
    raise ValueError(f"Don't know how to load {p} (expected folder, .json, or .jsonl)")


def report(replies) -> None:
    statuses: dict[str, int] = {}
    fact_count = 0
    for r in replies:
        statuses[r.quote_status] = statuses.get(r.quote_status, 0) + 1
        fact_count += len(r.facts)
    print(f"Mined {len(replies)} vendor reply record(s), {fact_count} evidence fact(s).")
    if statuses:
        print("Status mix: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    for r in replies[:10]:
        print(f"  • [{r.when.date() if r.when else 'date unknown'}] {r.vendor_name} <{r.from_email}> "
              f"{r.quote_status} facts={len(r.facts)} subject={r.subject[:70]!r}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    db_path = DEFAULT_DB
    if "--db" in sys.argv:
        db_path = sys.argv[sys.argv.index("--db") + 1]
    replace = "--replace" in sys.argv

    msgs = load_any(args[0])
    print(f"Loaded {len(msgs)} received message(s) from {args[0]}")
    store = Store(db_path)
    vendor_only = "--all-senders" not in sys.argv
    replies = mine_replies(msgs, store=store, vendor_only=vendor_only)
    skipped = max(0, len(msgs) - len(replies))

    # --attachments DIR mines saved PDF/Excel/CSV/Word quotes too.  Default base
    # dir is the export file's folder (where the exporter saves attachments).
    if "--attachments" in sys.argv or any(
            (r.get("attachments") for r in _read_records(args[0]))):
        if "--attachments" in sys.argv:
            base_dir = sys.argv[sys.argv.index("--attachments") + 1]
        else:
            base_dir = str(Path(args[0]).resolve().parent)
        raw_index = _raw_by_source_key(args[0])
        n_rec, n_facts, n_new, notes = enrich_with_attachments(
            replies, raw_index, base_dir, store=store, vendor_only=vendor_only)
        extra = f" ({n_new} from attachment-only emails)" if n_new else ""
        print(f"Attachments: mined {n_facts} fact(s) from {n_rec} record(s) with files{extra}.")
        # v0.47: bucket the notes correctly -- a missing LIBRARY was previously
        # lumped with missing FILES, hiding 'pip install pypdf' behind a
        # misleading "files not found" line.
        dep = [n for n in notes if "install" in n]
        ocr = [n for n in notes if "ocr_needed" in n]
        miss = [n for n in notes if "missing file" in n]
        if dep:
            hints = sorted({n.split(": ", 1)[-1] for n in dep})
            print(f"  WARNING: {len(dep)} attachment(s) NOT read — extractor "
                  f"library missing:")
            for h in hints[:3]:
                print(f"    {h}")
        if ocr:
            print(f"  {len(ocr)} attachment(s) look scanned/image -> route to SmartScan/OCR (skipped here).")
        if miss:
            print(f"  {len(miss)} listed attachment file(s) not found under {base_dir!r}.")
        # v0.51: priced documents nobody could parse -- the data is THERE but
        # unread. Named loudly so new vendor layouts get taught, not lost.
        unparsed = [n for n in notes if "unparsed_priced_doc" in n]
        if unparsed:
            names = sorted({n.split(":", 1)[0] for n in unparsed})
            print(f"  ATTENTION: {len(unparsed)} priced document(s) in a layout "
                  f"I can't parse yet: {', '.join(names[:3])}"
                  + (" ..." if len(names) > 3 else ""))
            print("    Teach it: py -m mainbox_brain.attachment_miner <that file> "
                  "and share the output.")
    if vendor_only:
        print(f"Vendor-only mode ON: skipped {skipped} message(s) that were not from your learned vendor list or had no mineable facts/items.")
        print("Use --all-senders only for troubleshooting or one-time broad mining.")
    # v0.49: sweep the raw export for vendor announcements (price increases,
    # surcharges) -- these carry no quote facts, so the fact miner rightly
    # skips them, but the WARNING is business-critical and now gets stored.
    try:
        from . import announcements
        n_ann = announcements.mine_records(_read_records(args[0]), db_path)
        if n_ann:
            print(f"Announcements: stored {n_ann} new vendor notice(s) "
                  f"(price increases / pricing changes).")
    except Exception as exc:
        print(f"  (announcement sweep skipped: {exc})")
    saved = store.save_reply_records(replies, replace=replace)
    print(f"Saved {saved} reply record(s) to {db_path} (total={store.reply_count()})")
    report(replies)

    if "--find" in sys.argv:
        i = sys.argv.index("--find")
        query = " ".join(sys.argv[i + 1:]).strip()
        print(f"\nMatches for {query!r}:")
        for h in store.find_replies(query):
            print(f"  [{h.get('when')}] {h.get('vendor')} <{h.get('from')}> | {h.get('line')}")


if __name__ == "__main__":
    main()
