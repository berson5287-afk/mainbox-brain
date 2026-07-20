"""
End-to-end demo for the MaINbox brain.

Run:
    python -m mainbox_brain.demo            # scripted walkthrough
    python -m mainbox_brain.demo --interactive   # type your own requests

No credentials, no GPU host needed. If tillium-bridge / local Ollama happen
to be reachable they'll be used for parsing + intent; otherwise the regex
paths run. Either way the flow completes — that's the degradation contract.
"""
from __future__ import annotations
import sys

from .parser import parse_request
from .resolver import resolve, summarize_proposal
from .conversation import QuoteConversation
from .graph_client import StubMailClient
from .intent import detect_meeting, classify_followup
from .llm import LLMClient


def _rule(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _show_parse(req) -> None:
    print(f"\nRequest: {req.raw_text!r}")
    print(f"Parsed {len(req.items)} line item(s):")
    for it in req.items:
        spec = f"  spec={it.spec}" if it.spec else ""
        print(f"  • {it.describe():<28} category={it.category}{spec}")


def _show_resolution(req, mail) -> None:
    resolved = resolve(req, mail.recent_sent())
    print("\nVendor resolution (ranked):")
    for r in resolved:
        who = r.contact.name if r.contact else r.vendor.name
        print(f"  [{r.score:>4}] {who} at {r.vendor.name}")
        for reason in r.reasons:
            print(f"         - {reason}")
    print("\n→ " + summarize_proposal(req, resolved))


def scripted(llm: LLMClient | None) -> None:
    mail = StubMailClient()

    _rule("DEMO 1 — single line, subset choice, create draft")
    req = parse_request("Can I get price and availability for 10,000ft of 12/2 MC?", llm)
    _show_parse(req)
    _show_resolution(req, mail)
    convo = QuoteConversation(mail)
    convo.start(req)
    print("\nuser: \"just Mark\"")
    print("brain: " + convo.handle("just Mark").message)
    print("\nuser: \"draft\"")
    print("brain: " + convo.handle("draft").message)

    _rule("DEMO 2 — multi-item request, send to all")
    req2 = parse_request("500ft of 3/4 EMT and (2) 200A panels", llm)
    _show_parse(req2)
    _show_resolution(req2, mail)
    convo2 = QuoteConversation(mail)
    convo2.start(req2)
    print("\nuser: \"yes both\"")
    print("brain: " + convo2.handle("yes both").message)
    print("\nuser: \"send now\"")
    print("brain: " + convo2.handle("send now").message)

    _rule("DEMO 3 — intent detection on text (the legal, buildable-now layer)")
    samples = [
        "Sounds good — yeah I'm free Wednesday, let's do lunch at noon.",
        "Can you send me the updated quote when you get a chance?",
        "We went with another supplier on this one, thanks anyway.",
    ]
    for s in samples:
        hint = detect_meeting(s, llm)
        fu = classify_followup(s, llm)
        tag = (f"MEETING day={hint.day} time={hint.time}" if hint.matched
               else "no meeting")
        print(f"\n  text: {s!r}")
        print(f"        → {tag} | follow-up={fu}")


def interactive(llm: LLMClient | None) -> None:
    import os
    if os.path.exists("mainbox.db"):
        print("NOTE: this demo uses PLACEHOLDER vendors. For your real learned "
              "registry (find/vendors commands), use:  py -m mainbox_brain.ask\n")
    mail = StubMailClient()
    print("Type a quote request (or 'quit'). Example: "
          "'price and availability on 5000ft of 12 awg thhn'")
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
        req = parse_request(text, llm)
        _show_parse(req)
        convo = QuoteConversation(mail)
        turn = convo.start(req)
        print("\nbrain: " + turn.message)
        while not turn.done:
            reply = input("you>   ").strip()
            turn = convo.handle(reply)
            print("brain: " + turn.message)


def main() -> None:
    use_llm = "--no-llm" not in sys.argv
    llm = LLMClient() if use_llm else None
    tier = None
    if llm is not None:
        tier = llm.last_tier_used  # set lazily after first call
    print("MaINbox brain demo — LLM tier will be used if reachable, "
          "else regex fallback.")
    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive(llm)
    else:
        scripted(llm)
        print("\n(LLM used: "
              f"{llm.last_tier_used if llm and llm.last_tier_used else 'none — ran on regex fallback'})")


if __name__ == "__main__":
    main()
