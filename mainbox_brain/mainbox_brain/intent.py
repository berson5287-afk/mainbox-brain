"""
Intent detection -- the buildable-now, legally-clean layer.

This runs on TEXT you already have (email bodies, or consented call
transcripts), never on live ambient/call audio. That's the whole point we
landed on: make extraction the processing layer, not the capture layer.

Two functions:
  detect_meeting()      -- find scheduling intent + a rough day/time
  classify_followup()   -- waiting / closed / neutral (your Phase-1 idea)

Both regex-first with an optional LLM assist for the fuzzy variants
("let's sync", "grab 15 min", "when works for you?") that keyword lists miss.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional

from .llm import LLMClient, extract_json

_MEETING_CUES = [
    "are you free", "can we meet", "let's meet", "let's do lunch", "grab lunch",
    "set up a call", "hop on a call", "let's sync", "touch base", "catch up",
    "schedule", "available", "meeting", "calendar", "when works", "grab 15",
    "coffee", "dinner",
]
_DAYS = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
_TIME = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|noon|midnight"
_DAY_RE = re.compile(_DAYS, re.IGNORECASE)
_TIME_RE = re.compile(_TIME, re.IGNORECASE)


@dataclass
class MeetingHint:
    matched: bool
    day: Optional[str] = None
    time: Optional[str] = None
    snippet: str = ""


def detect_meeting(text: str, llm: Optional[LLMClient] = None) -> MeetingHint:
    low = text.lower()
    cue = next((c for c in _MEETING_CUES if c in low), None)
    day = _DAY_RE.search(text)
    time = _TIME_RE.search(text)

    if cue or (day and time):
        return MeetingHint(
            matched=True,
            day=day.group(0).title() if day else None,
            time=time.group(0) if time else None,
            snippet=text.strip()[:160],
        )

    if llm is not None:
        raw = llm.complete(
            f"Text: {text}",
            system=('Does this text propose or agree to a meeting/call? Return ONLY '
                    'JSON: {"meeting": true|false, "day": string|null, "time": string|null}.'),
        )
        parsed = extract_json(raw) if raw else None
        if isinstance(parsed, dict) and parsed.get("meeting"):
            return MeetingHint(True, parsed.get("day"), parsed.get("time"),
                               text.strip()[:160])

    return MeetingHint(matched=False)


_WAITING = ["let me know", "get back to me", "waiting on", "once you", "pending",
            "send me", "can you send", "please advise", "awaiting"]
_CLOSED = ["all set", "no longer need", "we went with", "thanks anyway",
           "closed", "cancel", "disregard", "resolved"]


def classify_followup(text: str, llm: Optional[LLMClient] = None) -> str:
    """Return 'waiting', 'closed', or 'neutral' for an outgoing message."""
    low = text.lower()
    if any(p in low for p in _CLOSED):
        return "closed"
    if any(p in low for p in _WAITING):
        return "waiting"
    if llm is not None:
        raw = llm.complete(
            f"Outgoing message: {text}",
            system=("Classify whether the sender is waiting on a reply. Return ONLY "
                    'one word: waiting, closed, or neutral.'),
        )
        if raw:
            word = raw.strip().lower().split()[0].strip(".,")
            if word in {"waiting", "closed", "neutral"}:
                return word
    return "neutral"
