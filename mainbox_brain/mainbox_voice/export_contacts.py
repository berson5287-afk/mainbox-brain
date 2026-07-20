"""
export_contacts.py — v1.0
Dump Outlook contacts + everyone you've actually emailed to contacts.json,
so MaINbox Voice can resolve names like "nick at hubbell" to
ndattilo@hubbell.com.

Run this ON THE OUTLOOK PC (needs Outlook + pywin32):

    python export_contacts.py

It writes contacts.json NEXT TO THIS SCRIPT. Put this script in your
mainbox_voice folder (or copy the produced contacts.json there) — the voice
server auto-loads mainbox_voice\\contacts.json on every lookup, no restart
needed. Override the location with the MBB_CONTACTS environment variable.

Sources, in order:
  1. Your Outlook Contacts folder (names, emails, companies)
  2. Recipients of your Sent Items from the last 18 months — this captures
     people like vendors' inside sales reps who were never saved as contacts
Exchange (EX) addresses are resolved to real SMTP where possible.

Re-run any time; it overwrites contacts.json with a fresh export.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "contacts.json")
SENT_LOOKBACK_DAYS = 540          # ~18 months
SENT_SCAN_CAP = 4000              # newest N sent messages scanned
SKIP_LOCAL_RE = re.compile(r"no[-_.]?reply|donotreply|mailer-daemon|postmaster",
                           re.IGNORECASE)


def _smtp_of(entry):
    """Best-effort SMTP address from an Outlook AddressEntry/Recipient."""
    try:
        addr = entry.Address or ""
    except Exception:
        addr = ""
    if "@" in addr:
        return addr
    # Exchange DN — resolve to primary SMTP
    try:
        ae = entry.AddressEntry if hasattr(entry, "AddressEntry") else entry
        exu = ae.GetExchangeUser()
        if exu and exu.PrimarySmtpAddress:
            return exu.PrimarySmtpAddress
    except Exception:
        pass
    try:  # PR_SMTP_ADDRESS property fallback
        PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
        ae = entry.AddressEntry if hasattr(entry, "AddressEntry") else entry
        v = ae.PropertyAccessor.GetProperty(PR_SMTP)
        if v and "@" in v:
            return v
    except Exception:
        pass
    return ""


def _split_name(display):
    """'Dattilo, Nicholas' or 'Nicholas Dattilo' -> (first, last)."""
    d = (display or "").strip()
    if "," in d:
        last, _, first = d.partition(",")
        return first.strip().split(" ")[0], last.strip()
    parts = d.split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return d, ""


def main():
    try:
        import win32com.client  # type: ignore
    except ImportError:
        print("pywin32 is required:  pip install pywin32")
        sys.exit(1)

    print("Connecting to Outlook...")
    ol = win32com.client.Dispatch("Outlook.Application")
    ns = ol.GetNamespace("MAPI")

    people = {}   # email(lower) -> record; first writer wins name/company

    def add(email, name="", company=""):
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            return
        if SKIP_LOCAL_RE.search(email.split("@")[0]):
            return
        first, last = _split_name(name)
        rec = people.get(email)
        if rec is None:
            people[email] = {"name": (name or "").strip(),
                             "first": first, "last": last,
                             "email": email,
                             "company": (company or "").strip()}
        else:
            # fill blanks from later sources, never overwrite
            if not rec["name"] and name:
                rec["name"], rec["first"], rec["last"] = name.strip(), first, last
            if not rec["company"] and company:
                rec["company"] = company.strip()

    # ---- 1. Contacts folder -------------------------------------------------
    print("Reading Contacts folder...")
    n_contacts = 0
    try:
        contacts = ns.GetDefaultFolder(10)  # olFolderContacts
        for item in contacts.Items:
            try:
                if item.Class != 40:        # olContact
                    continue
                name = item.FullName or ""
                company = item.CompanyName or ""
                for attr in ("Email1Address", "Email2Address", "Email3Address"):
                    em = getattr(item, attr, "") or ""
                    if "@" not in em:       # may be EX — try display resolve
                        try:
                            em = _smtp_of(item.Recipients.Item(1))
                        except Exception:
                            em = ""
                    if em:
                        add(em, name, company)
                        n_contacts += 1
            except Exception:
                continue
    except Exception as e:
        print(f"  (couldn't read Contacts folder: {e})")
    print(f"  {n_contacts} contact addresses")

    # ---- 2. Sent Items recipients -------------------------------------------
    print(f"Scanning Sent Items recipients (last {SENT_LOOKBACK_DAYS} days,"
          f" newest {SENT_SCAN_CAP} messages)...")
    n_sent = 0
    try:
        sent = ns.GetDefaultFolder(5)       # olFolderSentMail
        items = sent.Items
        items.Sort("[SentOn]", True)        # newest first
        cutoff = datetime.now() - timedelta(days=SENT_LOOKBACK_DAYS)
        scanned = 0
        for item in items:
            if scanned >= SENT_SCAN_CAP:
                break
            scanned += 1
            try:
                when = item.SentOn
                if when and when.replace(tzinfo=None) < cutoff:
                    break                   # sorted — older from here on
                for r in item.Recipients:
                    em = _smtp_of(r)
                    if em:
                        add(em, r.Name or "")
                        n_sent += 1
            except Exception:
                continue
    except Exception as e:
        print(f"  (couldn't read Sent Items: {e})")
    print(f"  {n_sent} recipient addresses seen")

    out = sorted(people.values(), key=lambda r: r["email"])
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"\nWrote {len(out)} unique people -> {OUT_PATH}")
    print("The voice server picks this up automatically on the next question"
          " (no restart needed).")


if __name__ == "__main__":
    main()
