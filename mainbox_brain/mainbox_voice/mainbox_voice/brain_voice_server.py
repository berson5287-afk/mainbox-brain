#!/usr/bin/env python3
"""
brain_voice_server.py  -  MaINbox Voice: the phone-facing voice server.
v0.1.0

Serves the MaINbox Voice PWA (www/) and a small JSON API that the phone app
talks to. Architecture follows the established MaINbox pattern:

    phone (Chrome PWA)  ->  THIS SERVER (Brain PC, over Tailscale)  ->  toolkit

Nothing on the phone touches files or the Bridge directly; the phone only ever
speaks HTTP to this server.

Endpoints (all /api/* require the token, header X-MBB-Token or ?token=):
  POST /api/query           {"text": "whats equal to a topaz 100"}
                            -> parsed via xref_common.parse_query, routed to
                               the cross-reference dispatcher; returns display
                               results + a short speakable reply. Also handles
                               spoken corrections ("confirm the first one",
                               "reject rest") against the last results.
  POST /api/extract_events  {"text": "<call-notes transcript>"}
                            -> proposed calendar events with one-tap Google
                               Calendar links (+ /api/ics fallback).
  GET  /api/ics?...         -> downloadable .ics for one event.
  GET  /api/notifications?since=N   -> notifications newer than id N.
  POST /api/notify          {"title","body"} -> push a notification to the
                               phone (MaINbox/Brain call this).
  POST /api/rfq/reply       {"subject","body","from"} -> match an inbound
                               vendor email to its RFQ + vendor, record a
                               'replied' event, refresh coverage, notify the
                               phone. MaINbox's inbox hook forwards mail here.
  GET  /api/ping            -> {"ok": true, "version": ...}

Zero third-party dependencies (stdlib only). The xref toolkit modules
(xref_common, xref_dispatch, cross_reference) are soft-imported: if they're
missing, voice chat still works and says so honestly.

Run:
    python brain_voice_server.py                # port 8770, token auto-made
    python brain_voice_server.py --port 8770 --token mysecret

The token is printed at startup and saved to .voice_token so it survives
restarts. On the phone, open  http://<tailscale-ip>:8770/?token=<token>
"""

from __future__ import annotations

import os
import re
import ssl
import sys
import json
import time
import uuid
import secrets
import logging
import argparse
import threading
import concurrent.futures
from datetime import datetime, timedelta
from collections import deque
from urllib.parse import urlparse, parse_qs, quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.9.0"  # call-transcript ingest: /api/call_transcript, alerts with extracted events, last-call recall

log = logging.getLogger("mbb_voice")

# --- soft imports of the xref toolkit ----------------------------------------
def _imp(name):
    import importlib
    try:
        return importlib.import_module(name)
    except Exception as e:  # noqa: BLE001
        log.warning("module %s unavailable: %s", name, e)
        return None

xc = _imp("xref_common")          # parse_query / normalize
xd = _imp("xref_dispatch")        # cross_reference() / known_sites()
xr = _imp("cross_reference")      # confirm / reject store
rmatch = _imp("rfq_match")        # inbound vendor-reply matcher (pure module)

# --- config -------------------------------------------------------------------
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".voice_token")
DB_PATH = os.environ.get("XREF_DB", "cross_references.db")

# The full MaINbox Brain runs its own HTTP API (mainbox_brain.server, default
# 127.0.0.1:8585). General questions — pricing, "what did we last pay", vendor
# history, anything that isn't a cross-reference lookup or an RFQ/calendar
# command — are forwarded there so the phone is as smart as the PC. Override
# with MBB_BRAIN_URL if the Brain runs elsewhere or on another port.
BRAIN_URL = os.environ.get("MBB_BRAIN_URL",
                           "http://127.0.0.1:8585").rstrip("/")

# Cross-reference lookups can open a real browser (Playwright) for taught
# vendor sites, which occasionally stalls (a slow site, a consent modal, or a
# form that wants a manufacturer). Run them on a small pool with a hard
# timeout so a stuck lookup can never lock up the phone. Override the ceiling
# with MBB_XREF_TIMEOUT (seconds).
XREF_TIMEOUT = float(os.environ.get("MBB_XREF_TIMEOUT", "200"))
_XREF_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="xref")

_BASE = os.path.dirname(os.path.abspath(__file__))
RFQ_DIR = os.path.join(_BASE, "rfq_queue")            # queued/sent RFQ JSONs
RFQ_COVERAGE_DIR = os.path.join(RFQ_DIR, "coverage")  # Quote Coverage handoff
RFQ_CONFIG = os.path.join(_BASE, "rfq_config.json")   # signature/company
CERTS_DIR = os.path.join(_BASE, "certs")              # auto-detected TLS certs

# --- shared state (single-user v1) ---------------------------------------------
_state_lock = threading.Lock()
SESSION: dict = {"last": []}          # last numbered findings for corrections
NOTIFS: deque = deque(maxlen=200)     # {"id","ts","title","body"}
_NOTIF_ID = [0]


def push_notification(title: str, body: str = "") -> dict:
    with _state_lock:
        _NOTIF_ID[0] += 1
        n = {"id": _NOTIF_ID[0], "ts": time.time(),
             "title": str(title)[:120], "body": str(body)[:500]}
        NOTIFS.append(n)
        return n

CALL_DIR = os.path.join(_BASE, "call_transcripts")


def save_call_transcript(filename: str, text: str) -> dict:
    """v0.9.0: a call recording transcribed on the PC lands here — saved to
    disk, events extracted, and an alert pushed so the phone sees it without
    any copy/paste."""
    os.makedirs(CALL_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", (filename or "call"))[:80]
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(CALL_DIR, f"{ts}_{safe}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
    except OSError as e:
        return {"ok": False, "error": f"save failed: {e}"}
    evs = extract_events(text or "")
    ev_note = ""
    if evs:
        titles = "; ".join(e.get("title", "")[:40] for e in evs[:3])
        ev_note = f" — {len(evs)} event(s): {titles}"
    snippet = (text or "").strip().replace("\n", " ")[:160]
    push_notification(f"📞 Call transcript: {safe}",
                      snippet + ev_note
                      + " (open Listen → Load last call to review)")
    with _state_lock:
        SESSION["last_call_transcript"] = {"filename": safe, "text": text,
                                           "ts": time.time()}
    return {"ok": True, "saved": os.path.basename(path),
            "events": evs}


def latest_call_transcript() -> dict:
    with _state_lock:
        cur = SESSION.get("last_call_transcript")
    if cur:
        return {"ok": True, **cur}
    try:                       # survive restarts: newest file on disk
        files = sorted(os.listdir(CALL_DIR), reverse=True)
        for fn in files:
            if fn.endswith(".txt"):
                with open(os.path.join(CALL_DIR, fn), encoding="utf-8") as f:
                    return {"ok": True, "filename": fn, "text": f.read(),
                            "ts": os.path.getmtime(os.path.join(CALL_DIR, fn))}
    except OSError:
        pass
    return {"ok": False, "error": "no call transcripts yet"}



# ==============================================================================
# EVENT EXTRACTION (for Listen Mode / call notes)
# ==============================================================================
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6,
             "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
             "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}

_TIME_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b"
    r"|\b(noon|midnight)\b"
    r"|\bat\s+(\d{1,2})(?::(\d{2}))?\b", re.IGNORECASE)
_DUR_RE = re.compile(r"\bfor\s+(\d+)\s*(hour|hr|minute|min)s?\b", re.IGNORECASE)

# verbs that suggest a schedulable thing (used to build a decent title)
_EVENT_VERBS = ("meeting", "meet", "call", "appointment", "appt", "visit",
                "site visit", "pickup", "pick up", "delivery", "deliver",
                "quote due", "quote", "follow up", "followup", "demo",
                "walkthrough", "walk through", "lunch", "install", "estimate")


def _parse_time(seg: str):
    """Return (hour, minute, matched_text) or None. Times without am/pm use a
    business-hours guess: 1-7 -> pm, 8-11 -> am, 12 -> noon."""
    m = _TIME_RE.search(seg)
    if not m:
        return None
    if m.group(4):  # noon/midnight
        word = m.group(4).lower()
        return (12 if word == "noon" else 0, 0, m.group(0))
    if m.group(1):  # explicit with am/pm
        h = int(m.group(1)) % 12
        mn = int(m.group(2) or 0)
        if m.group(3).lower().startswith("p"):
            h += 12
        return (h, mn, m.group(0))
    # "at N" without am/pm
    h = int(m.group(5))
    mn = int(m.group(6) or 0)
    if 1 <= h <= 7:
        h += 12           # 1-7 -> afternoon
    elif h == 12:
        h = 12            # noon
    return (h, mn, m.group(0))


def _parse_date(seg: str, now: datetime):
    """Return (date, matched_text) or None. Handles today/tomorrow, weekdays
    (with optional 'next'), 'July 10(th)', and 7/10."""
    low = seg.lower()
    if re.search(r"\bday after tomorrow\b", low):
        return (now + timedelta(days=2)).date(), "day after tomorrow"
    if re.search(r"\btomorrow\b", low):
        return (now + timedelta(days=1)).date(), "tomorrow"
    if re.search(r"\btoday\b", low) or re.search(r"\bthis (morning|afternoon"
                                                 r"|evening)\b", low):
        return now.date(), "today"
    m = re.search(r"\b(next\s+)?(" + "|".join(_WEEKDAYS) + r")\b", low)
    if m:
        wd = _WEEKDAYS[m.group(2)]
        ahead = (wd - now.weekday()) % 7
        if ahead == 0:
            ahead = 7                     # bare weekday = the coming one
        if m.group(1):                    # "next monday" = the one after
            ahead += 7 if ahead <= 7 and (wd - now.weekday()) % 7 != 0 else 0
            # if today IS that weekday, 'next' means +7 which ahead already is
        return (now + timedelta(days=ahead)).date(), m.group(0)
    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})"
                  r"(?:st|nd|rd|th)?\b", low)
    if m:
        mo, dy = _MONTHS[m.group(1)], int(m.group(2))
        yr = now.year + (1 if (mo, dy) < (now.month, now.day) else 0)
        try:
            return datetime(yr, mo, dy).date(), m.group(0)
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", low)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else now.year
        if yr < 100:
            yr += 2000
        if not m.group(3) and (mo, dy) < (now.month, now.day):
            yr += 1
        try:
            return datetime(yr, mo, dy).date(), m.group(0)
        except ValueError:
            return None
    return None


def _title_for(seg: str, removed: list[str]) -> str:
    t = seg
    for r in removed:
        t = re.sub(re.escape(r), " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(at|on|next|this|the)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.-")
    if not t:
        for v in _EVENT_VERBS:
            if v in seg.lower():
                t = v.title()
                break
    return (t[:60] or "Event from call notes").strip().capitalize()


def _gcal_url(title: str, start: datetime, end: datetime) -> str:
    fmt = "%Y%m%dT%H%M%S"
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}"
            f"&dates={start.strftime(fmt)}/{end.strftime(fmt)}"
            f"&details={quote('Added by MaINbox Voice')}")


def extract_events(text: str, now: datetime | None = None) -> list[dict]:
    """Find schedulable events in free text. Returns a list of proposals:
    {title, start_iso, end_iso, gcal_url, source, confidence}.
    An event needs at least a date OR a time; date-only defaults to 9:00 AM,
    time-only defaults to today (tomorrow if that time already passed)."""
    now = now or datetime.now()
    events: list[dict] = []
    # segment on sentence-ish boundaries
    segs = [s.strip() for s in re.split(r"[.\n!?;]+", text or "") if s.strip()]
    for seg in segs:
        tm = _parse_time(seg)
        dt = _parse_date(seg, now)
        if not tm and not dt:
            continue
        removed = []
        if dt:
            day, dtxt = dt
            removed.append(dtxt)
        else:
            day = now.date()
        if tm:
            h, mn, ttxt = tm
            removed.append(ttxt)
        else:
            h, mn = 9, 0                       # date-only default 9am
        start = datetime(day.year, day.month, day.day, h, mn)
        if not dt and start <= now:            # time-only already passed
            start += timedelta(days=1)
        dur = 60
        dm = _DUR_RE.search(seg)
        if dm:
            n = int(dm.group(1))
            dur = n * 60 if dm.group(2).lower().startswith(("hour", "hr")) \
                else n
            removed.append(dm.group(0))
        end = start + timedelta(minutes=dur)
        title = _title_for(seg, removed)
        conf = 0.9 if (tm and dt) else 0.6
        events.append({
            "title": title,
            "start_iso": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_iso": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "start_h": start.strftime("%a %b %d, %I:%M %p").replace(" 0", " "),
            "gcal_url": _gcal_url(title, start, end),
            "source": seg[:140],
            "confidence": conf,
        })
    return events


def build_ics(title: str, start_iso: str, end_iso: str) -> str:
    def z(s):
        return s.replace("-", "").replace(":", "")
    uid = uuid.uuid4().hex
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "PRODID:-//MaINbox Voice//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}@mainbox\r\n"
            f"DTSTAMP:{datetime.now().strftime('%Y%m%dT%H%M%S')}\r\n"
            f"DTSTART:{z(start_iso)}\r\nDTEND:{z(end_iso)}\r\n"
            f"SUMMARY:{title}\r\n"
            "DESCRIPTION:Added by MaINbox Voice\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")


# ==============================================================================
# RFQ BUILDER (compose -> email out -> Quote Coverage handoff)
# ==============================================================================
_NUM_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
              "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_NUM_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_QTY_UNITS = ("feet", "foot", "ft", "pieces", "pcs", "each", "ea", "boxes",
              "box", "rolls", "roll", "coils", "coil", "cases", "case",
              "sticks", "stick", "spools", "spool")


def _words_to_number(tokens: list[str]):
    """Consume leading number words -> (value, tokens_consumed) or None.
    Handles 'fifty', 'twenty five', 'a hundred', 'two hundred fifty',
    'ten thousand', 'a thousand', 'two thousand five hundred'."""
    val, i, got = 0, 0, False
    if i < len(tokens) and tokens[i] in ("a", "an") and \
            i + 1 < len(tokens) and tokens[i + 1] in ("hundred", "thousand"):
        mult = 100 if tokens[i + 1] == "hundred" else 1000
        val, i, got = mult, 2, True
    else:
        if i < len(tokens) and tokens[i] in _NUM_UNITS:
            val, i, got = _NUM_UNITS[tokens[i]], 1, True
        elif i < len(tokens) and tokens[i] in _NUM_TENS:
            val, i, got = _NUM_TENS[tokens[i]], 1, True
            if i < len(tokens) and tokens[i] in _NUM_UNITS and \
                    _NUM_UNITS[tokens[i]] < 10:
                val += _NUM_UNITS[tokens[i]]; i += 1
        if got and i < len(tokens) and tokens[i] == "hundred":
            val *= 100; i += 1
            if i < len(tokens) and tokens[i] in _NUM_TENS:
                val += _NUM_TENS[tokens[i]]; i += 1
                if i < len(tokens) and tokens[i] in _NUM_UNITS and \
                        _NUM_UNITS[tokens[i]] < 10:
                    val += _NUM_UNITS[tokens[i]]; i += 1
            elif i < len(tokens) and tokens[i] in _NUM_UNITS:
                val += _NUM_UNITS[tokens[i]]; i += 1
    # handle "thousand" multiplier (e.g. "ten thousand", "two thousand five")
    if got and i < len(tokens) and tokens[i] == "thousand":
        val *= 1000; i += 1
        # optional remainder: five hundred, two fifty, etc.
        rest = _words_to_number(tokens[i:])
        if rest:
            val += rest[0]; i += rest[1]
    return (val, i) if got else None


def parse_rfq_line(text: str) -> dict:
    """'add 25 QO130' / 'fifty feet of 12-2 MC' / '10k ft of 12/2 MC'
    -> {qty, unit, part}. qty defaults to 1 if no leading number."""
    t = (text or "").strip()
    t = re.sub(r"^(add|and|also|plus|a|an|the)\s+", "", t,
               flags=re.IGNORECASE).strip()
    # strip trailing pleasantries ("thanks", "please", "for me")
    t = re.sub(r"\b(thanks?|thank you|please|for me|okay|ok)\b\s*[.!]*\s*$", "",
               t, flags=re.IGNORECASE).strip()
    # sometimes a stray leading "1 " precedes a real quantity ("1 10k ft ...")
    t = re.sub(r"^1\s+(?=\d)", "", t)
    # speech-to-text turns "12/2" into "12 to 2" — put the slash back
    t = re.sub(r"\b(\d{1,2})\s+to\s+(\d{1,2})\b", r"\1/\2", t)
    # "10k"/"10K"/"2.5k" → thousands (10000). Applies when glued or spaced.
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*k\b",
               lambda m: str(int(float(m.group(1)) * 1000)), t,
               flags=re.IGNORECASE)
    # strip thousands-separator commas anywhere a number is followed by a comma
    # then more digits — handles "10,000ft", "10,000 ft", "10,000" alike
    t = re.sub(r"(\d{1,3}(?:,\d{3})+)", lambda m: m.group(0).replace(",", ""), t)
    toks = t.split()
    qty, unit = 1, ""
    if toks:
        # handle "10000ft" — digit(s) glued to a unit word
        m2 = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$", toks[0].lower())
        if m2 and m2.group(2) in _QTY_UNITS:
            qty = float(m2.group(1))
            qty = int(qty) if qty == int(qty) else qty
            unit = m2.group(2)
            toks = toks[1:]
        else:
            m = re.match(r"^(\d+(?:\.\d+)?)$", toks[0])
            if m:
                qty = float(m.group(1))
                qty = int(qty) if qty == int(qty) else qty
                toks = toks[1:]
            else:
                wn = _words_to_number([w.lower() for w in toks])
                if wn:
                    qty = wn[0]
                    toks = toks[wn[1]:]
    if toks and toks[0].lower().rstrip(".,") in _QTY_UNITS:
        unit = toks[0].lower().rstrip(".,")
        toks = toks[1:]
    if toks and toks[0].lower() == "of":
        toks = toks[1:]
    part = " ".join(toks).strip(" ,.")
    return {"qty": qty, "unit": unit, "part": part}


def _rfq_config() -> dict:
    dflt = {"company": "American Power Electrical Supply Co.",
            "sender": "", "phone": "", "greeting": "Hello,",
            "closing": "Please include pricing and availability. Thank you,"}
    try:
        with open(RFQ_CONFIG, encoding="utf-8") as f:
            dflt.update(json.load(f))
    except Exception:  # noqa: BLE001
        try:
            with open(RFQ_CONFIG, "w", encoding="utf-8") as f:
                json.dump(dflt, f, indent=2)
        except OSError:
            pass
    return dflt


def _next_ref() -> str:
    day = datetime.now().strftime("%Y%m%d")
    os.makedirs(RFQ_DIR, exist_ok=True)
    n = 1 + sum(1 for f in os.listdir(RFQ_DIR)
                if f.startswith(f"RFQ_VR-{day}-") and f.endswith(".json"))
    return f"VR-{day}-{n:03d}"


def render_rfq_email(rfq: dict) -> tuple[str, str]:
    """-> (subject, body) plain text."""
    cfg = _rfq_config()
    job = rfq.get("job") or "Material Request"
    subject = f"RFQ {rfq['ref']} — {job}"
    lines = [cfg["greeting"], "",
             f"Please quote the following ({job}):", ""]
    for i, ln in enumerate(rfq.get("lines", []), 1):
        qty = ln.get("qty", 1)
        unit = (" " + ln["unit"]) if ln.get("unit") else ""
        note = f"   ({ln['note']})" if ln.get("note") else ""
        lines.append(f"  {i}. {qty}{unit}  {ln.get('part','')}{note}")
    if rfq.get("note"):
        lines += ["", f"Notes: {rfq['note']}"]
    lines += ["", cfg["closing"], "",
              cfg.get("sender") or cfg["company"]]
    if cfg.get("sender"):
        lines.append(cfg["company"])
    if cfg.get("phone"):
        lines.append(cfg["phone"])
    lines += ["", f"[Ref {rfq['ref']} — sent via MaINbox Voice]"]
    return subject, "\n".join(lines)


def _try_outlook_send(to_addr: str, subject: str, body: str):
    """Send via Outlook COM if available on this PC. Returns (ok, detail)."""
    try:
        import win32com.client  # type: ignore
    except Exception:  # noqa: BLE001
        return False, "win32com not on this PC (queued for the Outlook PC)"
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        mail = ol.CreateItem(0)
        mail.To = to_addr
        mail.Subject = subject
        mail.Body = body
        mail.Send()
        return True, "sent via Outlook"
    except Exception as e:  # noqa: BLE001
        return False, f"Outlook send failed: {e}"


def _try_outlook_draft(to_addr: str, subject: str, body: str):
    """v0.8.0: create a real draft in Outlook's Drafts folder (Save, never
    Send). Returns (ok, entry_id, detail). The EntryID lets us later send
    THAT draft instead of composing a duplicate."""
    try:
        import win32com.client  # type: ignore
    except Exception:  # noqa: BLE001
        return False, "", "no Outlook on this PC (companion will create it)"
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        mail = ol.CreateItem(0)
        mail.To = to_addr
        mail.Subject = subject
        mail.Body = body
        mail.Save()                      # -> Drafts folder
        return True, str(mail.EntryID or ""), "draft in Outlook Drafts"
    except Exception as e:  # noqa: BLE001
        return False, "", f"Outlook draft failed: {e}"


def _send_vendor(v: dict, subject: str, body: str):
    """v0.8.0: send to one vendor, preferring the existing Outlook draft
    (by EntryID) so releasing never duplicates the mail. Falls back to a
    fresh compose+send. Returns (ok, detail)."""
    eid = v.get("outlook_entry_id") or ""
    if eid:
        try:
            import win32com.client  # type: ignore
            ol = win32com.client.Dispatch("Outlook.Application")
            ns = ol.GetNamespace("MAPI")
            item = ns.GetItemFromID(eid)
            item.Send()
            return True, "sent the Outlook draft"
        except Exception as e:  # noqa: BLE001
            log.info("entry-id send failed (%s) — composing fresh", e)
    return _try_outlook_send(v["email"], subject, body)


def _vendor_list(data: dict) -> list[dict]:
    """Accept vendors:[...] (new) or vendor_email:"a@b, c@d" (legacy).
    -> [{email, name, status:'queued', sent_ts:None, detail:''}]"""
    raw = data.get("vendors")
    if isinstance(raw, list):
        emails = []
        for v in raw:
            if isinstance(v, dict):
                emails.append((v.get("email", ""), v.get("name", "")))
            else:
                emails.append((str(v), ""))
    else:
        emails = [(e, "") for e in
                  re.split(r"[,;\s]+", data.get("vendor_email") or "")]
    out, seen = [], set()
    for em, nm in emails:
        em = em.strip()
        if "@" in em and em.lower() not in seen:
            seen.add(em.lower())
            out.append({"email": em, "name": nm.strip(),
                        "status": "queued", "sent_ts": None, "detail": ""})
    return out


def _rollup(vendors: list[dict]) -> str:
    st = {v.get("status") for v in vendors}
    if st == {"draft"}:
        return "draft"              # held for review; nothing sends these
    if "draft" in st or "queued" in st:
        # any vendor not out the door yet
        pending_only = st <= {"draft", "queued"}
        return "queued" if pending_only else "partial"
    if st == {"replied"}:
        return "replied"
    return "sent"          # all out the door; some may have replied


# ---- lifecycle: stages, attention flags, manual events -----------------------
RFQ_EVENTS = ("replied", "awarded", "po_sent", "delivered", "closed")
_STAGE_ORDER = ("draft", "queued", "partial", "sent", "replied", "awarded",
                "po_sent", "delivered", "closed")


def derive_stage(rfq: dict) -> str:
    """Furthest point this RFQ has reached, from timeline + vendor states."""
    events = {e.get("event") for e in rfq.get("timeline", [])}
    for s in reversed(_STAGE_ORDER[3:]):        # closed..replied
        if s in events:
            return s
    if any(v.get("status") == "replied" for v in rfq.get("vendors", [])):
        return "replied"
    return rfq.get("status", "queued")


def rfq_attention(rfq: dict, now: float | None = None) -> tuple[str, list[str]]:
    """(health, flags). health: ok|warn|bad. Flags only from signals we
    actually have — no invented composite score. Wording is careful: before
    reply matching exists, 'no reply' really means 'no reply recorded'."""
    now = now or time.time()
    flags: list[str] = []
    health = "ok"
    if rfq.get("status") == "deleted":
        return "ok", []                       # hidden; no attention
    if rfq.get("status") == "draft":
        # intentionally held for review — a gentle nudge, never an alarm
        return "warn", ["draft — review & send when ready"]
    stage = derive_stage(rfq)
    if stage in ("awarded", "po_sent", "delivered", "closed"):
        return "ok", []
    vendors = rfq.get("vendors", [])
    failed = [v["email"] for v in vendors if v.get("status") == "queued"
              and "failed" in (v.get("detail") or "").lower()]
    if failed:
        flags.append("send failed: " + ", ".join(failed))
        health = "bad"
    age_h = (now - rfq.get("created_ts", now)) / 3600.0
    if rfq.get("status") == "queued" and age_h > 6 and not failed:
        flags.append(f"still queued after {age_h:.0f}h — is the Outlook "
                     "PC's sender running?")
        health = "bad" if age_h > 24 else "warn"
    elif rfq.get("status") == "partial" and age_h > 6:
        still = [v["email"] for v in vendors if v.get("status") == "queued"
                 and v["email"] not in failed]
        if still:
            flags.append("not yet sent to: " + ", ".join(still))
            if health == "ok":
                health = "warn"
    sent_ts = [v.get("sent_ts") for v in vendors if v.get("sent_ts")]
    if sent_ts and stage == "sent":
        days = (now - min(sent_ts)) / 86400.0
        no_reply = [v["email"] for v in vendors
                    if v.get("status") == "sent"]
        if days >= 3 and no_reply:
            flags.append(f"no reply recorded after {days:.0f}d from: "
                         + ", ".join(no_reply))
            if health == "ok":
                health = "warn"
    return health, flags


def _already_recorded(rfq: dict, msg_key: str) -> bool:
    return any(e.get("msg_key") == msg_key
               for e in rfq.get("timeline", []))


def record_reply(subject: str, body: str, sender: str) -> dict:
    """Automatic reply matching. Forward an inbound vendor email here and it
    finds the RFQ(s) by ref, ties the sender to a vendor, appends a 'replied'
    event (idempotently), refreshes coverage, and notifies the phone — no
    clicks. This is the producer of the same 'replied' events the lifecycle
    already consumes. Returns a summary of what it did."""
    if rmatch is None:
        return {"ok": False, "error": "rfq_match module not on the server"}
    refs = rmatch.extract_refs(subject or "", body or "")
    if not refs:
        return {"ok": True, "matched": False, "recorded": [],
                "reason": "no RFQ ref in the email"}
    candidates = [r for r in (_load_rfq(ref) for ref in refs) if r]
    res = rmatch.match_email(subject or "", body or "", sender or "",
                             candidates)
    recorded, skipped = [], []
    for mt in res.get("matches", []):
        ref = mt["ref"]
        rfq = _load_rfq(ref)
        if not rfq:
            continue
        if _already_recorded(rfq, mt["msg_key"]):
            skipped.append(ref)
            continue
        vendor = mt["vendor"]
        snippet = (subject or "").strip()[:80]
        note = (f"via {mt['via']}, {mt['method']}, "
                f"conf {mt['confidence']:.2f}"
                + (f" — from {sender}" if sender else ""))
        first_reply = False
        if vendor:
            for v in rfq.get("vendors", []):
                if v["email"].lower() == vendor.lower():
                    if v.get("status") != "replied":
                        v["status"] = "replied"
                        first_reply = True
            detail = f"{snippet} ({note})"
        else:
            # ref known, sender not tied to a vendor: real reply, no flip
            first_reply = True
            detail = f"{snippet} — unmatched sender ({note})"
        e = {"ts": time.time(), "event": "replied", "detail": detail[:300],
             "msg_key": mt["msg_key"]}
        if vendor:
            e["vendor"] = vendor
        rfq.setdefault("timeline", []).append(e)
        rfq["status"] = _rollup(rfq.get("vendors", []))
        _save_rfq(rfq)
        _write_coverage(rfq)
        if first_reply:
            who = vendor or (sender or "a vendor")
            push_notification(
                f"Reply on RFQ {ref}",
                f"{who} replied — {rfq.get('job') or 'no job name'}"
                + ("" if vendor else " (sender didn't match a known vendor)"))
        recorded.append({"ref": ref, "vendor": vendor,
                         "method": mt["method"], "via": mt["via"],
                         "confidence": mt["confidence"]})
    return {"ok": True, "matched": bool(recorded or skipped),
            "recorded": recorded, "skipped_duplicates": skipped,
            "reason": res.get("reason", "")}


def add_rfq_event(ref: str, event: str, vendor: str = "",
                  detail: str = "") -> dict:
    """Manual lifecycle append. Same event vocabulary automation will use
    later, so the timeline never needs a schema change."""
    if event not in RFQ_EVENTS:
        return {"ok": False,
                "error": f"event must be one of {', '.join(RFQ_EVENTS)}"}
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    vendor = (vendor or "").strip()
    if event == "replied" and vendor:
        hit = False
        for v in rfq.get("vendors", []):
            if v["email"].lower() == vendor.lower():
                v["status"] = "replied"
                hit = True
        if not hit:
            return {"ok": False, "error": f"{vendor} is not on this RFQ"}
    _tl(rfq, event, vendor=vendor, detail=(detail or "").strip()[:300]
        or ("marked by hand" if event != "replied" else ""))
    rfq["status"] = _rollup(rfq.get("vendors", []))
    _save_rfq(rfq)
    _write_coverage(rfq)
    return {"ok": True, "rfq": _with_computed(rfq)}


def _with_computed(rfq: dict) -> dict:
    """Attach derived fields (never stored — computed on read)."""
    out = dict(rfq)
    out["stage"] = derive_stage(rfq)
    out["health"], out["attention"] = rfq_attention(rfq)
    return out


def _tl(rfq: dict, event: str, **kw) -> None:
    """Append to the RFQ's append-only timeline. Never rewrite history."""
    e = {"ts": time.time(), "event": event}
    e.update({k: v for k, v in kw.items() if v})
    rfq.setdefault("timeline", []).append(e)


def _rfq_path(ref: str) -> str:
    return os.path.join(RFQ_DIR, f"RFQ_{ref}.json")


def _load_rfq(ref: str) -> dict | None:
    try:
        with open(_rfq_path(ref), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _save_rfq(rfq: dict) -> None:
    os.makedirs(RFQ_DIR, exist_ok=True)
    with open(_rfq_path(rfq["ref"]), "w", encoding="utf-8") as f:
        json.dump(rfq, f, indent=2)


def _write_coverage(rfq: dict) -> None:
    """Quote Coverage handoff — one record per RFQ, vendors as a list."""
    os.makedirs(RFQ_COVERAGE_DIR, exist_ok=True)
    cov = {"source": "mainbox_voice", "schema": 2, "ref": rfq["ref"],
           "created_ts": rfq["created_ts"], "job": rfq["job"],
           "vendors": [{"email": v["email"], "name": v.get("name", ""),
                        "status": v["status"]} for v in rfq["vendors"]],
           "lines": rfq["lines"], "email_subject": rfq["email_subject"],
           "status": rfq["status"]}
    with open(os.path.join(RFQ_COVERAGE_DIR,
                           f"RFQ_{rfq['ref']}_coverage.json"),
              "w", encoding="utf-8") as f:
        json.dump(cov, f, indent=2)


def preview_rfq(data: dict) -> dict:
    """Render subject/body without persisting anything. The ref shown is the
    next one; it's only consumed when the RFQ is actually sent."""
    lines = [ln for ln in (data.get("lines") or [])
             if (ln.get("part") or "").strip()]
    if not lines:
        return {"ok": False, "error": "at least one line item required"}
    tmp = {"ref": _next_ref(), "job": (data.get("job") or "").strip(),
           "note": (data.get("note") or "").strip(),
           "lines": lines}
    subject, body = render_rfq_email(tmp)
    vend = _vendor_list(data)
    return {"ok": True, "subject": subject, "body": body,
            "vendors": [v["email"] for v in vend],
            "ref_note": "ref is assigned when you send"}


def create_rfq(data: dict, draft: bool = False) -> dict:
    """Validate, render, persist queue + coverage handoff.
    draft=False (RFQ tab's explicit Send): try to send to every vendor now.
    draft=True  (voice-created): save as a DRAFT — nothing sends it until the
    user reviews and releases it ('send VR-...' by voice, or Send on the RFQ
    tab card). This is the safety net for misheard speech."""
    vendors = _vendor_list(data)
    lines = [ln for ln in (data.get("lines") or [])
             if (ln.get("part") or "").strip()]
    if not vendors:
        return {"ok": False, "error": "at least one vendor email required"}
    if not lines:
        return {"ok": False, "error": "at least one line item required"}
    rfq = {"schema": 2,
           "ref": _next_ref(),
           "created_ts": time.time(),
           "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "job": (data.get("job") or "").strip(),
           "note": (data.get("note") or "").strip(),
           "vendors": vendors,
           "lines": [{"qty": ln.get("qty", 1),
                      "unit": (ln.get("unit") or "").strip(),
                      "part": ln["part"].strip(),
                      "note": (ln.get("note") or "").strip()}
                     for ln in lines],
           "status": "queued", "timeline": []}
    _tl(rfq, "created", detail=f"{len(lines)} line(s), "
                               f"{len(vendors)} vendor(s)"
                               + (" — draft (held for review)" if draft
                                  else ""))
    subject, body = render_rfq_email(rfq)
    rfq["email_subject"], rfq["email_body"] = subject, body
    if draft:
        for v in rfq["vendors"]:
            v["status"] = "draft"
            ok, eid, detail = _try_outlook_draft(v["email"], subject, body)
            if ok:
                v["outlook_entry_id"] = eid
                v["detail"] = detail
                _tl(rfq, "outlook_draft", vendor=v["email"], detail=detail)
            else:
                v["detail"] = f"draft — held for review ({detail})"
    else:
        for v in rfq["vendors"]:
            ok, detail = _try_outlook_send(v["email"], subject, body)
            v["detail"] = detail
            if ok:
                v["status"], v["sent_ts"] = "sent", time.time()
                _tl(rfq, "sent", vendor=v["email"], detail=detail)
            else:
                _tl(rfq, "queued", vendor=v["email"], detail=detail)
    rfq["status"] = _rollup(rfq["vendors"])
    _save_rfq(rfq)
    _write_coverage(rfq)
    n_sent = sum(1 for v in rfq["vendors"] if v["status"] == "sent")
    n_q = len(rfq["vendors"]) - n_sent
    if draft:
        summary = "draft (held for review)"
    else:
        summary = (f"{n_sent} sent" if n_sent else "") + \
                  (", " if n_sent and n_q else "") + \
                  (f"{n_q} queued" if n_q else "")
    push_notification(f"RFQ {rfq['ref']} — {summary}",
                      f"{len(rfq['lines'])} line(s)"
                      + (f" — {rfq['job']}" if rfq["job"] else ""))
    return {"ok": True, "ref": rfq["ref"], "status": rfq["status"],
            "vendors": [{"email": v["email"], "status": v["status"],
                         "detail": v["detail"]} for v in rfq["vendors"]],
            "subject": subject, "body": body}


def release_rfq(ref: str) -> dict:
    """Release a draft: try to send right now if Outlook is on this PC,
    otherwise flip vendors draft→queued so the companion sends them."""
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    if rfq.get("status") == "deleted":
        return {"ok": False, "error": f"{ref} is deleted — restore it first"}
    subject = rfq.get("email_subject", f"RFQ {ref}")
    body = rfq.get("email_body", "")
    n_sent = n_q = 0
    for v in rfq.get("vendors", []):
        if v.get("status") != "draft":
            continue
        ok, detail = _send_vendor(v, subject, body)
        v["detail"] = detail
        if ok:
            v["status"], v["sent_ts"] = "sent", time.time()
            _tl(rfq, "sent", vendor=v["email"], detail=detail)
            n_sent += 1
        else:
            v["status"] = "queued"
            _tl(rfq, "queued", vendor=v["email"],
                detail="released from draft; " + detail)
            n_q += 1
    if not (n_sent or n_q):
        return {"ok": False, "error": f"{ref} has no draft vendors to send"}
    rfq["status"] = _rollup(rfq["vendors"])
    _save_rfq(rfq)
    _write_coverage(rfq)
    return {"ok": True, "ref": ref, "sent": n_sent, "queued": n_q,
            "status": rfq["status"]}


def delete_rfq(ref: str) -> dict:
    """Soft-delete: hide from lists/voice/attention, keep the record and its
    full timeline so it can be reviewed and restored. Never hard-deletes."""
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    if rfq.get("status") == "deleted":
        return {"ok": True, "ref": ref, "status": "deleted",
                "note": "already deleted"}
    rfq["status_before_delete"] = rfq.get("status", "queued")
    rfq["status"] = "deleted"
    _tl(rfq, "deleted", detail=f"was {rfq['status_before_delete']}")
    _save_rfq(rfq)
    return {"ok": True, "ref": ref, "status": "deleted"}


def restore_rfq(ref: str) -> dict:
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    if rfq.get("status") != "deleted":
        return {"ok": True, "ref": ref, "status": rfq.get("status"),
                "note": "not deleted"}
    rfq["status"] = _rollup(rfq.get("vendors", [])) or \
        rfq.pop("status_before_delete", "queued")
    rfq.pop("status_before_delete", None)
    rfq["hidden"] = False               # restoring always unhides too
    _tl(rfq, "restored", detail=f"back to {rfq['status']}")
    _save_rfq(rfq)
    return {"ok": True, "ref": ref, "status": rfq["status"]}


def hide_rfq(ref: str) -> dict:
    """v0.8.0: hide from the live list without deleting — status untouched,
    still restorable from the Hidden & deleted section."""
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    if rfq.get("hidden"):
        return {"ok": True, "ref": ref, "note": "already hidden"}
    rfq["hidden"] = True
    _tl(rfq, "hidden", detail=f"status stays {rfq.get('status')}")
    _save_rfq(rfq)
    return {"ok": True, "ref": ref, "hidden": True}


def unhide_rfq(ref: str) -> dict:
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    rfq["hidden"] = False
    _tl(rfq, "unhidden")
    _save_rfq(rfq)
    return {"ok": True, "ref": ref, "hidden": False}


def preview_saved_rfq(ref: str) -> dict:
    """v0.8.0: the exact email a saved RFQ will send, plus everything the
    editor needs to prefill (vendors/lines/job/note)."""
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    return {"ok": True, "ref": ref,
            "status": rfq.get("status"),
            "hidden": bool(rfq.get("hidden")),
            "subject": rfq.get("email_subject", ""),
            "body": rfq.get("email_body", ""),
            "to": [v.get("email", "") for v in rfq.get("vendors", [])],
            "vendors": [{"email": v.get("email", ""),
                         "status": v.get("status", "")}
                        for v in rfq.get("vendors", [])],
            "lines": rfq.get("lines", []),
            "job": rfq.get("job", ""), "note": rfq.get("note", "")}


def update_rfq(data: dict) -> dict:
    """v0.8.0: edit a DRAFT in place — vendors/lines/job/note — re-render the
    email, refresh any Outlook drafts (delete stale, create new), and stamp
    the timeline. Only drafts are editable; released RFQs are history."""
    ref = (data.get("ref") or "").strip()
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    if rfq.get("status") not in ("draft",):
        return {"ok": False, "error":
                f"{ref} is {rfq.get('status')} — only drafts can be edited"}
    vendors = _vendor_list(data)
    lines = [ln for ln in (data.get("lines") or [])
             if (ln.get("part") or "").strip()]
    if not vendors or not lines:
        return {"ok": False,
                "error": "need at least one vendor and one line"}
    # drop any stale Outlook drafts from the previous version (best effort)
    for v in rfq.get("vendors", []):
        eid = v.get("outlook_entry_id")
        if not eid:
            continue
        try:
            import win32com.client  # type: ignore
            ol = win32com.client.Dispatch("Outlook.Application")
            ns = ol.GetNamespace("MAPI")
            ns.GetItemFromID(eid).Delete()
        except Exception:  # noqa: BLE001
            pass
    rfq["job"] = (data.get("job") or "").strip()
    rfq["note"] = (data.get("note") or "").strip()
    rfq["vendors"] = vendors
    rfq["lines"] = [{"qty": ln.get("qty", 1),
                     "unit": (ln.get("unit") or "").strip(),
                     "part": ln["part"].strip(),
                     "note": (ln.get("note") or "").strip()} for ln in lines]
    subject, body = render_rfq_email(rfq)
    rfq["email_subject"], rfq["email_body"] = subject, body
    for v in rfq["vendors"]:
        v["status"] = "draft"
        ok, eid, detail = _try_outlook_draft(v["email"], subject, body)
        if ok:
            v["outlook_entry_id"] = eid
            v["detail"] = detail
        else:
            v["detail"] = f"draft — held for review ({detail})"
    _tl(rfq, "edited", detail=f"{len(lines)} line(s), {len(vendors)} "
                              f"vendor(s)")
    rfq["status"] = "draft"
    _save_rfq(rfq)
    _write_coverage(rfq)
    return {"ok": True, "ref": ref, "status": "draft",
            "vendors": rfq["vendors"]}


def add_rfq_note(ref: str, text: str) -> dict:
    rfq = _load_rfq(ref)
    if not rfq:
        return {"ok": False, "error": f"no RFQ {ref}"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty note"}
    _tl(rfq, "note", detail=text[:500])
    _save_rfq(rfq)
    return {"ok": True, "timeline": rfq["timeline"]}


def list_rfqs(limit: int = 15, include_deleted: bool = False) -> list[dict]:
    if not os.path.isdir(RFQ_DIR):
        return []
    out = []
    for name in sorted(os.listdir(RFQ_DIR), reverse=True):
        if not (name.startswith("RFQ_") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(RFQ_DIR, name), encoding="utf-8") as f:
                r = json.load(f)
            if r.get("status") == "deleted" and not include_deleted:
                continue
            if r.get("schema", 1) >= 2:
                vends = r.get("vendors", [])
                emails = [v["email"] for v in vends]
                n_sent = sum(1 for v in vends
                             if v.get("status") in ("sent", "replied"))
                stage = derive_stage(r)
                health, attention = rfq_attention(r)
            else:  # legacy single-vendor record
                emails = [r.get("vendor_email", "")]
                n_sent = 1 if r.get("status") == "sent" else 0
                stage = r.get("status", "queued")
                health, attention = "ok", []
            out.append({"ref": r.get("ref"), "created": r.get("created"),
                        "job": r.get("job"),
                        "vendor_emails": emails,
                        "n_vendors": len(emails), "n_sent": n_sent,
                        "n_lines": len(r.get("lines", [])),
                        "status": r.get("status"),
                        "hidden": bool(r.get("hidden")),
                        "stage": stage, "health": health,
                        "attention": attention})
        except Exception:  # noqa: BLE001
            continue
        if len(out) >= limit:
            break
    return out


def recent_vendors(limit: int = 8) -> list[str]:
    seen, out = set(), []
    for r in list_rfqs(50):
        for v in r.get("vendor_emails", []):
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return out


# ---- voice status queries over the RFQ data we already have ------------------
_RFQ_Q_RE = re.compile(
    r"\brfqs?\b|\bwho owes\b|\bneeds? attention\b|\bwaiting (?:on|for)\b"
    r"|\bstill waiting\b|\boutstanding\b|\bawarded\b"
    r"|\b(?:answered|unanswered|replied|responded)\b", re.IGNORECASE)
_REF_RE = re.compile(r"\bvr[-\s]?(\d{8})[-\s]?(\d{3})\b", re.IGNORECASE)


_GENERIC_MAIL = {"gmail", "yahoo", "hotmail", "outlook", "aol", "icloud",
                 "live", "msn", "comcast", "verizon", "att"}


def _vendor_fragments(summaries: list[dict]) -> dict[str, str]:
    """{'graybar': 'quotes@graybar.com', ...} — searchable vendor-name
    fragments. Uses the DOMAIN only, never the email local-part: people refer
    to a vendor by name ('Graybar'), and local-parts like 'quotes'/'rfq'/'bids'
    would otherwise collide with ordinary query words ('waiting for quotes')."""
    frags: dict[str, str] = {}
    for r in summaries:
        for em in r.get("vendor_emails", []):
            dom = em.split("@")[-1].split(".")[0].lower()
            if len(dom) >= 4 and dom not in _GENERIC_MAIL:
                frags.setdefault(dom, em)
    return frags


def _say_rfq(r: dict) -> str:
    extra = f" — {r['job']}" if r.get("job") else ""
    return (f"{r['ref']}{extra}: {r['stage']}, "
            f"{r['n_sent']}/{r['n_vendors']} out")


def _rfq_voice_answer(text: str) -> dict | None:
    """Answer RFQ status questions from existing timelines. Returns a
    handle_query-shaped dict, or None to fall through to xref parsing."""
    m = _REF_RE.search(text or "")
    if not m and not _RFQ_Q_RE.search(text or ""):
        return None
    out = {"reply": "", "speak": "", "results": [], "action": "rfq_status"}
    low = (text or "").lower()

    if m:  # ---- one specific RFQ ------------------------------------------
        ref = f"VR-{m.group(1)}-{m.group(2)}"
        rfq = _load_rfq(ref)
        if not rfq:
            out["reply"] = out["speak"] = f"I don't have an RFQ {ref}."
            return out
        c = _with_computed(rfq)
        lines = [f"{ref} — {c.get('job') or 'no job name'} — stage: "
                 f"{c['stage']}"]
        for v in c.get("vendors", []):
            lines.append(f"  {v['email']}: {v['status']}")
        for fl in c["attention"]:
            lines.append(f"  ⚠ {fl}")
        tl = c.get("timeline", [])[-3:]
        if tl:
            lines.append("  recent: " + "; ".join(
                e["event"] + (" " + e.get("vendor", "") if e.get("vendor")
                              else "") for e in tl))
        out["reply"] = "\n".join(lines)
        n_rep = sum(1 for v in c.get("vendors", [])
                    if v.get("status") == "replied")
        out["speak"] = (f"{ref} is at {c['stage']}. "
                        f"{n_rep} of {len(c.get('vendors', []))} vendors "
                        "have replied."
                        + (f" Heads up: {c['attention'][0]}"
                           if c["attention"] else ""))
        return out

    summaries = list_rfqs(50)
    if not summaries:
        out["reply"] = out["speak"] = "No RFQs yet."
        return out
    frags = _vendor_fragments(summaries)
    vend_filter = next((em for f, em in frags.items() if f in low), None)
    if vend_filter:
        summaries = [r for r in summaries
                     if vend_filter in r.get("vendor_emails", [])]

    if re.search(r"\bneeds? attention\b", low):  # ---- attention ----------
        hot = [r for r in summaries if r["health"] != "ok"]
        if not hot:
            out["reply"] = out["speak"] = \
                "Nothing needs attention right now."
            return out
        lines = []
        for r in hot:
            lines.append(_say_rfq(r))
            for fl in r["attention"]:
                lines.append(f"  ⚠ {fl}")
        out["reply"] = "\n".join(lines)
        out["speak"] = (f"{len(hot)} RFQ{'s need' if len(hot) != 1 else ' needs'} "
                        f"attention. {hot[0]['ref']}: "
                        f"{hot[0]['attention'][0] if hot[0]['attention'] else hot[0]['stage']}")
        return out

    if re.search(r"\bawarded\b", low):  # ---- awarded ----------------------
        won = [r for r in summaries
               if r["stage"] in ("awarded", "po_sent", "delivered", "closed")]
        if not won:
            out["reply"] = out["speak"] = "No RFQs marked awarded yet."
            return out
        out["reply"] = "\n".join(_say_rfq(r) for r in won)
        out["speak"] = f"{len(won)} awarded. Latest: {won[0]['ref']}."
        return out

    # ---- default: waiting / owes / outstanding / generic rfq --------------
    waiting = [r for r in summaries
               if r["stage"] in ("queued", "partial", "sent")]
    who = f" from {vend_filter}" if vend_filter else ""
    if not waiting:
        out["reply"] = out["speak"] = \
            f"Nothing is waiting on quotes{who}. All caught up."
        return out
    out["reply"] = f"Waiting on quotes{who}:\n" + \
        "\n".join(_say_rfq(r) for r in waiting)
    top = ". ".join(_say_rfq(r) for r in waiting[:3])
    out["speak"] = (f"{len(waiting)} RFQ{'s' if len(waiting) != 1 else ''} "
                    f"still waiting{who}. {top}.")
    return out


# ==============================================================================
# QUERY HANDLING (voice/chat -> xref toolkit)
# ==============================================================================
def _speakable_findings(part: str, findings: list) -> str:
    if not findings:
        return f"I didn't find any equivalents for {part}."
    bits = []
    for i, f in enumerate(findings[:3], 1):
        status = ", confirmed" if getattr(f, "status", "") == "confirmed" \
            else ""
        bits.append(f"Number {i}: {f.equiv_mfr} {f.equiv_part}{status}")
    more = f", plus {len(findings) - 3} more on screen" \
        if len(findings) > 3 else ""
    plural = "equivalents" if len(findings) != 1 else "equivalent"
    return (f"Found {len(findings)} {plural} for {part}. "
            + ". ".join(bits) + more + ".")


_CROSSREF_RE = re.compile(
    r"\b(equals?|equivalents?|cross(?:[- ]?ref(?:erence)?)?|interchange(?:able)?"
    r"|substitut(?:e|ion)|sub for|instead of|comparable|"
    r"what can i use|replace\b.*\bwith)\b",
    re.IGNORECASE)


_TEACH_RE = re.compile(
    r"\b(?:remember|note|save|learn|teach\s+(?:you|it|that)?)\b",
    re.IGNORECASE)
# "A is (an) equal to B", "A equals B", "A is the same as B",
# "A cross(es) to B", "A is equivalent to B"
_EQUIV_LINK_RE = re.compile(
    r"\bis\s+(?:also\s+)?(?:an?\s+)?equal\s+to\b"
    r"|\b(?:is\s+)?equivalent\s+to\b"
    r"|\bequals\b"
    r"|\bis\s+the\s+same\s+as\b"
    r"|\bcross(?:es)?\s+(?:to|with)\b"
    r"|\b=\b",
    re.IGNORECASE)


def _parse_teach_equivalence(text: str):
    """Detect 'remember A is an equal to B' and return {part_a, part_b}.
    Requires an explicit teach word (remember/save/note/teach) so normal
    'what's equal to X' questions are never treated as teaching."""
    if not _TEACH_RE.search(text or ""):
        return None
    m = _EQUIV_LINK_RE.search(text or "")
    if not m:
        # v0.8.2: "remember ilsco ik250 is also an equal" (no 'to Y') pairs X
        # with the part whose equals were just shown.
        m_ctx = re.search(
            r"^(.*?)\bis\s+(?:also\s+)?(?:an?\s+)?equal\s*[.!]?\s*$",
            (text or "").strip(), re.IGNORECASE)
        if m_ctx:
            with _state_lock:
                last_part = SESSION.get("last_xref_part") or ""
            if last_part:
                before = re.sub(
                    r"^\s*(?:please\s+)?(?:remember|note|save|learn"
                    r"|teach\s+(?:you|it|that)?)\s+(?:that\s+)?", "",
                    m_ctx.group(1), flags=re.IGNORECASE).strip(" .,!?")
                if len(before) >= 2:
                    return {"part_a": before, "part_b": last_part}
        return None
    before = text[:m.start()].strip()
    after = text[m.end():].strip()
    # strip the leading teach word from the 'before' part
    before = re.sub(r"^\s*(?:please\s+)?(?:remember|note|save|learn"
                    r"|teach\s+(?:you|it|that)?)\s+(?:that\s+)?", "",
                    before, flags=re.IGNORECASE).strip()
    # strip trailing pleasantries
    after = re.sub(r"\b(thanks?|thank you|please|for me|okay|ok)\b.*$", "",
                   after, flags=re.IGNORECASE).strip(" .,!?")
    before = before.strip(" .,!?")
    if len(before) >= 2 and len(after) >= 2:
        return {"part_a": before, "part_b": after}
    return None


def _is_cross_ref_query(text: str) -> bool:
    """True when the user is explicitly asking for a cross-reference /
    equivalent — those stay on the local taught-sites engine. Everything else
    is a general question for the Brain."""
    return bool(_CROSSREF_RE.search(text or ""))


def ask_brain(text: str, session_id):
    """POST a question to the MaINbox Brain's /ask endpoint. Raises on any
    connection problem (caller handles the offline case)."""
    import urllib.request
    body = json.dumps({"text": text, "session": session_id}).encode()
    req = urllib.request.Request(BRAIN_URL + "/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    # LLM answers on a 12B model over the tailnet can take a while; give the
    # Brain generous headroom before giving up (overridable via env).
    _t = int(os.environ.get("MBB_BRAIN_TIMEOUT", "180"))
    with urllib.request.urlopen(req, timeout=_t) as r:
        return json.loads(r.read().decode("utf-8"))


_LLM_PREAMBLE_RE = re.compile(
    r"^(?:"
    r"here(?:'s| is)(?: a)? rewritten version of[^:\n]{0,80}[:.]\s*"
    r"|here(?:'s| is)(?: a)?(?: rewritten)?(?: version of the)? (?:answer|response|reply)[:\.]?\s*"
    r"|based on (?:the )?(?:information|data)(?:\s+(?:provided|available|above))?[,:]?\s*"
    r"|i(?:'ve)? (?:reviewed|checked|found|looked at|analyzed) (?:our )?(?:records?|data|system|previous (?:quotes?|purchases?))[,.]?\s*"
    r"|(?:let me|i(?:'ll)?) (?:provide|give you|share)[^.]{0,60}\.\s*"
    r")+",
    re.IGNORECASE | re.DOTALL)

# dates that are stale (>180 days from today when the server ran)
_DATE_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(\d{4})\b"
    r"|\b(\d{1,2})/(\d{1,2})/(\d{4})\b", re.IGNORECASE)

_SERVER_START = datetime.now()


def _clean_brain_reply(msg: str) -> str:
    """Strip LLM meta-preambles and flag stale dates in Brain answers."""
    msg = _LLM_PREAMBLE_RE.sub("", msg).strip()
    # flag any year that is >12 months before today
    def _flag(m):
        yr_str = m.group(1) or m.group(4) or ""
        if yr_str:
            try:
                age_months = (_SERVER_START.year - int(yr_str)) * 12 + \
                             _SERVER_START.month
                if age_months > 12:
                    return m.group(0) + " ⚠️(>1 yr old)"
            except Exception:  # noqa: BLE001
                pass
        return m.group(0)
    return _DATE_RE.sub(_flag, msg)


# ---- active RFQ request detection ----------------------------------------
# "get price and availability from Brazill and Thea for 10,000ft of 12/2 MC"
# = send RFQ to vendors NOW  ≠  "what did we pay Brazill" (historical)
_ACTIVE_RFQ_RE = re.compile(
    r"\b(?:"
    # get/pull/need + price/quote/availability + from
    r"(?:get|pull|need(?:\s+a)?|request)(?:\s+me)?"
    r"(?:\s+(?:a\s+)?(?:price|pricing|availability|avail(?:ability)?"
    r"|quote|quotes?|bid|cost(?:ing)?))+"
    r"(?:\s+and\s+(?:a\s+)?(?:price|pricing|availability|avail(?:ability)?"
    r"|quote|quotes?|bid|cost(?:ing)?))*"
    r"\s+from"
    r"|"
    # send/create/setup/draft RFQ(s)/quote(s)/draft(s) to OR for
    r"(?:send|fire|shoot|create|draft|make|set\s*up|setup|prep(?:are)?|build)"
    r"(?:\s+(?:an?|the|some))?\s+(?:rfqs?|quotes?|requests?|bids?|drafts?)"
    r"\s+(?:to|for)"
    r"|"
    # get me a quote from
    r"get\s+me\s+(?:a\s+)?(?:price|quote|bid)\s+from"
    r"|"
    # ask X for pricing/a price/a quote (vendor comes before 'for')
    r"ask\s+.{3,40}\s+for\s+(?:a\s+)?(?:price|pricing|quote|bid|availability)"
    r"|"
    # follow-up: "send it/that/this to X", "yes send to X", "send to X and Y"
    r"(?:yes,?\s+)?(?:please\s+)?send\s+(?:it|that|this|them)?\s*to"
    r")\b",
    re.IGNORECASE)


# follow-up phrasing that reuses the last-quoted item ("setup drafts for X",
# "send it to X") — vendor comes after "for" or "to", NO new item text
_FOLLOWUP_RFQ_RE = re.compile(
    r"\b(?:send|fire|shoot|create|draft|make|set\s*up|setup|prep(?:are)?"
    r"|build)(?:\s+(?:an?|the|some|it|that|this|them))*"
    r"\s+(?:rfqs?|quotes?|requests?|bids?|drafts?)?\s*(?:to|for)\s+(.+)$"
    r"|\b(?:yes,?\s+)?(?:please\s+)?send\s+(?:it|that|this|them)?\s*to\s+(.+)$",
    re.IGNORECASE)


def _is_active_rfq_request(text: str) -> bool:
    """True when the user wants to *send* RFQs to named vendors right now,
    not look up historical pricing."""
    return bool(_ACTIVE_RFQ_RE.search(text or ""))


def _parse_active_rfq(text: str) -> dict:
    """Extract vendor names and item from an active RFQ request.
    v0.7.2: a leading "no,"/"yes," is stripped — "no, send it to michelle at
    brazill" is a send command, not a rejection.
    Two shapes:
      'get price from Brazill and Thea for 10,000ft of 12/2 MC'
        → vendors=[Brazill, Thea], item='10,000ft of 12/2 MC'
      'setup drafts for mark and thea' / 'send it to mark and thea'
        → vendors=[mark, thea], item='' (reuse last-quoted item from SESSION)
    """
    text = re.sub(r"^\s*(?:no|nope|yes|yeah)[,.\s]+", "", text or "",
                  flags=re.IGNORECASE)
    vendor_part, item_text = "", ""

    # Shape 1: "... from VENDORS for ITEM"
    m_from = re.search(r"\bfrom\s+(.+?)\s+(?=\bfor\b|\bon\b|$)", text,
                       re.IGNORECASE)
    if m_from:
        vendor_part = m_from.group(1).strip()
        m_item = re.search(r"\bfor\s+(.+)$", text, re.IGNORECASE)
        item_text = m_item.group(1).strip() if m_item else ""
    else:
        # Shape 2 (follow-up): "setup drafts for VENDORS" / "send it to VENDORS"
        m_fu = _FOLLOWUP_RFQ_RE.search(text)
        if m_fu:
            tail = (m_fu.group(1) or m_fu.group(2) or "").strip()
            # the tail may itself contain the item after a second "for":
            # "create a draft for brazill for 250 QO130"
            parts = re.split(r"\s+for\s+", tail, maxsplit=1,
                             flags=re.IGNORECASE)
            if len(parts) == 2 and parse_rfq_line(parts[1]).get("part"):
                vendor_part, item_text = parts[0].strip(), parts[1].strip()
            else:
                vendor_part = tail
                # reuse the item from the last price answer this session
                with _state_lock:
                    last = SESSION.get("last_priced_item")
                if last:
                    item_text = last

    # strip trailing pleasantries ("thanks!", "please", "for me")
    vendor_part = re.sub(r"\b(thanks?|thank you|please|for me|now)\b.*$", "",
                         vendor_part, flags=re.IGNORECASE).strip(" ,.!?")

    vendor_names = [v.strip() for v in
                    re.split(r"\band\b|[,;]", vendor_part, flags=re.IGNORECASE)
                    if v.strip() and len(v.strip()) > 1]
    return {"vendor_names": vendor_names, "item_text": item_text}


def _fuzzy_vendor_email(name: str, known: list[str]) -> str:
    """Match a spoken vendor name against known email addresses by domain/local."""
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    for email in known:
        parts = email.lower().replace("-", "").replace("_", "")
        dom = parts.split("@")[-1].split(".")[0] if "@" in parts else ""
        loc = parts.split("@")[0] if "@" in parts else ""
        for seg in (dom, loc):
            seg_n = re.sub(r"[^a-z0-9]", "", seg)
            if seg_n and (seg_n in norm or norm in seg_n or
                          (len(norm) >= 4 and
                           (norm[:4] in seg_n or seg_n[:4] in norm))):
                return email
    return ""


def _handle_active_rfq(text: str) -> dict | None:
    """Parse an active RFQ request and create per-vendor draft RFQs.
    Returns None when the parse is too thin to act on (fall through to Brain).
    Returns a clarification dict when vendor names are ambiguous."""
    parsed = _parse_active_rfq(text)
    vendor_names = parsed["vendor_names"]
    item_text = parsed["item_text"]
    if not vendor_names or not item_text:
        return None
    line = parse_rfq_line(item_text)
    if not line.get("part"):
        return None

    res = _resolve_names(vendor_names)
    qty_str = str(line.get("qty", ""))
    unit_str = (" " + line["unit"]) if line.get("unit") else ""
    part_str = line.get("part", item_text)
    item_desc = f"{qty_str}{unit_str} {part_str}".strip()

    # If any names are ambiguous, ask before creating anything
    if res["ambiguous"]:
        amb = res["ambiguous"][0]
        question = _clarification_prompt(amb["name"], amb["candidates"])
        with _state_lock:
            SESSION["pending_rfq"] = {
                "item_line": line,
                "resolved": res["resolved"],
                "ambiguous": res["ambiguous"],
                "unmatched": res["unmatched"],
                "current_amb_idx": 0,
            }
        return {"reply": question, "speak": question,
                "results": [], "action": "rfq_clarify",
                "candidates": amb["candidates"]}

    rfq_results, refs = [], []
    for v in res["resolved"]:
        r = create_rfq({"vendors": [v["email"]], "lines": [line], "job": "",
                        "note": ""}, draft=True)
        rfq_results.append({**v, "ref": r.get("ref"), "status": r.get("status"),
                             "ok": r.get("ok")})
        if r.get("ref"):
            refs.append(r["ref"])

    reply_parts, speak_parts = [], []
    sent = [r for r in rfq_results if r.get("status") == "sent"]
    queued = [r for r in rfq_results if r.get("status") not in ("sent", None)]
    for grp, verb, note in ((sent, "sent", ""),
                            (queued, "drafted",
                             " (not sent yet \u2014 will send from the Outlook "
                             "PC, or send it from the RFQ tab)")):
        if grp:
            labels = " and ".join(
                f"{r.get('contact') or r['email'].split('@')[0]} at "
                f"{r.get('company') or r['email'].split('@')[-1].split('.')[0]}"
                for r in grp)
            grp_refs = ", ".join(r["ref"] for r in grp if r.get("ref"))
            reply_parts.append(f"RFQ {verb} for {labels} \u2014 {item_desc}."
                               + (f" ({grp_refs})" if grp_refs else "")
                               + note)
            speak_parts.append(f"R F Q {verb} for {labels}, {item_desc}.")
    if res["unmatched"]:
        missing = " and ".join(res["unmatched"])
        reply_parts.append(f"I couldn\'t find contact details for {missing}. "
                           f"Go to the RFQ tab to add their email and send.")
        speak_parts.append(f"I need an email address for {missing}.")

    if not reply_parts:
        return None
    if refs:
        with _state_lock:
            SESSION["last_refs"] = refs      # v0.8.0: "preview it", "send it"
    return {"reply": "\n".join(reply_parts), "speak": " ".join(speak_parts),
            "results": [], "action": "rfq_draft", "rfq_refs": refs}
def brain_health() -> dict:
    """Quick reachability + stats probe of the Brain, for the ping endpoint."""
    import urllib.request
    try:
        with urllib.request.urlopen(BRAIN_URL + "/health", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return {"ok": True, "records": d.get("records"),
                "vendors": d.get("vendors"),
                "reply_records": d.get("reply_records"),
                "llm": d.get("llm_answering")}
    except Exception:  # noqa: BLE001
        return {"ok": False}


# ---- v0.8.1: small persisted settings + spoken contact aliases -------------
_SETTINGS_PATH = os.path.join(_BASE, "settings.json")
_ALIASES_PATH = os.path.join(_BASE, "contact_aliases.json")


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_setting(key: str, value) -> None:
    s = _load_settings()
    s[key] = value
    try:
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError as e:
        log.warning("settings save failed: %s", e)


def _answer_mode() -> str:
    """'human' (default, conversational) or 'short' (straight to the point)."""
    return _load_settings().get("answer_mode", "human")


def _load_contact_aliases() -> dict:
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            return {k.lower(): v for k, v in json.load(f).items()}
    except Exception:  # noqa: BLE001
        return {}


def _save_contact_alias(phrase: str, email: str) -> None:
    """'pipe and wire' -> pipeandwirequotes@theaenterprises.com. Learned from
    explicit teaching or from the user answering a which-one question."""
    a = _load_contact_aliases()
    a[(phrase or "").strip().lower()] = (email or "").strip().lower()
    try:
        with open(_ALIASES_PATH, "w", encoding="utf-8") as f:
            json.dump(a, f, indent=1)
    except OSError as e:
        log.warning("alias save failed: %s", e)


def _brain_answer(text: str) -> dict:
    """Forward a general question to the Brain and shape its reply for the
    phone. Keeps a single conversational session so follow-ups have context."""
    out = {"reply": "", "speak": "", "results": [], "action": "brain"}
    # if this is a price/availability question, remember the item so a
    # follow-up like "setup drafts for mark and thea" can reuse it
    m_item = re.search(r"\b(?:price|pricing|availability|avail|quote|cost|bid)"
                       r"\b.*?\bfor\s+(.+)$", text, re.IGNORECASE)
    if m_item:
        item = m_item.group(1).strip().rstrip(".?!").strip()
        # don't capture a vendor name as the item ("...from X")
        item = re.split(r"\bfrom\b", item, flags=re.IGNORECASE)[0].strip()
        if item and parse_rfq_line(item).get("part"):
            with _state_lock:
                SESSION["last_priced_item"] = item
    with _state_lock:
        sid = SESSION.get("brain_session")
    try:
        d = ask_brain(text, sid)
    except Exception as e:  # noqa: BLE001
        log.warning("brain /ask unreachable: %s", e)
        out["action"] = "brain_offline"
        out["reply"] = ("I couldn't reach the MaINbox Brain — pricing, vendor "
                        "history, and general questions run there. Start it on "
                        "this PC with:  py -m mainbox_brain.server")
        out["speak"] = ("I couldn't reach the main MaINbox Brain. Make sure "
                        "it's running on the PC.")
        return out
    with _state_lock:
        if d.get("session"):
            SESSION["brain_session"] = d["session"]
        # v0.7.2: remember when the Brain asked a question, so a bare
        # "yes"/"no" next turn goes back to it instead of being re-routed
        SESSION["brain_pending"] = bool(d.get("pending"))
    msg = (d.get("message") or "").strip() or \
        "The Brain didn't have an answer for that one."
    msg = _clean_brain_reply(msg)   # strip meta-preambles + flag stale dates

    # v0.8.1: when the Brain suggests vendors ("I have X at Y, ... as your
    # vendors"), remember the pairs — a follow-up "draft for mark" should
    # prefer the Mark it just suggested over cold address-book Marks.
    mm = re.search(r"\bI have\s+(.+?)\s+as your vendors", msg,
                   re.IGNORECASE | re.DOTALL)
    if mm:
        sugg = []
        for piece in re.split(r",\s*|\s+and\s+", mm.group(1)):
            m2 = re.match(r"^['\"]?(.+?)['\"]?\s+at\s+(.+)$", piece.strip())
            if m2:
                sugg.append({"name": m2.group(1).strip(),
                             "company": m2.group(2).strip()})
        if sugg:
            with _state_lock:
                SESSION["suggested_vendors"] = sugg

    with _state_lock:
        SESSION["last_full_reply"] = msg    # for "full answer" in short mode

    # v0.8.1: short-answer mode — first paragraph only, no essays
    if _answer_mode() == "short":
        paras = [p for p in msg.split("\n\n") if p.strip()]
        if len(paras) > 1:
            msg = paras[0] + "\n\n(short mode — say 'full answer' for the rest)"
        out["reply"] = msg
        first = paras[0]
        cut = first[:220].rsplit(". ", 1)[0] if len(first) > 220 else first
        out["speak"] = cut if cut.endswith(".") else cut + "."
        return out

    out["reply"] = msg
    # keep spoken answers from running on forever; full text stays on screen
    if len(msg) <= 400:
        out["speak"] = msg
    else:
        head = msg[:380].rsplit(". ", 1)[0]
        out["speak"] = (head or msg[:380]) + "… the full answer's on screen."
    return out


# "get price/availability/quote from X [and Y] for Z" — triggers RFQ prefill
_PRICE_FROM_RE = re.compile(
    r"\b(?:get|find|check|pull|need|want|request|send)\b.{0,60}\b"
    r"(?:price|pricing|quote|quotes|availability|avail|cost|bid)\b"
    r".{0,40}\bfrom\b",
    re.IGNORECASE)


def _parse_price_from_intent(text: str):
    """Detect 'get price from X and Y for Z'. Returns dict or None."""
    if not _PRICE_FROM_RE.search(text or ""):
        return None
    m = re.search(r"\bfrom\s+(.+?)\s+for\s+(.+)", text, re.IGNORECASE)
    if not m:
        return None
    vendor_text = m.group(1).strip()
    part_text = m.group(2).strip().rstrip(".?!").strip()
    vendor_names = [v.strip().strip(",").strip()
                    for v in re.split(r"\s+and\s+|\s*,\s*|\s+or\s+",
                                      vendor_text, flags=re.IGNORECASE)
                    if v.strip() and len(v.strip()) >= 2]
    if not vendor_names or not part_text:
        return None
    return {"vendor_names": vendor_names,
            "line": parse_rfq_line(part_text),
            "part_text": part_text}


# v0.7.2: common nickname map so "nick at hubbell" finds Nicholas Dattilo.
_NICKNAMES = {
    "nick": ["nicholas"], "mike": ["michael"], "bill": ["william"],
    "will": ["william"], "bob": ["robert"], "rob": ["robert"],
    "jim": ["james"], "dave": ["david"], "steve": ["stephen", "steven"],
    "rich": ["richard"], "rick": ["richard"], "tom": ["thomas"],
    "tony": ["anthony"], "chris": ["christopher", "christine"],
    "dan": ["daniel"], "danny": ["daniel"], "matt": ["matthew"],
    "joe": ["joseph"], "joey": ["joseph"], "greg": ["gregory"],
    "jeff": ["jeffrey"], "ed": ["edward"], "eddie": ["edward"],
    "andy": ["andrew"], "drew": ["andrew"], "ben": ["benjamin"],
    "sam": ["samuel", "samantha"], "alex": ["alexander", "alexandra"],
    "kate": ["katherine", "kathleen"], "katie": ["katherine"],
    "kathy": ["katherine", "kathleen"], "liz": ["elizabeth"],
    "beth": ["elizabeth"], "jen": ["jennifer"], "jenny": ["jennifer"],
    "pat": ["patrick", "patricia"], "tim": ["timothy"], "ken": ["kenneth"],
    "ron": ["ronald"], "don": ["donald"], "larry": ["lawrence"],
    "fred": ["frederick"], "ted": ["theodore", "edward"],
    "charlie": ["charles"], "chuck": ["charles"], "jack": ["john"],
    "vince": ["vincent"], "ray": ["raymond"], "phil": ["philip"],
}
_FORMAL_TO_NICK: dict = {}
for _n, _fs in _NICKNAMES.items():
    for _f in _fs:
        _FORMAL_TO_NICK.setdefault(_f, []).append(_n)


def _name_variants(name: str) -> list[str]:
    """Spoken-name variants via the nickname map, both directions.
    'nick' -> ['nick', 'nicholas']; 'nicholas' -> ['nicholas', 'nick'].
    Multi-word names expand the FIRST word: 'nick dattilo' also tries
    'nicholas dattilo'."""
    n = (name or "").strip().lower()
    if not n:
        return []
    parts = n.split()
    first, rest = parts[0], parts[1:]
    firsts = ([first] + _NICKNAMES.get(first, [])
              + _FORMAL_TO_NICK.get(first, []))
    seen, out = set(), []
    for f in firsts:
        v = " ".join([f] + rest)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


_COMPANY_STOPWORDS = {"brothers", "assoc", "associates", "enterprises",
                      "corp", "corporation", "inc", "llc", "co", "company",
                      "electric", "electrical", "supply", "cable", "wire",
                      "industries", "group", "sales", "the", "and", "of"}


def _contact_index() -> dict:
    """Build a multi-match name index from the Brain's /vendors endpoint.
    Returns {spoken_fragment: [(email, company_name, contact_name), ...]}
    Covers: first names extracted from email locals (markh→mark), company
    keywords (brazill→Brazill Brothers), and contact display names.
    Falls back to recent-RFQ emails if the Brain is unreachable."""
    import urllib.request as _ur
    # multi-match: fragment -> list of (email, company, contact_display)
    idx: dict[str, list] = {}

    def _add(frag: str, email: str, company: str, contact: str) -> None:
        frag = frag.lower().strip()
        if len(frag) < 2 or not email:
            return
        entry = (email.lower(), company, contact)
        bucket = idx.setdefault(frag, [])
        if entry not in bucket:
            bucket.append(entry)

    def _first_name(local: str) -> str:
        """'markh' → 'mark', 'pipeandwirequotes' → '' (not a person name)."""
        n = re.sub(r"[^a-z]", "", local.lower())
        # heuristic: short prefix is likely a name, long strings are mailboxes
        if 2 <= len(n) <= 10:
            return n
        return ""

    # --- Layer 1: Brain /vendors (richest source) -------------------------
    try:
        with _ur.urlopen(BRAIN_URL + "/vendors", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        for v in (data.get("vendors") or []):
            company = (v.get("name") or "").strip()
            # index each named contact individually
            for c in (v.get("contacts") or []):
                email = (c.get("email") or "").strip().lower()
                cname = (c.get("name") or "").strip()
                if not email:
                    continue
                local = email.split("@")[0]
                domain = email.split("@")[-1].split(".")[0]
                display = cname or local
                # first name from email local (markh → mark)
                fn = _first_name(local)
                if fn:
                    _add(fn, email, company, display)
                # full local part
                _add(local, email, company, display)
                # domain keyword → company match
                _add(domain, email, company, display)
                # contact display name words
                for w in cname.lower().split():
                    if len(w) >= 2:
                        _add(w, email, company, display)
            # company name keywords (no email on the vendor record itself,
            # but useful for matching "brazill" → any contact there)
            for w in company.lower().split():
                if w not in _COMPANY_STOPWORDS and len(w) >= 3:
                    # add all contacts under this company for this keyword
                    for c in (v.get("contacts") or []):
                        email = (c.get("email") or "").strip().lower()
                        cname = (c.get("name") or "").strip()
                        if email:
                            _add(w, email, company, cname or email.split("@")[0])
    except Exception:  # noqa: BLE001
        pass

    # --- Layer 2: local RFQ queue history (fast fallback) -----------------
    for em in recent_vendors(50):
        local = em.split("@")[0].lower()
        domain = em.split("@")[-1].split(".")[0].lower()
        fn = _first_name(local)
        if fn:
            _add(fn, em, domain.title(), local)
        _add(local, em, domain.title(), local)
        _add(domain, em, domain.title(), local)

    # --- Layer 3: exported Outlook contacts (contacts.json) ----------------
    # v0.8.1: loads LAST so procurement contacts (Brain registry + RFQ
    # history) rank ahead of cold address-book entries with the same name.
    # Produced by export_contacts.py on the Outlook PC.  Gives the resolver
    # real display names ("Dattilo, Nicholas") so "nick at hubbell" lands on
    # ndattilo@hubbell.com even though that contact never appeared in RFQ
    # traffic.  Override the path with MBB_CONTACTS.
    _cpath = os.environ.get("MBB_CONTACTS",
                            os.path.join(_BASE, "contacts.json"))
    try:
        with open(_cpath, "r", encoding="utf-8") as _cf:
            for _c in json.load(_cf):
                email = (_c.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                first = (_c.get("first") or "").strip()
                last = (_c.get("last") or "").strip()
                disp = ((_c.get("name") or f"{first} {last}").strip()
                        or email.split("@")[0])
                company = (_c.get("company")
                           or email.split("@")[-1].split(".")[0].title())
                local = email.split("@")[0]
                domain = email.split("@")[-1].split(".")[0]
                if first:
                    _add(first, email, company, disp)
                if last:
                    _add(last, email, company, disp)
                if first and last:
                    _add(first + last, email, company, disp)
                _add(local, email, company, disp)
                _add(domain, email, company, disp)
                for w in company.lower().split():
                    if w not in _COMPANY_STOPWORDS and len(w) >= 3:
                        _add(w, email, company, disp)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("contacts.json load failed: %s", e)


    return idx


def _resolve_names(names: list[str]) -> dict:
    """Resolve spoken vendor/contact names to email addresses.
    v0.7.2 upgrades:
      - "NAME at COMPANY" ("nick at hubbell", "michelle at brazill") — the
        company part filters candidates by company name / email domain
      - nickname map — "nick" also tries "nicholas", and vice versa
    Matching stays deliberately tight (>=3 shared leading chars for a prefix
    match); 2+ distinct contacts always ask the user instead of guessing."""
    idx = _contact_index()
    aliases = _load_contact_aliases()
    with _state_lock:
        suggested = list(SESSION.get("suggested_vendors") or [])

    def _sugg_match(entry) -> bool:
        """entry (email, company, contact) matches a Brain-suggested vendor?"""
        em, co, cn = entry
        local = re.sub(r"[^a-z0-9]", "", em.split("@")[0])
        dom = re.sub(r"[^a-z0-9]", "", em.split("@")[-1].split(".")[0])
        con = re.sub(r"[^a-z0-9]", "", (cn or "").lower())
        comp = re.sub(r"[^a-z0-9]", "", (co or "").lower())
        for sg in suggested:
            sn = re.sub(r"[^a-z0-9]", "", sg["name"].lower())
            sc = re.sub(r"[^a-z0-9]", "",
                        sg["company"].lower().split()[0]
                        if sg["company"].split() else "")
            n_ok = len(sn) >= 3 and (sn in local or local in sn
                                     or sn in con or con and con in sn)
            c_ok = len(sc) >= 4 and (sc in comp or sc in dom
                                     or comp.startswith(sc)
                                     or dom.startswith(sc))
            if n_ok and c_ok:
                return True
        return False

    resolved, ambiguous, unmatched = [], [], []
    for raw_name in names:
        name, company_hint = raw_name, ""
        # v0.8.1: learned spoken aliases win outright ("pipe and wire",
        # or a name the user disambiguated once before)
        akey = (raw_name or "").strip().lower()
        aem = aliases.get(akey) or aliases.get(akey.replace(" ", ""))
        if aem:
            hit = None
            for entries in idx.values():
                for e in entries:
                    if e[0] == aem:
                        hit = e
                        break
                if hit:
                    break
            resolved.append({"name": raw_name, "email": aem,
                             "company": hit[1] if hit else
                             aem.split("@")[-1].split(".")[0].title(),
                             "contact": hit[2] if hit else
                             aem.split("@")[0]})
            continue
        m_at = re.match(r"^(.*?)\s+at\s+(.+)$", (raw_name or "").strip(),
                        re.IGNORECASE)
        if m_at:
            name, company_hint = m_at.group(1).strip(), m_at.group(2).strip()

        pool = []
        for variant in (_name_variants(name) or [name]):
            norm = re.sub(r"[^a-z0-9]", "", variant.lower())
            if not norm:
                continue
            exact = idx.get(norm, [])
            prefix_hits = []
            if len(norm) >= 3:
                for k, entries in idx.items():
                    if k == norm:
                        continue
                    shared = 0
                    for a, b in zip(norm, k):
                        if a == b:
                            shared += 1
                        else:
                            break
                    is_prefix = k.startswith(norm) or norm.startswith(k)
                    if is_prefix and shared >= 3:
                        prefix_hits.extend(entries)
            # merge exact + prefix: an exact hit ("Mark" the contact name at
            # one vendor) must NOT silently win when another contact is also
            # a strong prefix match ("markh" elsewhere) — ambiguity asks
            for e in list(exact) + [p for p in prefix_hits if p not in exact]:
                if e not in pool:
                    pool.append(e)

        # "at COMPANY" filter: keep only contacts whose company or email
        # domain matches the hint ("at brazill" → brazill.com contacts)
        if company_hint and pool:
            ch = re.sub(r"[^a-z0-9]", "", company_hint.lower())

            def _co_ok(e):
                co = re.sub(r"[^a-z0-9]", "", (e[1] or "").lower())
                dom = re.sub(r"[^a-z0-9]", "",
                             e[0].split("@")[-1].split(".")[0])
                return bool(ch) and (
                    ch in co or co.startswith(ch)
                    or ch in dom or dom.startswith(ch)
                    or (len(ch) >= 4 and (co[:4] == ch[:4]
                                          or dom[:4] == ch[:4])))
            filtered = [e for e in pool if _co_ok(e)]
            if filtered:
                pool = filtered

        # dedupe by email
        seen, deduped = set(), []
        for e in pool:
            if e[0] not in seen:
                seen.add(e[0])
                deduped.append(e)

        # v0.8.1: the Brain just suggested vendors — if exactly one
        # candidate IS a suggested vendor, that's who the user means
        if len(deduped) > 1 and suggested:
            sm = [e for e in deduped if _sugg_match(e)]
            if len(sm) == 1:
                deduped = sm
            elif sm:                       # several suggested — front of list
                deduped = sm + [e for e in deduped if e not in sm]

        if not deduped:
            unmatched.append(raw_name)
        elif len(deduped) == 1:
            resolved.append({"name": name, "email": deduped[0][0],
                             "company": deduped[0][1],
                             "contact": deduped[0][2]})
        else:
            # multiple possible contacts — never guess, ask
            ambiguous.append({"name": name, "candidates": [
                {"email": e, "company": co, "contact": cn}
                for e, co, cn in deduped[:8]]})
    return {"resolved": resolved, "ambiguous": ambiguous,
            "unmatched": unmatched}


def _clarification_prompt(ambig_name: str, candidates: list) -> str:
    """Build a spoken clarification question for an ambiguous vendor name."""
    options = []
    for i, c in enumerate(candidates, 1):
        label = c.get("contact") or c["email"].split("@")[0]
        company = c.get("company") or c["email"].split("@")[-1].split(".")[0]
        options.append(f"{i}. {label} at {company}")
    return (f"I found {len(candidates)} contacts named {ambig_name}: "
            + "; ".join(options)
            + ". Say a number, or 'all of them'.")


def _teach_pair(part_a: str, part_b: str) -> dict:
    """Save an equivalence pair (both directions) and link it in the Brain
    for pricing. Used by the direct teach command and the yes-confirmation
    after a truncated teach. v0.8.4: extracted so both paths share it."""
    if xr is None:
        return {"reply": "The cross-reference store isn't available, so I "
                         "can't save that.", "speak": "I can't save that "
                         "right now.", "results": [], "action": "teach"}

    def _mfr_part(phrase: str):
        # split each side with the SAME parser lookups use — otherwise a
        # teach stores "topaz 699" whole while lookups search mfr=topaz
        # part=699 and never find it
        if xc is not None:
            try:
                itx = xc.parse_query("equal to " + phrase)
                if getattr(itx, "part", ""):
                    return (getattr(itx, "mfr", "") or "", itx.part)
            except Exception:  # noqa: BLE001
                pass
        return ("", phrase)

    a_m, a_p = _mfr_part(part_a)
    b_m, b_p = _mfr_part(part_b)
    try:
        xr.add(part=a_p, mfr=a_m, equiv_part=b_p, equiv_mfr=b_m,
               equiv_type="spec_equivalent",
               notes="taught via voice", db=DB_PATH)
        xr.add(part=b_p, mfr=b_m, equiv_part=a_p, equiv_mfr=a_m,
               equiv_type="spec_equivalent",
               notes="taught via voice", db=DB_PATH)
        linked = False
        try:
            import urllib.request as _ur
            body = json.dumps({"term": part_a,
                               "canonical": part_b}).encode()
            req = _ur.Request(BRAIN_URL + "/teach", data=body,
                              headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=6) as r:
                linked = bool(json.loads(r.read().decode()).get("ok"))
        except Exception as e:  # noqa: BLE001
            log.info("brain /teach not reached: %s", e)
        msg = f"Got it — {part_a} and {part_b} are equivalent"
        if linked:
            msg += ", and pricing lookups will now match both."
        else:
            msg += (". I couldn't reach the Brain to link pricing — "
                    "equivalents are saved; teach it again with the "
                    "Brain running to link pricing too.")
        return {"reply": msg, "speak": msg, "results": [], "action": "teach"}
    except Exception as e:  # noqa: BLE001
        log.warning("teach failed: %s", e)
        return {"reply": f"I couldn't save that: {e}",
                "speak": "I couldn't save that one.",
                "results": [], "action": "teach"}


def handle_query(text: str) -> dict:
    """The one brain both typed and spoken input flow through. Returns:
    {reply, speak, results:[{n,src_mfr,equiv_mfr,equiv_part,meta,decided}],
     action}"""
    out = {"reply": "", "speak": "", "results": [], "action": "lookup"}

    # v0.8.4: a truncated teach asked "Did you mean X is an equal to Y?" —
    # resolve that FIRST so a bare yes/no doesn't wander to the Brain.
    with _state_lock:
        _pt = SESSION.get("pending_teach")
    if _pt:
        _ty = (text or "").strip().lower().rstrip(".!?")
        if re.fullmatch(r"(?:yes|yeah|yep|correct|right|sure|do it|save it)",
                        _ty):
            with _state_lock:
                SESSION.pop("pending_teach", None)
            return _teach_pair(_pt["a"], _pt["b"])
        if re.fullmatch(r"(?:no|nope|cancel|never ?mind|don'?t)", _ty):
            with _state_lock:
                SESSION.pop("pending_teach", None)
            msg = "Okay — not saved."
            return {"reply": msg, "speak": msg, "results": [],
                    "action": "teach"}
        with _state_lock:                 # anything else: move on normally
            SESSION.pop("pending_teach", None)

    # ---- v0.8.1: answer-style + full-answer + contact-alias commands --------
    _t0 = (text or "").strip().lower().rstrip(".!")
    if re.fullmatch(r"(?:load |read |show |get )?(?:the )?last call"
                    r"(?: transcript)?", _t0):
        lc = latest_call_transcript()
        if lc.get("ok"):
            t = lc["text"]
            shown = t if len(t) <= 900 else t[:900] + "\n…"
            return {"reply": f"📞 {lc.get('filename','last call')}:\n{shown}",
                    "speak": "Here's the last call transcript, on screen.",
                    "results": [], "action": "call_transcript",
                    "transcript": t}
        return {"reply": "No call transcripts yet — the ingest watcher sends "
                "them here automatically once it's running.",
                "speak": "No call transcripts yet.",
                "results": [], "action": "call_transcript"}
    if re.fullmatch(r"(?:short (?:answer )?mode|be brief|keep it short"
                    r"|short answers?)", _t0):
        _save_setting("answer_mode", "short")
        msg = "Short mode on — straight answers. Say 'human mode' to switch back."
        return {"reply": msg, "speak": msg, "results": [], "action": "settings"}
    if re.fullmatch(r"(?:human mode|conversation(?:al)? mode|normal mode"
                    r"|talk to me)", _t0):
        _save_setting("answer_mode", "human")
        msg = "Human mode on — conversational answers."
        return {"reply": msg, "speak": msg, "results": [], "action": "settings"}
    if re.fullmatch(r"(?:full answer|read the rest|the rest|full version)", _t0):
        with _state_lock:
            full = SESSION.get("last_full_reply") or ""
        if full:
            return {"reply": full, "speak": "It's on screen.",
                    "results": [], "action": "brain"}
        return {"reply": "No longer answer stored yet.", "speak":
                "Nothing to expand yet.", "results": [], "action": "noop"}

    # "when I say pipe and wire use pipeandwirequotes@thea..." /
    # "remember thea is pipeandwirequotes@theaenterprises.com"
    _al = re.search(r"\b(?:when(?:ever)? i say|call)\s+(.{2,40}?)\s+"
                    r"(?:use|it means|means|is|=)\s+([\w.+-]+@[\w.-]+)\b",
                    text or "", re.IGNORECASE) or \
        re.search(r"\bremember\s+(?:that\s+)?(.{2,40}?)\s+(?:is|means|=)\s+"
                  r"([\w.+-]+@[\w.-]+)\b", text or "", re.IGNORECASE)
    if _al:
        phrase, email = _al.group(1).strip(" '\"."), _al.group(2).lower()
        _save_contact_alias(phrase, email)
        msg = f"Got it — '{phrase}' now means {email}."
        return {"reply": msg, "speak": msg, "results": [], "action": "teach"}

    # ---- teach an equivalence: "remember X is (an) equal to Y" -------------
    teach = _parse_teach_equivalence(text or "")
    if teach:
        return _teach_pair(teach["part_a"], teach["part_b"])

    # v0.8.4: a teach the STT cut off mid-sentence ("remember ilsco ik250 is
    # an…") must NEVER fall through to the Brain, which "notes" the fragment
    # and claims success. If equals were just shown, ask to confirm the
    # obvious pairing; otherwise ask what to pair it with.
    if _TEACH_RE.search(text or "") and "@" not in (text or "") and \
            not re.search(r"\bremember to\b", text or "", re.IGNORECASE):
        _subj = re.sub(r"^\s*(?:please\s+)?(?:remember|note|save|learn"
                       r"|teach\s+(?:you|it|that)?)\s+(?:that\s+)?", "",
                       (text or "").strip(), flags=re.IGNORECASE)
        _subj = re.sub(r"\b(?:is|it'?s)\s*(?:also\s*)?(?:an?|the same"
                       r"|equal(?:\s+to)?)?\s*[.!]?\s*$", "", _subj,
                       flags=re.IGNORECASE).strip(" .,!?")
        if 2 <= len(_subj) <= 60:
            with _state_lock:
                _lastp = SESSION.get("last_xref_part") or ""
            if _lastp and _subj.lower() != _lastp.lower():
                with _state_lock:
                    SESSION["pending_teach"] = {"a": _subj, "b": _lastp}
                q = (f"Did you mean: {_subj} is an equal to {_lastp}? "
                     "Say yes or no.")
                return {"reply": q, "speak": q, "results": [],
                        "action": "teach_confirm"}
            q = (f"Remember {_subj} as what? Say '{_subj} is an equal to "
                 "(part)', or give an email address to save a contact.")
            return {"reply": q, "speak": q, "results": [],
                    "action": "teach_confirm"}

    # ---- cancel a pending clarification ------------------------------------
    if re.search(r"\b(cancel|never\s*mind|nevermind|forget it|stop)\b",
                 text or "", re.IGNORECASE):
        with _state_lock:
            had = SESSION.pop("pending_rfq", None)
        if had:
            return {"reply": "Okay, cancelled — nothing was created.",
                    "speak": "Cancelled.", "results": [], "action": "cancel"}

    # ---- RFQ lifecycle by voice: send/preview/edit/hide/delete/restore -----
    _ref_m = re.search(r"\bvr[-\s]?(\d{8})[-\s]?(\d{3})\b", text or "",
                       re.IGNORECASE)
    _low0 = (text or "").lower()
    if not _ref_m and re.search(
            r"\b(preview|show|read|edit|open|send|release|delete|hide|"
            r"archive|restore)\b", _low0) and \
            re.search(r"\b(it|that|this|them|the last (?:one|draft)s?"
                      r"|last draft)\b", _low0) and \
            not re.search(r"\bto\s+\S", _low0):
        # v0.8.0: pronoun ref — "preview it", "send it", "edit that" resolve
        # to the most recent voice-created draft(s). "send it TO x" is a new
        # draft, so 'to <name>' is excluded above.
        with _state_lock:
            _lr = list(SESSION.get("last_refs") or [])
        if _lr:
            class _FakeM:                      # reuse the ref-verb handling
                def group(self, n):
                    p = _lr[-1].split("-")
                    return p[1] if n == 1 else p[2]
            _ref_m = _FakeM()
        else:
            return {"reply": "I don't have a recent draft to apply that to — "
                    "say the ref, like 'preview VR-20260710-001'.",
                    "speak": "Which RFQ? Say the ref.",
                    "results": [], "action": "noop"}
    if _ref_m:
        ref = f"VR-{_ref_m.group(1)}-{_ref_m.group(2)}"
        low = (text or "").lower()
        if re.search(r"\b(preview|show|read)\b", low):
            p = preview_saved_rfq(ref)
            if not p.get("ok"):
                msg = p.get("error", f"couldn't preview {ref}")
                return {"reply": msg, "speak": msg, "results": [],
                        "action": "rfq_preview"}
            to = ", ".join(p["to"])
            body = p["body"]
            shown = body if len(body) <= 700 else body[:700] + "\n…"
            reply = (f"{ref} ({p['status']})\nTo: {to}\n"
                     f"Subject: {p['subject']}\n\n{shown}")
            speak = (f"{ref}: to {to}. Subject, {p['subject']}. "
                     "The full email is on screen. Say 'send it' to send, "
                     "or 'edit it' to change it.")
            return {"reply": reply, "speak": speak, "results": [],
                    "action": "rfq_preview", "ref": ref}
        if re.search(r"\b(edit|open|change|modify)\b", low):
            p = preview_saved_rfq(ref)
            if not p.get("ok"):
                msg = p.get("error", f"couldn't open {ref}")
                return {"reply": msg, "speak": msg, "results": [],
                        "action": "rfq_edit"}
            if p.get("status") != "draft":
                msg = (f"{ref} is {p.get('status')} — only drafts can be "
                       "edited.")
                return {"reply": msg, "speak": msg, "results": [],
                        "action": "rfq_edit"}
            msg = f"Opening {ref} in the RFQ editor — check the RFQ tab."
            return {"reply": msg, "speak": msg, "results": [],
                    "action": "rfq_edit", "ref": ref, "prefill": p}
        if re.search(r"\b(hide|archive)\b", low):
            r = hide_rfq(ref)
            msg = (f"{ref} hidden — it's under 'Hidden & deleted' on the RFQ "
                   f"tab if you need it."
                   if r.get("ok") else r.get("error", "hide failed"))
            return {"reply": msg, "speak": f"{ref} hidden.",
                    "results": [], "action": "rfq_hide"}
        if re.search(r"\b(send|release|fire|go ahead|approve)\b", low):
            r = release_rfq(ref)
            if r.get("ok"):
                bits = []
                if r.get("sent"):
                    bits.append(f"{r['sent']} sent")
                if r.get("queued"):
                    bits.append(f"{r['queued']} queued for the Outlook PC")
                msg = f"{ref} released — " + " and ".join(bits) + "."
            else:
                msg = r.get("error", f"couldn't send {ref}")
            return {"reply": msg, "speak": msg, "results": [],
                    "action": "rfq_send"}
        if re.search(r"\b(delete|remove|trash|kill|scrap)\b", low):
            r = delete_rfq(ref)
            msg = (f"{ref} deleted — it's in the Deleted section of the RFQ "
                   f"tab if you need it back (say 'restore {ref}')."
                   if r.get("ok") else r.get("error", "delete failed"))
            return {"reply": msg, "speak": f"{ref} deleted. Say restore to "
                    "bring it back.", "results": [], "action": "rfq_delete"}
        if re.search(r"\b(restore|undelete|bring back|recover)\b", low):
            r = restore_rfq(ref)
            msg = (f"{ref} restored (status: {r.get('status')})."
                   if r.get("ok") else r.get("error", "restore failed"))
            return {"reply": msg, "speak": msg, "results": [],
                    "action": "rfq_restore"}

    # ---- "send all drafts" -------------------------------------------------
    if re.search(r"\bsend\s+(?:all\s+(?:the\s+)?|my\s+)?drafts\b",
                 text or "", re.IGNORECASE):
        drafts = [r for r in list_rfqs(50) if r.get("status") == "draft"]
        if not drafts:
            return {"reply": "No drafts waiting.", "speak": "No drafts "
                    "waiting.", "results": [], "action": "rfq_send"}
        n_ok = 0
        for d in drafts:
            if release_rfq(d["ref"]).get("ok"):
                n_ok += 1
        msg = f"Released {n_ok} draft{'s' if n_ok != 1 else ''}."
        return {"reply": msg, "speak": msg, "results": [],
                "action": "rfq_send"}

    # ---- reminders / calendar (before parsing — not a parts request) --------
    if re.search(r"\b(remind me|reminder|put (it |this )?on (my |the )?"
                 r"calendar|schedule)\b", text or "", re.IGNORECASE):
        cleaned = re.sub(r"^\s*(please\s+)?(remind me( to)?|set a reminder"
                         r"( to| for)?|put (it |this )?on (my |the )?calendar"
                         r"( to| for)?|schedule)\s*",
                         "", text or "", flags=re.IGNORECASE)
        evs = extract_events(cleaned or text)
        if evs:
            e = evs[0]
            out["action"] = "reminder"
            out["events"] = evs
            out["reply"] = f"Reminder ready: {e['title']} — {e['start_h']}. " \
                           f"Tap to add it to your calendar."
            out["speak"] = f"Got it. {e['title']}, {e['start_h']}. " \
                           "Tap the card to put it on your calendar."
        else:
            out["action"] = "reminder"
            out["reply"] = out["speak"] = \
                "When should I set that for? Say a day and time."
        return out

    # ---- RFQ status questions (answered from existing timelines) -----------
    rfq_ans = _rfq_voice_answer(text or "")
    if rfq_ans is not None:
        return rfq_ans

    # ---- clarification answer: user chose from an ambiguous vendor list ----
    with _state_lock:
        pending = SESSION.get("pending_rfq")
    if pending and pending.get("ambiguous"):
        # detect "the first one", "number 2", "all of them", "Brazill"
        n_match = re.search(r"\b(\d+|first|second|third|fourth|fifth|one|two"
                            r"|three|four|five)\b", (text or "").lower())
        all_match = re.search(r"\ball\b|\ball of them\b|\beveryone\b",
                              (text or "").lower(), re.IGNORECASE)
        word_to_n = {"first": 1, "one": 1, "second": 2, "two": 2,
                     "third": 3, "three": 3, "fourth": 4, "four": 4,
                     "fifth": 5, "five": 5}
        amb = pending["ambiguous"][pending.get("current_amb_idx", 0)]
        candidates = amb["candidates"]
        chosen = []
        if all_match:
            chosen = candidates
        elif n_match:
            raw = n_match.group(1).lower()
            idx = (word_to_n.get(raw, int(raw) if raw.isdigit() else 0)) - 1
            if 0 <= idx < len(candidates):
                chosen = [candidates[idx]]
        else:
            # try matching text against company/contact names
            norm = text.lower()
            for c in candidates:
                for field in (c.get("company", ""), c.get("contact", ""),
                              c.get("email", "").split("@")[0]):
                    if any(w in norm for w in field.lower().split() if len(w) >= 3):
                        if c not in chosen:
                            chosen.append(c)
        if chosen:
            # record this name's choice, then move to the next ambiguous name
            # (if any) before creating anything
            pending.setdefault("chosen", [])
            pending["chosen"].extend(chosen)
            # v0.8.1: a single explicit pick teaches the alias — next time
            # this name resolves instantly instead of asking again
            if len(chosen) == 1:
                try:
                    amb_now = pending["ambiguous"][
                        pending.get("current_amb_idx", 0)]
                    _save_contact_alias(amb_now["name"], chosen[0]["email"])
                except Exception:  # noqa: BLE001
                    pass
            cur = pending.get("current_amb_idx", 0)
            if cur + 1 < len(pending["ambiguous"]):
                pending["current_amb_idx"] = cur + 1
                with _state_lock:
                    SESSION["pending_rfq"] = pending
                nxt = pending["ambiguous"][cur + 1]
                q = _clarification_prompt(nxt["name"], nxt["candidates"])
                return {"reply": q, "speak": q, "results": [],
                        "action": "rfq_clarify", "candidates": nxt["candidates"]}

            # all ambiguous names decided — build the full vendor set
            line = pending["item_line"]
            qty_str = str(line.get("qty", ""))
            unit_str = (" " + line["unit"]) if line.get("unit") else ""
            item_desc = f"{qty_str}{unit_str} {line.get('part', '')}".strip()
            all_v = list(pending.get("resolved", [])) + pending["chosen"]
            # dedupe by email
            seen, refs, labels = set(), [], []
            for v in all_v:
                em = v.get("email", "")
                if not em or em in seen:
                    continue
                seen.add(em)
                r = create_rfq({"vendors": [em], "lines": [line],
                                "job": "", "note": ""}, draft=True)
                if r.get("ref"):
                    refs.append(r["ref"])
                contact = v.get("contact") or em.split("@")[0]
                company = v.get("company") or em.split("@")[-1].split(".")[0]
                labels.append(f"{contact} at {company}")
            with _state_lock:
                SESSION.pop("pending_rfq", None)
                if refs:
                    SESSION["last_refs"] = refs   # v0.8.0
            label_str = " and ".join(labels)
            reply = (f"RFQ drafted for {label_str} \u2014 {item_desc}."
                     + (f" ({', '.join(refs)})" if refs else "")
                     + " (not sent yet \u2014 will send from the Outlook PC, "
                       "or send from the RFQ tab)")
            speak = f"R F Q drafted for {label_str}, {item_desc}."
            return {"reply": reply, "speak": speak,
                    "results": [], "action": "rfq_draft", "rfq_refs": refs}
        elif pending.get("ambiguous"):
            # didn't understand — re-ask the current one
            amb = pending["ambiguous"][pending.get("current_amb_idx", 0)]
            q = _clarification_prompt(amb["name"], amb["candidates"])
            return {"reply": q, "speak": q, "results": [],
                    "action": "rfq_clarify",
                    "candidates": amb["candidates"]}

    # v0.7.2: a bare yes/no while the Brain has a question pending goes back
    # to the Brain session ("Would you like me to send it to them? (say yes)")
    with _state_lock:
        _bp = SESSION.get("brain_pending")
    if _bp and re.fullmatch(r"(?:yes|yeah|yep|sure|ok(?:ay)?|no|nope)[.! ]*",
                            (text or "").strip(), re.IGNORECASE):
        return _brain_answer(text)

    # v0.8.2: the Brain just asked "send it to them?" — answering with BARE
    # NAMES ("mark and thea please") is vendor selection. Draft immediately;
    # never forward names to the Brain (it would misread them as
    # contact-teaching: "Saved Mark <> as your Thea contact...").
    if _bp:
        _clean = re.sub(r"\b(please|thanks?|thank you|for me)\b", "",
                        text or "", flags=re.IGNORECASE).strip(" ,.!")
        _segs = [x.strip() for x in re.split(r"\band\b|[,;]", _clean,
                                             flags=re.IGNORECASE)
                 if x.strip()]
        if 1 <= len(_segs) <= 4 and all(
                re.fullmatch(r"[A-Za-z][A-Za-z .'&-]{1,30}", s)
                and len(s.split()) <= 4 for s in _segs):
            _r0 = _resolve_names(_segs)
            if _r0["resolved"] or _r0["ambiguous"]:
                rfq_out = _handle_active_rfq("send drafts to " + _clean)
                if rfq_out:
                    return rfq_out

    if xc is None:
        return _brain_answer(text)   # no local parser — let the Brain field it
    it = xc.parse_query(text or "")
    out["action"] = it.action

    # v0.7.2: send/draft commands outrank confirm-reject parsing — "no, send
    # it to michelle at brazill" must create a draft, not read as a reject.
    if _is_active_rfq_request(text):
        rfq_out = _handle_active_rfq(text)
        if rfq_out:          # parse succeeded — return draft/clarify result
            return rfq_out
        # parse too thin (no vendors / no item) — fall through below

    # v0.7.2: bare "rest"/"all"/number with nothing pending must not leak to
    # the Brain as a topic (it once researched vendors "regarding 'rest'").
    _bare = (text or "").strip().lower().rstrip(".!?")
    if (_bare in ("rest", "the rest", "all", "all of them", "everything")
            or re.fullmatch(r"\d{1,2}", _bare)):
        with _state_lock:
            _has_xref = bool(SESSION.get("last"))
            _has_rfq = bool(SESSION.get("pending_rfq"))
        if not _has_xref and not _has_rfq:
            out["reply"] = out["speak"] = (
                "Nothing's pending for that — look something up or start an "
                "RFQ first.")
            out["action"] = "noop"
            return out
        if _has_xref and it.action not in ("confirm", "reject"):
            hint = (f"confirm {_bare}' or 'reject {_bare}" if _bare.isdigit()
                    else "confirm the rest' or 'reject the rest")
            out["reply"] = out["speak"] = \
                f"For the list above, say '{hint}'."
            out["action"] = "noop"
            return out

    # ---- list sites -------------------------------------------------------
    if it.action == "list_sites":
        sites = xd.known_sites() if xd else []
        if sites:
            out["reply"] = "Sources I can query: " + ", ".join(sites) + \
                ". Plus the curated store, always checked."
        else:
            out["reply"] = ("No taught sites yet. Teach one on the PC with "
                            "site_teacher.py.")
        out["speak"] = out["reply"]
        return out

    # ---- corrections ------------------------------------------------------
    if it.action in ("confirm", "reject"):
        with _state_lock:
            last = SESSION.get("last") or []
        if not last:
            out["reply"] = out["speak"] = \
                "There are no results to correct yet. Look something up first."
            return out
        if xr is None:
            out["reply"] = out["speak"] = \
                "The cross_reference store isn't available on the server."
            return out
        # which numbers?
        if it.selection:
            picks = [n for n in it.selection if 1 <= n <= len(last)]
        elif it.select_all:
            bulk = xd._extract_bulk_word(it.raw) if xd else "all"
            if bulk == "everything":
                picks = [r["n"] for r in last]
            else:                                  # all/rest = undecided only
                picks = [r["n"] for r in last if not r.get("decided")]
                if not picks:
                    out["reply"] = out["speak"] = \
                        ("Everything has already been decided this round. Say "
                         "'everything' to override.")
                    return out
        else:
            out["reply"] = out["speak"] = \
                f"Which ones should I {it.action}? Say a number, or 'rest'."
            return out
        done, failed = [], []
        for n in picks:
            row = last[n - 1]
            try:
                fn = xr.confirm if it.action == "confirm" else xr.reject
                fn(row["part"], row["equiv_part"], db=DB_PATH)
                row["decided"] = it.action + "ed"
                done.append(n)
            except Exception as e:  # noqa: BLE001
                log.warning("correction failed #%d: %s", n, e)
                failed.append(n)
        with _state_lock:
            SESSION["last"] = last
        verb = "Confirmed" if it.action == "confirm" else "Rejected"
        nums = " and ".join(map(str, done)) if done else "none"
        remaining = [r["n"] for r in last if not r.get("decided")]
        rem = (f" {len(remaining)} still undecided."
               if remaining else " That's everything decided.")
        out["reply"] = out["speak"] = f"{verb} number {nums}.{rem}"
        out["results"] = [_row_public(r) for r in last]
        return out

    # ---- route: explicit cross-references stay on the local taught-sites
    #      engine (Arlington, Legrand, etc.); everything else — pricing,
    #      "what did we last pay", vendor history, general questions — is for
    #      the full MaINbox Brain ----
    if not _is_cross_ref_query(text):
        return _brain_answer(text)

    # ---- cross-reference lookup (local) -----------------------------------
    # v0.8.5: the parser sometimes absorbs leading verbs into the mfr
    # ("list equals for ilsco ik250" -> mfr='list ilsco'); keep only the
    # real brand token so labels stay clean and the manufacturer
    # family-matching isn't poisoned.
    _JUNK_MFR = {"list", "show", "find", "get", "give", "me", "what",
                 "whats", "what's", "remember", "the", "a", "an", "equals",
                 "equal", "for"}
    mfr_clean = it.mfr or ""
    if mfr_clean and " " in mfr_clean:
        _kept = [w for w in mfr_clean.split()
                 if w.lower() not in _JUNK_MFR]
        mfr_clean = " ".join(_kept[-1:])

    if not it.part:
        out["reply"] = out["speak"] = \
            "I didn't catch a part number. What part should I look up?"
        return out
    if xd is None:
        out["reply"] = out["speak"] = \
            "The dispatcher isn't available on the server."
        return out
    policy = "always" if it.research else "auto"
    try:
        fut = _XREF_POOL.submit(xd.cross_reference, it.part,
                                mfr=mfr_clean or None, live_policy=policy,
                                db=DB_PATH)
        res = fut.result(timeout=XREF_TIMEOUT)
    except concurrent.futures.TimeoutError:
        log.warning("cross_reference timed out for %r (mfr=%r)",
                    it.part, it.mfr)
        hint = ("" if it.mfr else " If it's a specific brand's part, try "
                f"including the maker, e.g. 'equal for a Topaz {it.part}'.")
        out["reply"] = out["speak"] = (
            f"The live lookup for {it.part} is taking too long — a vendor "
            f"site may be slow to respond.{hint}")
        out["action"] = "xref_timeout"
        return out
    except Exception as e:  # noqa: BLE001
        log.exception("dispatch failed")
        out["reply"] = out["speak"] = f"Lookup failed: {e}"
        return out
    findings = res.get("findings", [])
    rows = []
    for i, f in enumerate(findings, 1):
        rows.append({"n": i, "part": it.part,
                     "src_mfr": getattr(f, "src_mfr", "") or "",
                     "equiv_mfr": getattr(f, "equiv_mfr", "") or "",
                     "equiv_part": getattr(f, "equiv_part", "") or "",
                     "url": getattr(f, "source_url", "") or "",
                     "meta": _meta_for(f), "decided": None})
    with _state_lock:
        SESSION["last"] = rows
    label = f"{mfr_clean} {it.part}" if mfr_clean else it.part
    with _state_lock:
        SESSION["last_xref_part"] = label   # v0.8.2: "X is also an equal"
    if rows:
        srcs = []
        if res.get("curated_hit"):
            srcs.append("curated store")
        if res.get("sites_with_hits"):
            srcs.append("live: " + ", ".join(res["sites_with_hits"]))
        src_note = f"  (from {'; '.join(srcs)})" if srcs else ""
        out["reply"] = f"{label}: {len(rows)} equivalent(s){src_note}"
    else:
        out["reply"] = f"{label}: no equivalents found."
        if res.get("suggestion"):
            out["reply"] += "\n" + res["suggestion"]
    if findings:
        out["speak"] = _speakable_findings(label, findings)
    elif res.get("sites_need_mfr"):
        need = ", ".join(dict.fromkeys(res["sites_need_mfr"]))
        out["speak"] = (f"No equivalents for {label}. {need} needs a "
                        "manufacturer to search — ask again with the maker.")
    else:
        out["speak"] = _speakable_findings(label, findings)
    out["results"] = rows
    return out


def _meta_for(f) -> str:
    bits = []
    if getattr(f, "equiv_type", ""):
        bits.append(f.equiv_type.replace("_", " "))
    if getattr(f, "confidence", 0):
        bits.append(f"conf {f.confidence:.2f}")
    st = getattr(f, "status", "")
    if st and st != "from_vendor_tool":
        bits.append(st)
    cav = getattr(f, "caveat", "")
    if cav:                       # v0.8.3: show "via topaz 699" for chained equals
        bits.append(cav)
    return ", ".join(bits)


def _row_public(r: dict) -> dict:
    return {k: r.get(k) for k in
            ("n", "src_mfr", "equiv_mfr", "equiv_part", "meta", "decided")}


# ==============================================================================
# HTTP SERVER
# ==============================================================================
TOKEN = ""

_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
         ".css": "text/css", ".json": "application/json",
         ".png": "image/png", ".svg": "image/svg+xml",
         ".webmanifest": "application/manifest+json",
         ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"MaINboxVoice/{__version__}"

    # -- helpers ------------------------------------------------------------
    def _json(self, obj, code=200):
        try:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError):
            # The phone closed the connection before we finished replying —
            # almost always a slow Brain/LLM answer that the browser gave up
            # waiting for. The answer was computed fine; there's just nobody
            # left to send it to. Log one line instead of a crash + traceback.
            log.info("client hung up before the response was sent "
                     "(usually a slow answer); ignoring")

    def _authed(self, qs) -> bool:
        tok = self.headers.get("X-MBB-Token") or \
            (qs.get("token", [""])[0] if qs else "")
        return secrets.compare_digest(tok or "", TOKEN)

    def _read_json(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(ln) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def log_message(self, fmt, *args):  # quieter default logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_one_request(self):
        # Catch the client disconnecting mid-request anywhere (slow answers,
        # the phone backgrounding Chrome, radio drop). BaseHTTPRequestHandler
        # would otherwise surface a full traceback per drop.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError):
            self.close_connection = True
            log.info("client disconnected mid-request; ignoring")

    # -- GET ------------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path

        if path.startswith("/api/"):
            if not self._authed(qs):
                return self._json({"error": "bad token"}, 401)
            if path == "/api/ping":
                queued = 0
                if os.path.isdir(RFQ_DIR):
                    for r in list_rfqs(50):
                        if r.get("status") in ("queued", "partial"):
                            queued += 1
                return self._json({"ok": True, "version": __version__,
                                   "toolkit": {"parser": xc is not None,
                                               "dispatch": xd is not None,
                                               "store": xr is not None,
                                               "store_version":
                                                   getattr(xr, "__version__",
                                                           "?") if xr else "",
                                               "reply_match":
                                                   rmatch is not None},
                                   "brain": brain_health(),
                                   "rfq_queued": queued})
            if path == "/api/call_transcript/latest":
                return self._json(latest_call_transcript())

            if path == "/api/rfq/list":
                all_r = list_rfqs(40, include_deleted=True)
                live = [r for r in all_r if r.get("status") != "deleted"
                        and not r.get("hidden")]
                hidden = [r for r in all_r if r.get("status") != "deleted"
                          and r.get("hidden")]
                deleted = [r for r in all_r if r.get("status") == "deleted"]
                return self._json({"rfqs": live[:15], "hidden": hidden[:15],
                                   "deleted": deleted[:15],
                                   "vendors": recent_vendors()})
            if path == "/api/rfq/get":
                ref = qs.get("ref", [""])[0]
                rfq = _load_rfq(ref) if ref else None
                if not rfq:
                    return self._json({"error": f"no RFQ {ref}"}, 404)
                return self._json({"rfq": _with_computed(rfq)})
            if path == "/api/notifications":
                since = int(qs.get("since", ["0"])[0] or 0)
                with _state_lock:
                    items = [n for n in NOTIFS if n["id"] > since]
                return self._json({"notifications": items})
            if path == "/api/ics":
                title = qs.get("title", ["Event"])[0]
                s = qs.get("start", [""])[0]
                e = qs.get("end", [""])[0]
                if not (s and e):
                    return self._json({"error": "start/end required"}, 400)
                ics = build_ics(title, s, e).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("Content-Disposition",
                                 'attachment; filename="event.ics"')
                self.send_header("Content-Length", str(len(ics)))
                self.end_headers()
                self.wfile.write(ics)
                return
            return self._json({"error": "unknown endpoint"}, 404)

        # ---- static PWA files ----------------------------------------------
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        rel = os.path.normpath(rel)
        if rel.startswith(".."):
            return self._json({"error": "bad path"}, 400)
        full = os.path.join(WWW_DIR, rel)
        if not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "read failed"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext,
                                                   "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # v0.8.6: never let the phone's HTTP cache pin an old app.js — the
        # service worker fetches "network-first", but Chrome can satisfy that
        # from its disk cache when no cache headers are sent.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- POST -----------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not self._authed(qs):
            return self._json({"error": "bad token"}, 401)
        data = self._read_json()

        if u.path == "/api/query":
            text = (data.get("text") or "").strip()
            if not text:
                return self._json({"error": "text required"}, 400)
            try:
                return self._json(handle_query(text))
            except Exception as e:  # noqa: BLE001
                log.exception("query failed")
                return self._json({"reply": f"Server error: {e}",
                                   "speak": "Sorry, that failed on the server.",
                                   "results": [], "action": "error"})

        if u.path == "/api/extract_events":
            text = (data.get("text") or "").strip()
            events = extract_events(text)
            speak = (f"I found {len(events)} event"
                     f"{'s' if len(events) != 1 else ''} to add."
                     if events else "I didn't hear any dates or times.")
            return self._json({"events": events, "speak": speak})

        if u.path == "/api/notify":
            n = push_notification(data.get("title", "MaINbox"),
                                  data.get("body", ""))
            return self._json({"ok": True, "id": n["id"]})

        if u.path == "/api/notifications/dismiss":
            nid = int(data.get("id") or 0)
            with _state_lock:
                keep = [n for n in NOTIFS if n["id"] != nid]
                NOTIFS.clear()
                NOTIFS.extend(keep)
            return self._json({"ok": True})

        if u.path == "/api/notifications/clear":
            with _state_lock:
                NOTIFS.clear()
            return self._json({"ok": True})

        if u.path == "/api/rfq/send":
            return self._json(release_rfq(data.get("ref", "")))

        if u.path == "/api/rfq/delete":
            return self._json(delete_rfq(data.get("ref", "")))

        if u.path == "/api/rfq/restore":
            return self._json(restore_rfq(data.get("ref", "")))

        if u.path == "/api/rfq":
            try:
                return self._json(create_rfq(data))
            except Exception as e:  # noqa: BLE001
                log.exception("rfq failed")
                return self._json({"ok": False, "error": str(e)}, 500)

        if u.path == "/api/rfq/preview":
            try:
                # v0.8.0: with a ref, preview the SAVED rfq (card preview /
                # edit prefill); without, render the compose form (original)
                if (data.get("ref") or "").strip():
                    return self._json(preview_saved_rfq(data["ref"].strip()))
                return self._json(preview_rfq(data))
            except Exception as e:  # noqa: BLE001
                log.exception("preview failed")
                return self._json({"ok": False, "error": str(e)}, 500)

        if u.path == "/api/rfq/update":
            try:
                return self._json(update_rfq(data))
            except Exception as e:  # noqa: BLE001
                log.exception("update failed")
                return self._json({"ok": False, "error": str(e)}, 500)

        if u.path == "/api/call_transcript":
            return self._json(save_call_transcript(
                data.get("filename", ""), data.get("text", "")))

        if u.path == "/api/rfq/hide":
            return self._json(hide_rfq(data.get("ref", "")))

        if u.path == "/api/rfq/unhide":
            return self._json(unhide_rfq(data.get("ref", "")))

        if u.path == "/api/rfq/note":
            return self._json(add_rfq_note(data.get("ref", ""),
                                           data.get("text", "")))

        if u.path == "/api/rfq/event":
            return self._json(add_rfq_event(data.get("ref", ""),
                                            data.get("event", ""),
                                            data.get("vendor", ""),
                                            data.get("detail", "")))

        if u.path == "/api/rfq/reply":
            return self._json(record_reply(
                data.get("subject", ""), data.get("body", ""),
                data.get("from", "") or data.get("sender", "")))

        if u.path == "/api/rfq/parse_line":
            return self._json(parse_rfq_line(data.get("text", "")))

        return self._json({"error": "unknown endpoint"}, 404)


def _load_or_make_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token
    env = os.environ.get("MBB_VOICE_TOKEN")
    if env:
        return env
    if os.path.isfile(TOKEN_FILE):
        try:
            return open(TOKEN_FILE, encoding="utf-8").read().strip()
        except OSError:
            pass
    tok = secrets.token_urlsafe(12)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


def _tailscale_ip() -> str:
    """Best-effort: find this machine's Tailscale 100.x.x.x address.
    Tries `tailscale ip` (looking in PATH and the default Windows install
    location), then falls back to scanning interfaces for a 100.64-100.127
    address (the CGNAT block Tailscale uses). Returns '' if nothing found."""
    import subprocess
    candidates = [
        ["tailscale", "ip", "-4"],
        [r"C:\Program Files\Tailscale\tailscale.exe", "ip", "-4"],
        [r"C:\Program Files (x86)\Tailscale\tailscale.exe", "ip", "-4"],
    ]
    for cmd in candidates:
        try:
            out = subprocess.check_output(
                cmd, timeout=3, stderr=subprocess.DEVNULL, text=True).strip()
            if out and out.startswith("100."):
                return out.splitlines()[0].strip()
        except Exception:  # noqa: BLE001
            continue
    # fallback: scan interfaces for the Tailscale CGNAT range
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            parts = ip.split(".")
            if len(parts) == 4 and parts[0] == "100":
                if 64 <= int(parts[1]) <= 127:
                    return ip
    except Exception:  # noqa: BLE001
        pass
    return ""


def _find_certs(certfile: str | None, keyfile: str | None):
    """Explicit --certfile/--keyfile win; else auto-detect one .crt/.key pair
    in ./certs/ (drop your `tailscale cert` output there). Returns
    (cert, key, hostname_hint) or (None, None, None)."""
    if certfile and keyfile:
        host = os.path.basename(certfile)
        host = host[:-4] if host.endswith(".crt") else host
        return certfile, keyfile, host
    if os.path.isdir(CERTS_DIR):
        crts = [f for f in os.listdir(CERTS_DIR) if f.endswith(".crt")]
        keys = [f for f in os.listdir(CERTS_DIR) if f.endswith(".key")]
        if len(crts) == 1 and len(keys) == 1:
            return (os.path.join(CERTS_DIR, crts[0]),
                    os.path.join(CERTS_DIR, keys[0]), crts[0][:-4])
    return None, None, None


def main(argv=None) -> int:
    global TOKEN
    ap = argparse.ArgumentParser(description="MaINbox Voice server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--token", default=None,
                    help="shared secret (default: auto, saved to .voice_token)")
    ap.add_argument("--certfile", default=None,
                    help="TLS cert (e.g. from `tailscale cert`)")
    ap.add_argument("--keyfile", default=None, help="TLS private key")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s")

    TOKEN = _load_or_make_token(args.token)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)

    cert, key, host_hint = _find_certs(args.certfile, args.keyfile)
    scheme = "http"
    if cert and key:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            scheme = "https"
        except Exception as e:  # noqa: BLE001
            print(f"  !! TLS setup failed ({e}) — falling back to http")
            cert = None

    print(f"\nMaINbox Voice v{__version__}")
    print(f"  serving  : {scheme}://{args.host}:{args.port}/  (www: {WWW_DIR})")
    if scheme == "https":
        print(f"  TLS cert : {cert}")
        print(f"  phone URL: https://{host_hint or '<cert-hostname>'}:"
              f"{args.port}/?token={TOKEN}")
        print("             (use the cert's hostname, not the IP — the mic "
              "needs the names to match)")
    else:
        ts_ip = _tailscale_ip()
        if ts_ip:
            print(f"  phone URL: http://{ts_ip}:{args.port}/?token={TOKEN}")
        else:
            print(f"  phone URL: http://<this-pc-tailscale-ip>:{args.port}/"
                  f"?token={TOKEN}")
            print("             (couldn't detect Tailscale IP — run "
                  "'tailscale ip' to find it)")
        print("             (mic blocked on plain http — see README, or add "
              "certs/ for https)")
    print(f"  token    : {TOKEN}")
    _xrv = getattr(xr, "__version__", "?") if xr else "missing"
    print(f"  toolkit  : parser={'OK' if xc else 'missing'} "
          f"dispatch={'OK' if xd else 'missing'} "
          f"store={'v' + _xrv if xr else 'missing'}")
    # v0.8.5: stale-module tripwire — transitive equals need store >= 0.2.x
    if xr is not None:
        import inspect as _insp
        try:
            _has_graph = "transitive" in _insp.signature(xr.lookup).parameters
        except Exception:  # noqa: BLE001
            _has_graph = False
        if not _has_graph:
            print("  WARNING  : cross_reference.py in THIS folder is an old "
                  "version — transitive equals are OFF. Replace it here and "
                  "restart.")
    print("  Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
