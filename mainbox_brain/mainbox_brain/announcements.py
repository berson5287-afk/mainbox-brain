"""Vendor announcement intelligence (v0.49).

Vendors broadcast price increases, surcharges, and policy changes in emails
that carry no quote facts -- so the reply miner rightly ignores them, and the
knowledge silently rots in the inbox (a Wesanco-ZSI increase effective Aug 1
sat unnoticed while the user asked exactly that question).

This module gives those emails a home:
  detect()        - is this email an announcement? extract vendor/date/size
  mine_records()  - sweep an export's records into the announcements table
  lookup()        - find announcements matching a vendor/brand query
  heads_up_for()  - one-line warning to append to price/vendor answers

Storage: `vendor_announcements` table in mainbox.db (created on demand).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

__version__ = "0.49"

_ANNOUNCE_RE = re.compile(
    r"\b(?:price\s+(?:increases?|adjustments?|changes?|revisions?)|surcharges?|"
    r"new\s+pricing|rate\s+increases?|price\s+files?)\b", re.I)
_KIND_INCREASE_RE = re.compile(
    r"\b(?:price\s+increase|rate\s+increase|surcharge|prices?\s+(?:will\s+)?"
    r"(?:go(?:ing)?\s+up|increas\w+|ris\w+))\b", re.I)
_EFFECTIVE_RE = re.compile(
    r"\beffective\s+(?:on\s+)?"
    r"((?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{2,4})", re.I)
_MAGNITUDE_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?\s*%(?:\s*(?:-|–|to)\s*\d{1,2}(?:\.\d+)?\s*%)?)")
_SUBJ_NOISE_RE = re.compile(r"^\s*(?:fw|fwd|re)\s*:\s*", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vendor_announcements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_hint    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    effective_text TEXT,
    effective_date TEXT,
    magnitude      TEXT,
    subject        TEXT,
    from_email     TEXT,
    received_at    TEXT,
    excerpt        TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(vendor_hint, kind, effective_text, subject)
);
"""


def _conn(db) -> sqlite3.Connection:
    """Accept a path or an existing connection; ensure the table exists."""
    if isinstance(db, sqlite3.Connection):
        c = db
    else:
        c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 \-]", "", (s or "").lower())).strip()


def _parse_effective(text: str) -> str:
    """Best-effort ISO date from 'August 1, 2026' or '8/1/26'; '' if unparsed."""
    t = (text or "").replace(",", " ")
    for fmt in ("%B %d %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(re.sub(r"\s+", " ", t).strip(), fmt) \
                .date().isoformat()
        except ValueError:
            continue
    return ""


def _vendor_hint(subject: str, from_email: str) -> str:
    """Best vendor identifier: the subject's leading brand words if present
    ('FW: Wesanco-ZSI Price Increase ...' -> 'Wesanco-ZSI'), else the sender
    domain's first label ('ordersne@wesanco-zsi.com' -> 'wesanco-zsi')."""
    subj = _SUBJ_NOISE_RE.sub("", subject or "").strip()
    m = re.match(r"([A-Za-z][\w&.\-]*(?:\s+[A-Za-z][\w&.\-]*){0,3}?)\s+"
                 r"(?=(?:price|rate|surcharge|new pricing)\b)", subj, re.I)
    if m and _norm(m.group(1)):
        return m.group(1).strip()
    dom = (from_email or "").split("@")[-1].lower()
    label = dom.split(".")[0] if dom else ""
    return label


def detect(subject: str, body: str, from_email: str = "",
           received_at: str = "") -> dict | None:
    """Return an announcement dict if this email is one, else None."""
    hay = f"{subject or ''}\n{(body or '')[:4000]}"
    if not _ANNOUNCE_RE.search(hay):
        return None
    kind = "price_increase" if _KIND_INCREASE_RE.search(hay) else "pricing_notice"
    eff = _EFFECTIVE_RE.search(hay)
    effective_text = eff.group(1).strip() if eff else ""
    mag = ""
    m = _MAGNITUDE_RE.search(hay)
    if m:
        mag = re.sub(r"\s+", "", m.group(1)).replace("to", "-")
    # excerpt: the sentence around the trigger, for provenance
    trig = _KIND_INCREASE_RE.search(body or "") or _ANNOUNCE_RE.search(body or "")
    excerpt = ""
    if trig:
        start = max(0, (body or "").rfind(".", 0, trig.start()) + 1)
        end = (body or "").find(".", trig.end())
        excerpt = (body or "")[start:end if end != -1 else trig.end() + 160]
        excerpt = re.sub(r"\s+", " ", excerpt).strip()[:280]
    hint = _vendor_hint(subject, from_email)
    if not hint:
        return None
    return {"vendor_hint": hint, "kind": kind,
            "effective_text": effective_text,
            "effective_date": _parse_effective(effective_text),
            "magnitude": mag, "subject": (subject or "").strip(),
            "from_email": (from_email or "").strip(),
            "received_at": (received_at or "").strip(), "excerpt": excerpt}


def save(db, ann: dict) -> bool:
    """Insert if new; True when a row was added."""
    c = _conn(db)
    cur = c.execute(
        "INSERT OR IGNORE INTO vendor_announcements "
        "(vendor_hint, kind, effective_text, effective_date, magnitude, "
        " subject, from_email, received_at, excerpt, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ann["vendor_hint"], ann["kind"], ann.get("effective_text", ""),
         ann.get("effective_date", ""), ann.get("magnitude", ""),
         ann.get("subject", ""), ann.get("from_email", ""),
         ann.get("received_at", ""), ann.get("excerpt", ""),
         datetime.now().isoformat(timespec="seconds")))
    c.commit()
    return cur.rowcount > 0


def mine_records(records, db) -> int:
    """Sweep exporter records (dicts with subject/body/from/when) into the
    table. Returns how many NEW announcements were stored."""
    n = 0
    for r in records or []:
        ann = detect(r.get("subject", ""), r.get("body", ""),
                     r.get("from", ""), r.get("when", "") or "")
        if ann and save(db, ann):
            n += 1
    return n


_STOP = {"price", "prices", "increase", "increases", "adjustment", "change",
         "changes", "surcharge", "pricing", "rate", "rates", "new", "the",
         "and", "for", "with", "from", "having", "about", "when", "will",
         "does", "did", "are", "there", "any", "coming", "going", "raising",
         "raise", "raised", "effective", "vendor", "who", "what", "that",
         "this", "you", "know"}


def lookup(db, query: str, limit: int = 5) -> list[sqlite3.Row]:
    """Announcements whose vendor hint overlaps the query's NAME tokens,
    newest first. Stopwords and short tokens are ignored so 'is topaz having a
    price increase' can only match on 'topaz'."""
    q = _norm(query)
    qtoks = {t for t in q.split() if len(t) >= 3 and t not in _STOP}
    if len(q) >= 4:
        qtoks.add(q)
    if not qtoks:
        return []
    rows = _conn(db).execute(
        "SELECT * FROM vendor_announcements "
        "ORDER BY received_at DESC, id DESC LIMIT 100").fetchall()
    out = []
    for r in rows:
        h = _norm(r["vendor_hint"])
        htoks = {h} | set(h.split()) | set(h.split("-"))
        hit = any(a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a))
                  for a in qtoks for b in htoks)
        if hit:
            out.append(r)
            if len(out) >= limit:
                break
    return out


def recent(db, limit: int = 5) -> list[sqlite3.Row]:
    """Newest announcements regardless of vendor -- for 'any price increases
    coming?' style questions that name nobody."""
    return _conn(db).execute(
        "SELECT * FROM vendor_announcements "
        "ORDER BY received_at DESC, id DESC LIMIT ?", (limit,)).fetchall()


def _fmt_date(iso_or_text: str, text: str) -> str:
    return text or iso_or_text or "an unspecified date"


def format_heads_up(r: sqlite3.Row) -> str:
    """One-line warning suitable for appending to an answer."""
    eff_iso, eff_txt = r["effective_date"] or "", r["effective_text"] or ""
    when = _fmt_date(eff_iso, eff_txt)
    tense = "takes effect"
    if eff_iso:
        try:
            if datetime.fromisoformat(eff_iso).date() < datetime.now().date():
                tense = "took effect"
        except ValueError:
            pass
    mag = f" (avg {r['magnitude']})" if r["magnitude"] else ""
    src = (r["received_at"] or "")[:10]
    src = f" — announced {src}" if src else ""
    label = ("price increase" if r["kind"] == "price_increase"
             else "pricing notice")
    return (f"⚠ Heads up: {r['vendor_hint']} announced a {label}{mag} that "
            f"{tense} {when}{src} ({r['subject'][:70]!r}).")


def heads_up_for(db, query: str) -> str:
    """Newest matching announcement as a warning line, or ''. `query` can be a
    vendor name, a brand, or a whole product string."""
    try:
        rows = lookup(db, query, limit=1)
    except Exception:
        return ""
    return format_heads_up(rows[0]) if rows else ""
