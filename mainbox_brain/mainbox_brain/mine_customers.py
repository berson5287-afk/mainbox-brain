"""Mine a sales-mailbox export into customer purchase records (the sell side).

Run after exporting the sales inbox unfiltered, with attachments:

    py export_replies_outlook.py 3 sales_inbox.json --mailbox sales@... \\
        --all-senders --attachments sales_files
    py -m mainbox_brain.mine_customers sales_inbox.json sales_files --db mainbox.db

It classifies each email's sender (vendor / customer / internal), and for
customers it mines the attached POs/quotes into sell-tagged line items
(qty / description / unit price) plus the document header (PO#, quote#, total,
job).  Cost (vendor) data is untouched — this only adds the customer/sell side.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

from .store import Store
from .customer_docs import mine_customer_records


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    db = "mainbox.db"
    if "--db" in argv:
        i = argv.index("--db")
        db = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) < 2:
        print("usage: py -m mainbox_brain.mine_customers <sales_inbox.json> "
              "<attachments_dir> [--db mainbox.db]")
        sys.exit(1)
    inbox_path, att_dir = argv[0], argv[1]
    if not Path(inbox_path).exists():
        print(f"not found: {inbox_path}")
        sys.exit(1)
    if not Path(att_dir).is_dir():
        print(f"attachments dir not found: {att_dir}")
        sys.exit(1)

    with open(inbox_path, encoding="utf-8-sig") as f:
        records = json.load(f)
    print(f"loaded {len(records)} sales-inbox emails; mining customer documents…")

    store = Store(db)
    recs = mine_customer_records(records, store, att_dir)
    priced = [f for r in recs for f in r.facts if f.unit_price is not None]
    customers = sorted({r.vendor_name for r in recs})
    saved = store.save_reply_records(recs)

    print(f"\nCustomer mining complete:")
    print(f"  customer purchase records: {saved}")
    print(f"  sell-priced line items:    {len(priced)}")
    print(f"  distinct customers:        {len(customers)}")
    print(f"  POs with a number captured:{sum(1 for r in recs if r.subject.startswith('PO '))}")
    print("\nAsk things like:  \"what did <customer> order\",  "
          "\"what's our sell price on <part>\".")


if __name__ == "__main__":
    main()
