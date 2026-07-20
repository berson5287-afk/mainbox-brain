"""
Export received vendor replies from classic Outlook (COM) to JSON for reply mining.

Run this on the Windows desktop where Outlook/MaINbox runs:

    py -m pip install pywin32

Recommended vendor-filtered export:

    py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db

Then mine the replies into the brain database:

    py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db

Notes:
  - Read-only: it only reads mail and writes a JSON export.
  - By default, this scans your Inbox for the requested time range.
  - With --vendors-db, it exports only messages from known vendor emails/domains
    found in your Brain database contacts/records tables.
  - Bodies are truncated to BODY_LIMIT characters; the miner only needs the
    sender's top reply, price/stock/ETA lines, and subject.
  - To scan a different folder, use --folder "Sales\\Inbox".
  - Use --all-senders only for troubleshooting; it disables vendor filtering.
"""
from __future__ import annotations
__version__ = "0.49"

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

OL_INBOX_FOLDER = 6        # olFolderInbox
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
BODY_LIMIT = 8000
_PUBLIC_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
    "msn.com", "live.com", "comcast.net", "verizon.net", "optonline.net", "me.com",
}


def smtp_of_sender(item) -> str:
    """Best-effort SMTP for sender, including Exchange EX senders."""
    try:
        addr = item.SenderEmailAddress or ""
        if "@" in addr:
            return addr.lower()
    except Exception:
        pass
    try:
        sender = item.Sender
        if sender is not None:
            try:
                smtp = sender.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
                if smtp and "@" in smtp:
                    return smtp.lower()
            except Exception:
                pass
            try:
                ex_user = sender.GetExchangeUser()
                if ex_user and ex_user.PrimarySmtpAddress:
                    return ex_user.PrimarySmtpAddress.lower()
            except Exception:
                pass
    except Exception:
        pass
    return ""


def domain_of(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def load_vendor_senders(db_path: str | Path) -> tuple[set[str], set[str]]:
    """Load known vendor emails and vendor domains from mainbox.db.

    Exact emails come from contacts and sent RFQ records. Domains are used as a
    safe backup for vendor employees you emailed before under the same company
    domain. Public mailbox domains are intentionally excluded from domain-level
    matching.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Vendor DB not found: {path}")
    emails: set[str] = set()
    domains: set[str] = set()
    con = sqlite3.connect(str(path))
    try:
        for table, col in (("contacts", "email"), ("records", "to_email")):
            try:
                rows = con.execute(f"SELECT DISTINCT lower({col}) FROM {table}")
            except sqlite3.OperationalError:
                continue
            for (email,) in rows:
                email = (email or "").strip().lower()
                if "@" not in email:
                    continue
                emails.add(email)
                dom = domain_of(email)
                if dom and dom not in _PUBLIC_DOMAINS:
                    domains.add(dom)
    finally:
        con.close()
    return emails, domains


def is_known_vendor_sender(email: str, vendor_emails: set[str], vendor_domains: set[str]) -> bool:
    email = (email or "").strip().lower()
    if email in vendor_emails:
        return True
    dom = domain_of(email)
    return bool(dom and dom not in _PUBLIC_DOMAINS and dom in vendor_domains)


def get_folder(ns, folder_path: str):
    """Resolve a backslash-separated Outlook folder path.

    Examples: "Sales\\Inbox", "Mailbox - Steve\\Inbox", "Vendor Quotes".
    If omitted, caller uses the default Inbox.
    """
    parts = [p for p in (folder_path or "").split("\\") if p]
    if not parts:
        return ns.GetDefaultFolder(OL_INBOX_FOLDER)
    folders = ns.Folders
    current = None
    for idx, part in enumerate(parts):
        found = None
        search_in = folders if current is None else current.Folders
        for f in search_in:
            if (f.Name or "").lower() == part.lower():
                found = f
                break
        if found is None:
            raise RuntimeError(f"Could not find Outlook folder path part {idx + 1}: {part!r}")
        current = found
    return current


def open_shared_folder(ns, mailbox: str, folder_type: int):
    """Open a shared mailbox's default Inbox via GetSharedDefaultFolder.

    `mailbox` is the shared address, e.g. "sales@americanpoweresc.com".
    Requires that your account has access (delegate or full-access) to it.
    """
    recip = ns.CreateRecipient(mailbox)
    recip.Resolve()
    if not recip.Resolved:
        raise RuntimeError(
            f"Could not resolve shared mailbox {mailbox!r}. Check the address "
            f"and that your account has access to it in Outlook.")
    return ns.GetSharedDefaultFolder(recip, folder_type)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export received Outlook replies for MaINbox Brain reply mining.")
    parser.add_argument("months", nargs="?", type=int, default=12, help="Months of received mail to scan. Default: 12")
    parser.add_argument("out_path", nargs="?", default="received_export.json", help="Output JSON path. Default: received_export.json")
    # Backward compatibility: older command used the 3rd positional arg as folder.
    parser.add_argument("legacy_folder", nargs="?", default="", help=argparse.SUPPRESS)
    parser.add_argument("--folder", default="", help=r'Outlook folder path, e.g. "Sales\Inbox"')
    parser.add_argument("--mailbox", default="",
                        help="Shared mailbox address to read the Inbox of, "
                             "e.g. sales@americanpoweresc.com")
    parser.add_argument("--vendors-db", "--db", dest="vendors_db", default="mainbox.db",
                        help="mainbox.db path used to filter to known vendor senders. Default: mainbox.db")
    parser.add_argument("--all-senders", action="store_true",
                        help="Disable vendor filtering and export every sender in the folder/time range.")
    parser.add_argument("--body-limit", type=int, default=BODY_LIMIT, help=f"Body character limit. Default: {BODY_LIMIT}")
    parser.add_argument("--attachments", dest="attachments_dir", default="",
                        help="Save PDF/Excel/CSV/Word quote attachments into this folder for the attachment miner.")
    parser.add_argument("--min-attachment-kb", type=int, default=8,
                        help="Skip attachments smaller than this (drops signature logos/icons). Default: 8")
    parser.add_argument("--keep-images", action="store_true",
                        help="Also save image attachments (png/jpg/tif) for OCR via SmartScan.")
    return parser.parse_args(argv)


_DOC_EXTS = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".docx", ".doc"}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp"}


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "attachment").strip("_")
    return name[:80] or "attachment"


def save_attachments(item, dest_dir: Path, key: str, min_kb: int, keep_images: bool) -> list:
    """Save relevant attachments; return paths relative to dest_dir.

    Read-only on the mailbox (SaveAsFile copies out).  Skips inline signature
    logos and icons by extension and a small size floor.
    """
    import os as _os
    saved = []
    try:
        atts = item.Attachments
        count = atts.Count
    except Exception:
        return saved
    allowed = _DOC_EXTS | (_IMG_EXTS if keep_images else set())
    for i in range(1, count + 1):
        try:
            att = atts.Item(i)
            fname = getattr(att, "FileName", "") or ""
            ext = _os.path.splitext(fname)[1].lower()
            if ext not in allowed:
                continue
            try:
                if ext not in _DOC_EXTS and int(getattr(att, "Size", 0)) < min_kb * 1024:
                    continue  # tiny image -> almost certainly a signature/icon
            except Exception:
                pass
            rel = f"{key[:12]}__{i:02d}__{_safe_name(fname)}"
            att.SaveAsFile(str((dest_dir / rel).resolve()))
            saved.append(rel)
        except Exception:
            continue
    return saved


def main() -> None:
    ns_args = parse_args(sys.argv[1:])
    months = ns_args.months
    out_path = ns_args.out_path
    folder_path = ns_args.folder or ns_args.legacy_folder or ""
    body_limit = max(500, int(ns_args.body_limit or BODY_LIMIT))
    attachments_dir = Path(ns_args.attachments_dir) if ns_args.attachments_dir else None
    if attachments_dir is not None:
        attachments_dir.mkdir(parents=True, exist_ok=True)
    saved_attachments = 0

    vendor_emails: set[str] = set()
    vendor_domains: set[str] = set()
    vendor_filter_enabled = not ns_args.all_senders
    if vendor_filter_enabled:
        try:
            vendor_emails, vendor_domains = load_vendor_senders(ns_args.vendors_db)
        except Exception as exc:
            print(f"Could not load vendor filter from {ns_args.vendors_db!r}: {exc}")
            print("Use --all-senders to intentionally export every received email, or fix the --vendors-db path.")
            sys.exit(2)
        if not vendor_emails and not vendor_domains:
            print(f"No vendor emails/domains found in {ns_args.vendors_db!r}.")
            print("Run the sent-history miner first, or use --all-senders for a broad export.")
            sys.exit(2)

    try:
        import win32com.client  # type: ignore
    except ImportError:
        print("pywin32 not installed. Run:  py -m pip install pywin32")
        sys.exit(1)

    outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")
    if ns_args.mailbox:
        folder = open_shared_folder(ns, ns_args.mailbox, OL_INBOX_FOLDER)
        if folder_path:        # optional subfolder under the shared Inbox
            for part in [p for p in folder_path.split("\\") if p]:
                folder = next((f for f in folder.Folders
                               if (f.Name or "").lower() == part.lower()), None)
                if folder is None:
                    raise RuntimeError(f"Subfolder {part!r} not found under {ns_args.mailbox}")
        print(f"Reading Inbox from shared mailbox: {ns_args.mailbox}")
    else:
        folder = get_folder(ns, folder_path)

    cutoff = datetime.now() - timedelta(days=30 * months)
    flt = f"[ReceivedTime] >= '{cutoff.strftime('%m/%d/%Y %H:%M %p')}'"
    items = folder.Items.Restrict(flt)
    items.Sort("[ReceivedTime]", True)

    records, scanned, skipped, non_mail, filtered = [], 0, 0, 0, 0
    for item in items:
        scanned += 1
        try:
            if getattr(item, "Class", 0) != 43:      # 43 = olMail
                non_mail += 1
                continue
            when = None
            try:
                rt = item.ReceivedTime
                when = datetime(rt.year, rt.month, rt.day, rt.hour, rt.minute, rt.second)
            except Exception:
                pass
            frm = smtp_of_sender(item)
            if not frm:
                skipped += 1
                continue
            # v0.49: vendor announcements (price increases etc.) are often
            # FORWARDED by colleagues, so the vendor-sender filter would drop
            # exactly the emails that warn about money. Announcement-looking
            # subjects always pass.
            subj_l = (item.Subject or "").lower()
            is_announcement = bool(re.search(
                r"price\s+(?:increase|adjustment|change|revision)|surcharge|"
                r"new\s+pricing|rate\s+increase", subj_l))
            if (vendor_filter_enabled and not is_announcement
                    and not is_known_vendor_sender(frm, vendor_emails, vendor_domains)):
                filtered += 1
                continue
            rec = {
                "from": frm,
                "from_name": getattr(item, "SenderName", "") or "",
                "subject": item.Subject or "",
                "body": (item.Body or "")[:body_limit],
                "when": when.isoformat() if when else None,
                "message_id": getattr(item, "EntryID", "") or "",
            }
            if attachments_dir is not None:
                files = save_attachments(item, attachments_dir, rec["message_id"] or frm,
                                         ns_args.min_attachment_kb, ns_args.keep_images)
                if files:
                    rec["attachments"] = files
                    saved_attachments += len(files)
            records.append(rec)
        except Exception:
            skipped += 1
            continue

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1)

    mode = "known vendor senders only" if vendor_filter_enabled else "all senders"
    print(f"Scanned {scanned} item(s) in {folder.Name!r}; export mode: {mode}.")
    if vendor_filter_enabled:
        print(f"Vendor filter loaded {len(vendor_emails)} email(s) and {len(vendor_domains)} domain(s) from {ns_args.vendors_db!r}.")
    print(f"Wrote {len(records)} received record(s) to {out_path} "
          f"({filtered} filtered as non-vendor, {skipped} skipped/unresolvable, {non_mail} non-mail).")
    if attachments_dir is not None:
        print(f"Saved {saved_attachments} attachment file(s) to {attachments_dir}/.")
        print(f"Next:  py -m mainbox_brain.reply_corpus {out_path} --db {ns_args.vendors_db} --attachments {attachments_dir}")
    else:
        print(f"Next:  py -m mainbox_brain.reply_corpus {out_path} --db {ns_args.vendors_db}")
        print("Tip: add  --attachments quote_files  to also capture PDF/Excel quotes.")


if __name__ == "__main__":
    main()
