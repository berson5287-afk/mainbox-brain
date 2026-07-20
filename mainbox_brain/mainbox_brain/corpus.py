"""
Corpus loader -- mine sent mail from local exports, no Graph required.

The miner doesn't care where messages come from. This module feeds it from:

  1. A JSON/JSONL export (e.g. produced by export_sent_outlook.py via COM
     on your desktop -- real mailbox data, zero admin consent needed)
  2. A folder of .txt files (hand-made test cases: first line = To:,
     second = Subject:, rest = body)

CLI:
    py -m mainbox_brain.corpus sent_export.json
    py -m mainbox_brain.corpus my_test_emails\\
    py -m mainbox_brain.corpus sent_export.json --ask   # quote flow after mining

JSON record shape (a list of these, or one per line for .jsonl):
    {"to": "mark@brazill.com", "to_name": "Mark Evans",
     "subject": "RFQ", "body": "Hi Mark, ...", "when": "2026-05-01T10:00:00"}
Only "to" is required.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

from .history_miner import mine, SentMessage, MiningResult


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


def _from_record(rec: dict) -> SentMessage | None:
    to = (rec.get("to") or rec.get("to_email") or "").strip()
    if not to or "@" not in to:
        return None
    return SentMessage(
        to_email=to,
        to_display_name=rec.get("to_name", "") or rec.get("to_display_name", ""),
        subject=rec.get("subject", "") or "",
        body=rec.get("body", "") or "",
        when=_parse_when(rec.get("when") or rec.get("sent") or rec.get("date")),
    )


def load_json(path: str | Path) -> list[SentMessage]:
    """Load a .json (list of records) or .jsonl (one record per line) export."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    records: list[dict] = []
    stripped = text.lstrip()
    if stripped.startswith("["):
        records = json.loads(text)
    else:  # jsonl
        for line in text.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    out = []
    for rec in records:
        msg = _from_record(rec)
        if msg:
            out.append(msg)
    return out


def load_folder(path: str | Path) -> list[SentMessage]:
    """Folder of .txt files: line 1 'To: addr', line 2 'Subject: ...', rest body."""
    out: list[SentMessage] = []
    for f in sorted(Path(path).glob("*.txt")):
        lines = f.read_text(encoding="utf-8-sig").splitlines()
        to, subject, body_start = "", "", 0
        for i, line in enumerate(lines[:4]):
            low = line.lower()
            if low.startswith("to:"):
                to = line.split(":", 1)[1].strip()
                body_start = max(body_start, i + 1)
            elif low.startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
                body_start = max(body_start, i + 1)
        if not to:
            continue
        out.append(SentMessage(to_email=to, subject=subject,
                               body="\n".join(lines[body_start:]).strip()))
    return out


def load_any(path: str | Path) -> list[SentMessage]:
    p = Path(path)
    if p.is_dir():
        return load_folder(p)
    if p.suffix.lower() in {".json", ".jsonl"}:
        return load_json(p)
    raise ValueError(f"Don't know how to load {p} (expected folder, .json, or .jsonl)")


def report(result: MiningResult) -> None:
    print(f"\nKept {len(result.records)} RFQ record(s) | "
          f"skipped {result.skipped_customer_quotes} customer quote(s), "
          f"{result.skipped_unknown} unclassified")
    print("\nLearned vendors:")
    if not result.vendors:
        print("  (none — check phrasing against history_miner._VENDOR_CUES)")
    for vid, mv in sorted(result.vendors.items(),
                          key=lambda kv: kv[1].sightings, reverse=True):
        flag = "SUGGESTABLE" if mv.confident else f"below floor ({mv.sightings}x)"
        contacts = ", ".join(c.name for c in mv.vendor.contacts) or "?"
        print(f"  • {mv.vendor.name:<28} [{flag}] contacts: {contacts}"
              f" | cats: {sorted(mv.categories) or ['-']}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    msgs = load_any(args[0])
    print(f"Loaded {len(msgs)} message(s) from {args[0]}")
    result = mine(msgs)

    # Persistence + corrections: every mine applies your recorded judgments
    # ("exclude ej1899") and saves the corrected registry to the db.
    from .store import Store, DEFAULT_DB
    db_path = DEFAULT_DB
    if "--db" in sys.argv:
        db_path = sys.argv[sys.argv.index("--db") + 1]
    store = Store(db_path)
    removed = store.apply_corrections(result)
    store.save_result(result)
    if removed:
        print(f"Applied corrections from {db_path}: removed {removed} vendor(s)")
    print(f"Saved registry to {db_path} "
          f"({store.vendor_count()} vendors, {len(result.records)} records)")

    report(result)

    if "--ask" in sys.argv:
        from .parser import parse_request
        from .conversation import QuoteConversation
        from .graph_client import StubMailClient
        from . import vendors
        vendors.VENDORS = result.confident_registry()
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
            memory = result.describe_for({it.category for it in req.items})
            if memory:
                print("brain: " + memory)
            convo = QuoteConversation(StubMailClient(), sent_history=result.records)
            turn = convo.start(req)
            print("brain: " + turn.message)
            while not turn.done:
                turn = convo.handle(input("you>   ").strip())
                print("brain: " + turn.message)


if __name__ == "__main__":
    main()
