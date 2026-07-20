"""
Interactive console on the PERSISTED brain -- everything we built, no re-mining.

    py -m mainbox_brain.ask                 # uses mainbox.db in this folder
    py -m mainbox_brain.ask --db path.db

Loads the learned registry (corrections applied at save time, contacts ranked)
and the full sent-history signal straight from the db, then talks:

    request> price and availability on 2,000ft of 12/2 MC
    request> find 8400 connector          <- recall search, no confidence floor
    request> vendors                      <- list the learned registry
    request> quit

Mail is the printing stub (nothing real is sent). For real drafts use the
server with --graph, or demo_graph.
"""
from __future__ import annotations
__version__ = "0.46"
import os
import sys
import time

from .store import Store, DEFAULT_DB
from . import intents
from .parser import parse_request
from .conversation import QuoteConversation
from .graph_client import StubMailClient
from .history_miner import MiningResult, MinedVendor
from . import vendors

# friendly words -> category sets for 'vendors wire' style filters
_FRIENDLY_CATS = {
    "wire": {"building_wire", "wire_cable", "mc_cable"},
    "cable": {"wire_cable", "mc_cable", "building_wire"},
    "mc": {"mc_cable"},
    "conduit": {"conduit"},
    "pipe": {"conduit"},
    "fittings": {"fittings"},
    "gear": {"gear"},
    "panels": {"gear"},
    "breakers": {"gear"},
    "lighting": {"lighting"},
    "transformers": {"transformer"},
    "transformer": {"transformer"},
    "boxes": {"boxes_enclosures"},
    "enclosures": {"boxes_enclosures"},
    "tools": {"tools"},
    "strut": {"fittings"},
    "fasteners": {"fittings"},
    "grounding": {"fittings"},
    "firestop": {"fittings"},
    "devices": {"gear"},
}
import re
_VENDOR_LIST = re.compile(
    r"^(?:list\s+)?(?:(?P<a>[a-z ]+?)\s+)?vendors(?:\s+(?P<b>[a-z ]+))?$")


# -- v0.46: data freshness --------------------------------------------------
# The db is written by the Outlook sync (manual `py -m mainbox_brain.update`
# or the server's --auto-refresh timer). Searches read live SQL, so new rows
# appear automatically; only the in-memory vendor registry needs a reload.
def _db_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _age_str(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    h = m / 60
    if h < 36:
        return f"{h:.1f} hr"
    return f"{h / 24:.1f} days"


def _hot_reload(store, session) -> str:
    """Re-read the vendor registry after a background sync touched the db.
    Returns a one-line notice for the prompt."""
    registry = store.load_vendors(confident_only=True)
    vendors.VENDORS = registry
    n_r = len(store.load_records())
    n_rep = store.reply_count() if hasattr(store, "reply_count") else 0
    return (f"(synced with Outlook: now {len(registry)} vendors / "
            f"{n_r} RFQ / {n_rep} reply records)")


def main() -> None:
    db_path = DEFAULT_DB
    if "--db" in sys.argv:
        db_path = sys.argv[sys.argv.index("--db") + 1]

    store = Store(db_path)
    registry = store.load_vendors(confident_only=True)
    records = store.load_records()
    if not registry:
        print(f"No vendors in {db_path}. Run the miner first:\n"
              f"    py -m mainbox_brain.corpus sent_export.json --db {db_path}")
        sys.exit(1)

    vendors.VENDORS = registry
    mail = StubMailClient(verbose=True)
    last_convo = None
    # v0.44: llm must be assigned BEFORE session is constructed so InfoSession
    # receives the live LLMClient (or None) rather than always None.
    llm = None
    if "--llm" in sys.argv:
        from .llm import LLMClient
        probe = LLMClient()
        if probe.complete("ping", system="Reply with: ok"):
            llm = probe
            print(f"(LLM router active: {probe.last_tier_used})")
        else:
            print("(LLM unreachable — regex intent routing)")
    session = intents.InfoSession(store, llm=llm)
    reply_count = store.reply_count() if hasattr(store, "reply_count") else 0
    print(f"Loaded {len(registry)} vendors / {len(records)} RFQ records / {reply_count} reply records from {db_path}.")
    # v0.46: show how fresh the Outlook-mined data is, and nudge toward the
    # auto-sync server when it's gone stale.
    db_mtime = _db_mtime(db_path)
    if db_mtime:
        age_s = max(0.0, time.time() - db_mtime)
        stale = age_s > 24 * 3600
        print(f"Data last synced from Outlook {_age_str(age_s)} ago."
              + ("  (stale — run the auto-sync server: "
                 "py -m mainbox_brain.server --auto-refresh 15)" if stale else ""))
    print("Type a quote request, 'find <product>', 'vendors', "
          "'vendors wire' (or conduit/fittings/gear/lighting...), price/lead-time questions, or 'quit'.")

    while True:
        try:
            text = input("\nrequest> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        # v0.46: a background sync (server --auto-refresh or a manual update)
        # rewrote the db since we last looked -> reload the in-memory vendor
        # registry so new vendors/contacts are usable mid-session. Record
        # searches already read live SQL and need nothing.
        new_mtime = _db_mtime(db_path)
        if new_mtime > db_mtime:
            db_mtime = new_mtime
            try:
                print(_hot_reload(store, session))
            except Exception:
                pass
        low = text.lower()
        if low in {"quit", "exit", "q"}:
            return

        # v0.51: 'version' / 'versions' -- shows what's installed and flags stale files
        if low in {"version", "versions", "version?", "what version"}:
            try:
                from mainbox_brain import _version_report
                print(_version_report.report(db_path))
            except Exception as e:
                print(f"(version check unavailable: {e})")
            continue

        if low.startswith("find "):
            hits = store.find(text[5:].strip())
            if not hits:
                print("No matching past RFQ lines.")
            for h in hits:
                print(f"  [{h['when'] or '????-??-??'}] {h['item'][:60]!r} -> "
                      f"{h['vendor']} <{h['to']}> ({int(h['score']*100)}%)")
            continue

        # 'vendors', 'vendors wire', 'list wire vendors', 'list vendors'
        m = _VENDOR_LIST.match(low)
        if m:
            word = (m.group("a") or m.group("b") or "").strip()
            cats = _FRIENDLY_CATS.get(word) if word else None
            if word and cats is None:
                # not a category -- treat it as a product ("list caddy vendors")
                print("brain: " + intents.answer_vendors_for(store, word))
                continue
            vcats = store.vendor_categories()
            shown = 0
            for vid, v in sorted(registry.items(), key=lambda kv: kv[1].name.lower()):
                if cats is not None and not (vcats.get(vid, set()) & cats):
                    continue
                primary = v.primary_contact
                who = f"{primary.name} <{primary.email}>" if primary else "?"
                tag = ",".join(sorted(vcats.get(vid, set()))) or "-"
                print(f"  {v.name:<28} primary: {who:<42} [{tag}]")
                shown += 1
            print(f"  ({shown} vendor(s){' for ' + word if word else ''})")
            continue

        # the conversational session handles info questions with context:
        # clarifications ("which product?" -> "red emt" resumes), follow-ups
        # ("what's the lead time?"), customer disambiguation, and research "yes"
        answer = session.answer(text)
        if answer is not None:
            print("brain: " + answer)
            continue

        # escape hatch: if the previous request auto-drafted, "send now"
        # right after still sends those drafts instead of parsing as parts
        if last_convo is not None and getattr(last_convo, "_auto_drafted", False):
            from .conversation import _extract_delivery
            if _extract_delivery(text)[0] == "send":
                print("brain: " + last_convo.handle(text).message)
                last_convo = None
                continue

        req = parse_request(text)
        convo = QuoteConversation(mail, sent_history=records, store=store)
        last_convo = convo
        turn = convo.start(req)
        print("brain: " + turn.message)
        while not turn.done:
            try:
                reply = input("you>   ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            # a question mid-selection is a topic change, not a vendor pick
            rit = intents.classify(reply, llm)
            ranswer = intents.handle(rit, store, question=reply, llm=llm)
            if ranswer is not None:
                print("brain: (setting that RFQ aside)")
                print("brain: " + ranswer)
                break
            turn = convo.handle(reply)
            print("brain: " + turn.message)


if __name__ == "__main__":
    main()
