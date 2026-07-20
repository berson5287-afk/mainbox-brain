"""
Live demo against YOUR real mailbox via Microsoft Graph.

    python -m mainbox_brain.demo_graph              # login + mine + show vendors
    python -m mainbox_brain.demo_graph --ask        # ...then run a quote request
    python -m mainbox_brain.demo_graph --months 24  # scan further back

SAFETY DEFAULTS (deliberate):
  - This demo NEVER auto-sends email. In --ask mode, "send now" is downgraded
    to creating a DRAFT in your real Drafts folder; you hit send in Outlook.
    Remove the training wheels only by editing ALLOW_REAL_SEND below.
  - Mining is read-only: it fetches Sent Items and learns; it changes nothing.

Prereqs (see README Tier 3): pip install msal, register an Entra app,
set MAINBOX_GRAPH_CLIENT_ID.
"""
from __future__ import annotations
import sys

from .graph_client import GraphMailClient, GraphAuthError
from .parser import parse_request
from .conversation import QuoteConversation
from . import vendors

ALLOW_REAL_SEND = False   # flip to True only when you trust it


def main() -> None:
    months = 12
    if "--months" in sys.argv:
        months = int(sys.argv[sys.argv.index("--months") + 1])

    try:
        client = GraphMailClient()
    except GraphAuthError as e:
        print(f"Setup needed: {e}")
        sys.exit(1)

    print("Signing in to Microsoft Graph…")
    who = client.login()
    print(f"Signed in as: {who}")

    print(f"\nMining Sent Items (last {months} months, up to 500 messages)…")
    result = client.mine_history(months=months)

    print(f"\nScanned: {len(result.records)} RFQ record(s) kept | "
          f"{result.skipped_customer_quotes} customer quote(s) skipped | "
          f"{result.skipped_unknown} unclassified skipped")
    print("\nLearned vendors:")
    if not result.vendors:
        print("  (none found — try --months 24, or your RFQ phrasing may need "
              "cues added to history_miner._VENDOR_CUES)")
    for vid, mv in sorted(result.vendors.items(),
                          key=lambda kv: kv[1].sightings, reverse=True):
        flag = "SUGGESTABLE" if mv.confident else f"below floor ({mv.sightings}x)"
        contacts = ", ".join(c.name for c in mv.vendor.contacts) or "?"
        print(f"  • {mv.vendor.name:<28} [{flag}] contacts: {contacts}"
              f" | cats: {sorted(mv.categories) or ['-']}")

    if "--ask" not in sys.argv:
        print("\nRead-only run complete. Re-run with --ask to make a request "
              "against the learned registry.")
        return

    # quote flow against the LEARNED registry, real Drafts folder as output
    vendors.VENDORS = result.confident_registry()
    if not ALLOW_REAL_SEND:
        real_send = client.send_email
        client.send_email = lambda d: (
            print("  [safety] send downgraded to DRAFT — check your Drafts folder"),
            client.create_draft(d))[-1]

    print("\nType a request (or 'quit'):")
    while True:
        try:
            text = input("\nrequest> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text.lower() in {"quit", "exit", "q"}:
            return
        if not text:
            continue
        req = parse_request(text)
        cats = {it.category for it in req.items}
        memory = result.describe_for(cats)
        if memory:
            print("brain: " + memory)
        convo = QuoteConversation(client, sent_history=result.records)
        turn = convo.start(req)
        print("brain: " + turn.message)
        while not turn.done:
            reply = input("you>   ").strip()
            turn = convo.handle(reply)
            print("brain: " + turn.message)


if __name__ == "__main__":
    main()
