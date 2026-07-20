"""
Export Sent Items from classic Outlook (COM) to JSON for the corpus miner.

Run this ON YOUR DESKTOP where Outlook is installed (same machine as MaINbox):

    py -m pip install pywin32
    py export_sent_outlook.py                  # last 12 months -> sent_export.json
    py export_sent_outlook.py 24               # last 24 months
    py export_sent_outlook.py 12 out.json      # custom output path
    py export_sent_outlook.py 12 sent_export.json --mailbox sales@americanpoweresc.com
                                               # read a SHARED mailbox's Sent Items

Then feed it to the miner (no Graph, no admin consent):

    py -m mainbox_brain.corpus sent_export.json
    py -m mainbox_brain.corpus sent_export.json --ask

Notes:
  - Read-only: it only reads Sent Items, writes one JSON file.
  - Bodies are truncated to 4000 chars (plenty for classification).
  - Familiar territory: this is the same Outlook COM surface MaINbox uses,
    minus all the Cached-Exchange identity headaches -- we never store
    EntryIDs, just addresses/subjects/bodies/dates.
  - SMTP resolution: for Exchange senders the To recipient usually carries a
    resolvable SMTP address; where only a LegacyExchangeDN is available we
    try PropertyAccessor PR_SMTP_ADDRESS, else skip the recipient.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta

OL_SENT_FOLDER = 5          # olFolderSentMail
OL_INBOX_FOLDER = 6         # olFolderInbox
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
BODY_LIMIT = 4000


def open_shared_folder(ns, mailbox: str, folder_type: int):
    """Open a shared mailbox's default Inbox/Sent via GetSharedDefaultFolder.

    `mailbox` is the shared address, e.g. "sales@americanpoweresc.com".
    Requires that you have access (delegate or full-access) to that mailbox.
    """
    recip = ns.CreateRecipient(mailbox)
    recip.Resolve()
    if not recip.Resolved:
        raise RuntimeError(
            f"Could not resolve shared mailbox {mailbox!r}. Check the address "
            f"and that your account has access to it in Outlook.")
    return ns.GetSharedDefaultFolder(recip, folder_type)


def smtp_of(recipient) -> str:
    """Best-effort SMTP for a recipient (handles Exchange EX-type addresses)."""
    try:
        addr = recipient.Address or ""
    except Exception:
        return ""
    if "@" in addr:
        return addr
    # EX / LegacyExchangeDN -> resolve via PropertyAccessor or AddressEntry
    try:
        ae = recipient.AddressEntry
        try:
            smtp = ae.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
            if smtp and "@" in smtp:
                return smtp
        except Exception:
            pass
        try:
            ex_user = ae.GetExchangeUser()
            if ex_user and ex_user.PrimarySmtpAddress:
                return ex_user.PrimarySmtpAddress
        except Exception:
            pass
    except Exception:
        pass
    return ""


def main() -> None:
    argv = sys.argv[1:]
    mailbox = ""
    if "--mailbox" in argv:
        i = argv.index("--mailbox")
        mailbox = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    months = int(argv[0]) if len(argv) > 0 else 12
    out_path = argv[1] if len(argv) > 1 else "sent_export.json"

    try:
        import win32com.client  # type: ignore
    except ImportError:
        print("pywin32 not installed. Run:  py -m pip install pywin32")
        sys.exit(1)

    outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")
    if mailbox:
        sent = open_shared_folder(ns, mailbox, OL_SENT_FOLDER)
        print(f"Reading Sent Items from shared mailbox: {mailbox}")
    else:
        sent = ns.GetDefaultFolder(OL_SENT_FOLDER)

    cutoff = datetime.now() - timedelta(days=30 * months)
    # Restrict server/store-side instead of walking everything
    flt = f"[SentOn] >= '{cutoff.strftime('%m/%d/%Y %H:%M %p')}'"
    items = sent.Items.Restrict(flt)
    items.Sort("[SentOn]", True)

    records, scanned, skipped = [], 0, 0
    for item in items:
        scanned += 1
        try:
            if getattr(item, "Class", 0) != 43:      # 43 = olMail
                continue
            when = None
            try:
                so = item.SentOn
                when = datetime(so.year, so.month, so.day, so.hour, so.minute, so.second)
            except Exception:
                pass
            body = (item.Body or "")[:BODY_LIMIT]
            subject = item.Subject or ""
            for r in item.Recipients:
                if getattr(r, "Type", 1) != 1:        # 1 = olTo (skip CC/BCC)
                    continue
                addr = smtp_of(r)
                if not addr:
                    skipped += 1
                    continue
                records.append({
                    "to": addr,
                    "to_name": getattr(r, "Name", "") or "",
                    "subject": subject,
                    "body": body,
                    "when": when.isoformat() if when else None,
                })
        except Exception:
            skipped += 1
            continue

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1)

    print(f"Scanned {scanned} item(s), wrote {len(records)} recipient record(s) "
          f"to {out_path} ({skipped} skipped/unresolvable).")
    print(f"Next:  py -m mainbox_brain.corpus {out_path}")


if __name__ == "__main__":
    main()
