#!/usr/bin/env python3
"""
outlook_rfq_sender.py  -  companion for MaINbox Voice RFQs.
v0.1.0

Run this on the PC that has Outlook (if that's not the PC running the voice
server). It watches the rfq_queue/ folder for RFQs the voice server queued
(status "queued"), sends each via Outlook COM, and flips its status to "sent"
in both the queue JSON and the coverage handoff JSON.

If the voice server runs ON the Outlook PC you don't need this script at all —
the server sends directly and RFQs never sit in "queued".

Usage:
    python outlook_rfq_sender.py                 # watch ./rfq_queue every 15s
    python outlook_rfq_sender.py --dir "C:\\path\\to\\rfq_queue" --interval 30
    python outlook_rfq_sender.py --once          # single pass, then exit

Requires: pywin32 (`pip install pywin32`) and Outlook signed in.
"""

from __future__ import annotations

import os
import json
import time
import argparse

__version__ = "0.3.0"  # creates Outlook drafts for voice drafts; sends released drafts by EntryID (no duplicates)


def _rollup(vendors):
    st = {v.get("status") for v in vendors}
    if st == {"draft"}:                   # v0.3.0: mirror server exactly
        return "draft"
    if "draft" in st or "queued" in st:
        return "queued" if st <= {"draft", "queued"} else "partial"
    if st == {"replied"}:
        return "replied"
    return "sent"


def _send_outlook(to_addr: str, subject: str, body: str):
    import win32com.client  # noqa: PLC0415  (import here so --help works w/o pywin32)
    ol = win32com.client.Dispatch("Outlook.Application")
    mail = ol.CreateItem(0)
    mail.To = to_addr
    mail.Subject = subject
    mail.Body = body
    mail.Send()


def _draft_outlook(to_addr: str, subject: str, body: str) -> str:
    """v0.3.0: create a draft in Outlook's Drafts folder (Save, never Send).
    Returns the EntryID so a later release can send THAT item."""
    import win32com.client  # noqa: PLC0415
    ol = win32com.client.Dispatch("Outlook.Application")
    mail = ol.CreateItem(0)
    mail.To = to_addr
    mail.Subject = subject
    mail.Body = body
    mail.Save()
    return str(mail.EntryID or "")


def _send_by_entry_id(entry_id: str) -> bool:
    """Send the existing Outlook draft; True on success."""
    import win32com.client  # noqa: PLC0415
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = ol.GetNamespace("MAPI")
        ns.GetItemFromID(entry_id).Send()
        return True
    except Exception:  # noqa: BLE001
        return False


def _flip_coverage(rfq_dir: str, ref: str, status: str):
    cov = os.path.join(rfq_dir, "coverage", f"RFQ_{ref}_coverage.json")
    if not os.path.isfile(cov):
        return
    try:
        with open(cov, encoding="utf-8") as f:
            c = json.load(f)
        c["status"] = status
        with open(cov, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"    (coverage update failed for {ref}: {e})")


def process_once(rfq_dir: str) -> int:
    """Send every queued vendor on every RFQ found. Returns emails sent."""
    if not os.path.isdir(rfq_dir):
        print(f"  queue folder not found: {rfq_dir}")
        return 0
    sent = 0
    for name in sorted(os.listdir(rfq_dir)):
        if not (name.startswith("RFQ_") and name.endswith(".json")):
            continue
        path = os.path.join(rfq_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                rfq = json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {name}: unreadable ({e})")
            continue

        subj = rfq.get("email_subject", f"RFQ {rfq.get('ref', name)}")
        body = rfq.get("email_body", "")
        ref = rfq.get("ref", name)
        changed = False

        if rfq.get("schema", 1) >= 2:
            # ---- schema 2: per-vendor; one failure never blocks the rest ---
            for v in rfq.get("vendors", []):
                to = v.get("email", "")
                # v0.3.0: voice DRAFTS get a real Outlook draft (Save, never
                # Send) so they can be reviewed/sent from Outlook itself.
                if v.get("status") == "draft" and not v.get("outlook_entry_id"):
                    print(f"  {ref} -> {to} (draft) ...", end=" ", flush=True)
                    try:
                        eid = _draft_outlook(to, subj, body)
                        v["outlook_entry_id"] = eid
                        v["detail"] = "draft in Outlook Drafts (companion)"
                        rfq.setdefault("timeline", []).append(
                            {"ts": time.time(), "event": "outlook_draft",
                             "vendor": to,
                             "detail": "created by companion"})
                        print("draft created.")
                        changed = True
                    except Exception as e:  # noqa: BLE001
                        print(f"draft FAILED: {e}")
                    continue
                if v.get("status") != "queued":
                    continue
                print(f"  {ref} -> {to} ...", end=" ", flush=True)
                # released voice draft: send the EXISTING Outlook draft so we
                # never duplicate the mail; fall back to a fresh compose
                eid = v.get("outlook_entry_id") or ""
                sent_ok = bool(eid) and _send_by_entry_id(eid)
                how = "sent the Outlook draft (companion)" if sent_ok else ""
                if not sent_ok:
                    try:
                        _send_outlook(to, subj, body)
                        how = "sent via Outlook (companion)"
                        sent_ok = True
                    except Exception as e:  # noqa: BLE001
                        print(f"FAILED: {e}")
                        v["detail"] = f"outlook send failed: {e}"
                        rfq.setdefault("timeline", []).append(
                            {"ts": time.time(), "event": "send_failed",
                             "vendor": to, "detail": str(e)[:200]})
                        changed = True
                        continue
                v["status"], v["sent_ts"] = "sent", time.time()
                v["detail"] = how
                rfq.setdefault("timeline", []).append(
                    {"ts": time.time(), "event": "sent", "vendor": to,
                     "detail": how})
                print("sent.")
                sent += 1
                changed = True
            new_status = _rollup(rfq.get("vendors", []))
            if new_status != rfq.get("status"):
                rfq["status"] = new_status
                changed = True
        else:
            # ---- legacy single-vendor record (pre-schema-2) ---------------
            if rfq.get("status") != "queued":
                continue
            to = rfq.get("vendor_email", "")
            print(f"  {ref} -> {to} ...", end=" ", flush=True)
            try:
                _send_outlook(to, subj, body)
            except Exception as e:  # noqa: BLE001
                print(f"FAILED: {e}")
                rfq["send_detail"] = f"outlook send failed: {e}"
                changed = True
            else:
                rfq["status"] = "sent"
                rfq["sent_ts"] = time.time()
                rfq["send_detail"] = "sent via Outlook (companion)"
                print("sent.")
                sent += 1
                changed = True

        if changed:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rfq, f, indent=2)
            except OSError as e:
                print(f"  couldn't update {name}: {e}")
                continue
            _flip_coverage(rfq_dir, ref, rfq.get("status", "sent"))
    return sent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send queued MaINbox Voice RFQs "
                                             "via Outlook")
    ap.add_argument("--dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "rfq_queue"),
        help="rfq_queue folder (default: ./rfq_queue next to this script)")
    ap.add_argument("--interval", type=int, default=15,
                    help="seconds between passes (default 15)")
    ap.add_argument("--once", action="store_true",
                    help="single pass, then exit")
    args = ap.parse_args(argv)

    try:
        import win32com.client  # noqa: F401
    except ImportError:
        print("pywin32 is not installed. Run:  pip install pywin32")
        return 1

    print(f"outlook_rfq_sender v{__version__} watching {args.dir} "
          f"(every {args.interval}s){' — single pass' if args.once else ''}")
    while True:
        n = process_once(args.dir)
        if n:
            print(f"  pass complete: {n} sent")
        if args.once:
            break
        time.sleep(max(3, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
