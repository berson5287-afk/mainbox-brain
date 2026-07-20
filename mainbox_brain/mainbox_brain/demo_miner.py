"""
Demo: zero-config vendor learning from sent mail.

Feeds the miner a realistic fake Sent Items folder -- including the dangerous
case (quotes TO customers that mention the same products) -- then runs your
exact scenario:

    "Can I get price and availability for 1,000ft thhn 12 stranded black"
      -> miner-built registry, no vendors.py editing
      -> "The last couple of times you sent this to ..."
      -> normal confirm flow

Run:  python -m mainbox_brain.demo_miner
"""
from __future__ import annotations
from datetime import datetime, timedelta

from .history_miner import mine, SentMessage
from .parser import parse_request
from . import resolver, vendors
from .conversation import QuoteConversation
from .graph_client import StubMailClient

NOW = datetime.now()


def fake_sent_items() -> list[SentMessage]:
    return [
        # --- real RFQs to vendors (should be learned) ---
        SentMessage("mark@brazill.com", "Mark Evans",
                    "RFQ: 12 AWG THHN",
                    "Hi Mark,\n\nCan you send price and availability on 5,000ft "
                    "12 awg THHN stranded black? Lead time too please.\n\nSteve",
                    NOW - timedelta(days=9)),
        SentMessage("mark@brazill.com", "Mark Evans",
                    "P&A — 10/2 MC",
                    "Hi Mark,\n\nPrice and availability on 2,500ft 10/2 MC?\n\nSteve",
                    NOW - timedelta(days=31)),
        SentMessage("thea@pipeandwirequotes.com", "Thea R",
                    "quick quote",
                    "Hi Thea,\n\nCould you quote 1,000ft 12 awg thhn and 500ft "
                    "of 3/4 EMT? Best price please.\n\nSteve",
                    NOW - timedelta(days=15)),
        SentMessage("thea@pipeandwirequotes.com", "Thea R",
                    "availability check",
                    "Hi Thea,\n\nDo you stock 12/2 MC? Availability on 4,000ft?\n\nSteve",
                    NOW - timedelta(days=58)),
        SentMessage("john@lex.com", "John Carter",
                    "RFQ — EMT",
                    "Hi John,\n\nCan you quote 2,000ft of 1/2 EMT and 200 "
                    "connectors? What's your lead time on the EMT?\n\nSteve",
                    NOW - timedelta(days=20)),

        # --- TRAP: quotes TO CUSTOMERS, same products (must be skipped) ---
        SentMessage("pm@constructco.com", "Project Manager",
                    "Quote — THHN order",
                    "Hi Dave,\n\nThank you for the opportunity. Please find our "
                    "quote attached for 1,000ft 12 awg THHN. Pricing below is "
                    "valid for 30 days.\n\nSteve",
                    NOW - timedelta(days=7)),
        SentMessage("buyer@megabuild.com", "Buyer",
                    "Your MC cable pricing",
                    "Hi Karen,\n\nPer your request, pricing below for 12/2 MC. "
                    "We have stock and lead time is 2 days.\n\nSteve",
                    NOW - timedelta(days=12)),

        # --- one-off vendor (below confidence floor; learned but not suggested) ---
        SentMessage("sales@onetimevendor.com", "Sales",
                    "price check",
                    "Hi,\n\nBest price on 100 1/2in EMT connectors?\n\nSteve",
                    NOW - timedelta(days=90)),
    ]


def main() -> None:
    print("=" * 68)
    print("STEP 1 — mine the (fake) Sent Items")
    print("=" * 68)
    result = mine(fake_sent_items())

    print(f"\nSkipped {result.skipped_customer_quotes} customer quote(s) "
          f"(the dangerous misfire), {result.skipped_unknown} unknown.")
    print("\nLearned vendors:")
    for vid, mv in result.vendors.items():
        flag = "SUGGESTABLE" if mv.confident else "below floor (1 sighting)"
        contacts = ", ".join(c.name for c in mv.vendor.contacts)
        print(f"  • {mv.vendor.name:<22} [{flag}] contacts: {contacts} "
              f"| cats: {sorted(mv.categories)}")

    print()
    print("=" * 68)
    print("STEP 2 — your scenario, zero vendors.py editing")
    print("=" * 68)
    # Use ONLY the learned registry: swap it in over the hand-built table.
    vendors.VENDORS = result.confident_registry()

    text = "Can I get price and availability for 1,000ft thhn 12 stranded black"
    req = parse_request(text)
    print(f"\nRequest: {text!r}")
    for it in req.items:
        print(f"  • {it.describe()}  category={it.category}  spec={it.spec}")

    cats = {it.category for it in req.items}
    memory_line = result.describe_for(cats)
    if memory_line:
        print(f"\nbrain: {memory_line}")

    mail = StubMailClient(verbose=True)
    convo = QuoteConversation(mail, sent_history=result.records)
    turn = convo.start(req)
    print("brain: " + turn.message)

    print('\nuser: "yes both"')
    print("brain: " + convo.handle("yes both").message)
    print('\nuser: "draft"')
    print("brain: " + convo.handle("draft").message)


if __name__ == "__main__":
    main()
