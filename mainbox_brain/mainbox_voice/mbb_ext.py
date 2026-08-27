"""MaINbox Voice v0.10 extension module.

Three things the phone needed that the server didn't have, kept in one
stdlib-only module so brain_voice_server.py stays the thin HTTP layer:

1. **Email references** -- when the Brain answers a price / stock / lead-time
   question it now names the emails it read (their Outlook EntryIDs, via
   reply_records.source_key). ``mail_get(key)`` turns such a key back into the
   message (COM on the Outlook PC first, then the JSON exports, then the
   mined excerpt) so the phone can show it; ``mail_open(key)`` pops it open in
   Outlook on the desktop.

2. **MaINbox follow-up sync** -- the desktop app publishes its follow-up
   queue through its file bridge (``%LOCALAPPDATA%\\MaINbox\\bridge``) and
   polls the same folder for commands on its Tk main thread every 4s. This
   module is the phone-side client: read the snapshot, drop a command file,
   wait for the result. Nothing here ever writes MaINbox's own JSON files.

3. **Durable server state** -- alerts and the voice session survive a restart
   (``state_save`` / ``state_load``), and a watcher thread raises an alert on
   the phone when a follow-up comes due.
"""
from __future__ import annotations

import os
import re
import json
import time
import uuid
import glob
import html
import sqlite3
import logging
import threading
from datetime import datetime, timedelta

log = logging.getLogger("mbb_voice.ext")

_BASE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BASE)          # ...\mainbox_brain (repo root)

# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
MAINBOX_DB = os.environ.get("MBB_MAINBOX_DB",
                            os.path.join(_ROOT, "mainbox.db"))
EXPORT_FILES = [p for p in (
    os.environ.get("MBB_RECEIVED_EXPORT", ""),
    os.path.join(_ROOT, "received_export.json"),
    os.path.join(_ROOT, "received_export_sales.json"),
    os.path.join(_ROOT, "sales_inbox.json"),
) if p]
# v0.10.2: state lives OUTSIDE the OneDrive-synced repo -- OneDrive holds a
# lock on freshly written files and os.replace() then fails with
# "Access is denied", so every save was lost. %LOCALAPPDATA% is never synced.
_STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "MaINbox")
STATE_FILE = os.path.join(_STATE_DIR, "voice_state.json")
_LEGACY_STATE_FILE = os.path.join(_BASE, "voice_state.json")

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
BRIDGE_DIR = (os.environ.get("MAINBOX_BRIDGE_DIR", "").strip()
              or os.path.join(_LOCALAPPDATA, "MaINbox", "bridge"))
BRIDGE_FOLLOWUPS = os.path.join(BRIDGE_DIR, "followups.json")
BRIDGE_HEARTBEAT = os.path.join(BRIDGE_DIR, "heartbeat.json")
BRIDGE_CMD_DIR = os.path.join(BRIDGE_DIR, "commands")
BRIDGE_RES_DIR = os.path.join(BRIDGE_DIR, "results")
# the desktop polls every 4s; wait a little longer than one tick
BRIDGE_WAIT_S = float(os.environ.get("MBB_BRIDGE_WAIT", "7"))


# ==========================================================================
# 1. EMAIL REFERENCES
# ==========================================================================
_export_index: dict | None = None
_export_lock = threading.Lock()


def _load_export_index() -> dict:
    """message_id (EntryID) -> record, built lazily once from the Outlook
    JSON exports (the reply miner's ingest source; full bodies live there)."""
    global _export_index
    with _export_lock:
        if _export_index is not None:
            return _export_index
        idx: dict = {}
        for path in EXPORT_FILES:
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    for rec in json.load(f):
                        k = (rec.get("message_id") or "").strip()
                        if k and k not in idx:
                            idx[k] = rec
            except Exception as e:  # noqa: BLE001
                log.warning("export index: %s unreadable (%s)", path, e)
        _export_index = idx
        log.info("export index: %d messages", len(idx))
        return idx


def _db_record(key: str) -> dict | None:
    if not os.path.isfile(MAINBOX_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{MAINBOX_DB}?mode=ro", uri=True,
                              check_same_thread=False)
        try:
            row = con.execute(
                "SELECT vendor_name, from_email, from_name, subject, received_at, "
                "body_excerpt, items, facts FROM reply_records WHERE source_key=?",
                (key,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.warning("mainbox.db read failed: %s", e)
        return None
    if not row:
        return None
    vendor, frm, frm_name, subj, when, excerpt, items, facts = row
    try:
        facts = json.loads(facts or "[]")
    except ValueError:
        facts = []
    return {"vendor": vendor or "", "from": frm or "", "from_name": frm_name or "",
            "subject": subj or "", "when": when or "", "body": excerpt or "",
            "facts": facts, "partial": True}


def _com_message(key: str) -> dict | None:
    """Read the live item from Outlook by EntryID (this server runs on the
    Outlook PC). Runs COM on this worker thread with its own apartment."""
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = ol.GetNamespace("MAPI")
        item = ns.GetItemFromID(key)
        sender_email = ""
        try:
            sender_email = item.SenderEmailAddress or ""
            if item.SenderEmailType == "EX":   # exchange DN -> SMTP
                try:
                    sender_email = item.Sender.GetExchangeUser().PrimarySmtpAddress or sender_email
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        when = ""
        try:
            when = item.ReceivedTime.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:  # noqa: BLE001
            pass
        atts = []
        try:
            for i in range(1, item.Attachments.Count + 1):
                atts.append(item.Attachments.Item(i).FileName)
        except Exception:  # noqa: BLE001
            pass
        return {"from": sender_email, "from_name": getattr(item, "SenderName", "") or "",
                "to": getattr(item, "To", "") or "", "subject": item.Subject or "",
                "when": when, "body": (item.Body or "")[:20000],
                "attachments": atts, "partial": False, "source": "outlook"}
    except Exception as e:  # noqa: BLE001
        log.info("COM read of %s failed: %s", key[:16], e)
        return None
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def mail_get(key: str) -> dict:
    """Best available copy of the email behind a source_key."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "missing key"}
    db = _db_record(key)
    live = _com_message(key)
    if live:
        if db:
            live["vendor"] = db.get("vendor", "")
            live["facts"] = db.get("facts", [])
        return {"ok": True, "key": key, **live}
    exp = _load_export_index().get(key)
    if exp:
        out = {"from": exp.get("from", ""), "from_name": exp.get("from_name", ""),
               "subject": exp.get("subject", ""), "when": exp.get("when", "") or "",
               "body": exp.get("body", "") or "", "attachments": exp.get("attachments", []),
               "partial": False, "source": "export"}
        if db:
            out["vendor"] = db.get("vendor", "")
            out["facts"] = db.get("facts", [])
        return {"ok": True, "key": key, **out}
    if db:
        db["source"] = "mined excerpt"
        return {"ok": True, "key": key, **db}
    return {"ok": False, "error": "I don't have that email on file any more"}


def mail_open(key: str) -> dict:
    """Display the message in Outlook on the desktop (the PC this runs on)."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "missing key"}
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "Outlook isn't on this PC"}
    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        item = ol.GetNamespace("MAPI").GetItemFromID(key)
        item.Display()
        try:
            item.GetInspector.Activate()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "subject": item.Subject or ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Outlook couldn't open it: {e}"}
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _fmt_when(when: str) -> str:
    try:
        return datetime.fromisoformat(when[:19]).strftime("%b %d, %Y %I:%M %p")
    except Exception:  # noqa: BLE001
        return when or ""


def mail_view_html(key: str, token: str, base: str = "") -> str:
    """Self-contained phone page for one email (dark, mobile-first)."""
    m = mail_get(key)
    esc = html.escape
    if not m.get("ok"):
        body = f"<p class='err'>{esc(m.get('error', 'not found'))}</p>"
        title = "Email not found"
    else:
        title = m.get("subject") or "(no subject)"
        facts = ""
        if m.get("facts"):
            rows = []
            for f in m["facts"][:12]:
                bits = [f.get("item") or ""]
                if f.get("unit_price") is not None:
                    bits.append(f"${f['unit_price']:g}" + (f"/{f['unit']}" if f.get("unit") else ""))
                if f.get("lead_time"):
                    bits.append(f"lead {f['lead_time']}")
                if f.get("availability"):
                    bits.append(str(f["availability"]).replace("_", " "))
                rows.append("<li>" + esc(" — ".join(b for b in bits if b)) + "</li>")
            facts = "<h3>What MaINbox read from it</h3><ul class='facts'>" + "".join(rows) + "</ul>"
        atts = ""
        if m.get("attachments"):
            atts = "<div class='meta'>📎 " + esc(", ".join(m["attachments"][:8])) + "</div>"
        partial = ("<div class='note'>Only the mined excerpt is on file — open it in Outlook for the full message.</div>"
                   if m.get("partial") else "")
        body = f"""
<div class='meta'><b>From:</b> {esc(m.get('from_name') or '')} &lt;{esc(m.get('from') or '')}&gt;</div>
{('<div class="meta"><b>To:</b> ' + esc(m['to']) + '</div>') if m.get('to') else ''}
<div class='meta'><b>Date:</b> {esc(_fmt_when(m.get('when') or ''))}</div>
{('<div class="meta"><b>Vendor:</b> ' + esc(m['vendor']) + '</div>') if m.get('vendor') else ''}
{atts}{partial}
<div class='acts'><button id='op'>Open in Outlook on the PC</button><span id='opmsg'></span></div>
{facts}
<h3>Message</h3>
<pre>{esc(m.get('body') or '')}</pre>"""
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)}</title>
<style>
body{{margin:0;background:#0a0e14;color:#e6edf3;font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;padding:14px}}
h1{{font-size:17px;margin:0 0 10px}} h3{{font-size:13px;color:#8aa0b3;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.4px}}
.meta{{color:#8aa0b3;font-size:13px;margin:2px 0}} .meta b{{color:#e6edf3}}
.note{{background:#1c2b3a;border-radius:8px;padding:8px 10px;font-size:13px;margin:10px 0}}
.err{{color:#ff5252}}
pre{{white-space:pre-wrap;word-break:break-word;background:#111a26;border:1px solid #1c2b3a;border-radius:12px;padding:12px;font:inherit}}
.facts{{padding-left:18px}} .facts li{{margin:3px 0}}
.acts{{margin:12px 0}} button{{background:#1e88e5;color:#fff;border:none;border-radius:10px;padding:11px 14px;font-size:14px;font-weight:650}}
#opmsg{{margin-left:10px;color:#8aa0b3;font-size:13px}}
a.back{{color:#1e88e5;text-decoration:none;font-size:14px}}
</style></head><body>
<a class='back' href='{esc(base or "/")}'>‹ MaINbox Voice</a>
<h1>{esc(title)}</h1>
{body}
<script>
var b=document.getElementById('op');if(b){{b.onclick=function(){{
 document.getElementById('opmsg').textContent='opening…';
 fetch('/api/mail/open',{{method:'POST',headers:{{'Content-Type':'application/json','X-MBB-Token':{json.dumps(token)}}},
   body:JSON.stringify({{key:{json.dumps(key)}}})}}).then(function(r){{return r.json()}}).then(function(d){{
   document.getElementById('opmsg').textContent=d.ok?'✓ open on the PC':('✗ '+(d.error||'failed'));}})
 .catch(function(){{document.getElementById('opmsg').textContent='✗ server unreachable';}});}};}}
</script></body></html>"""


def sources_with_links(sources: list[dict], token: str) -> list[dict]:
    """Attach the phone-openable viewer URL to each source card."""
    out = []
    for s in sources or []:
        s = dict(s)
        if s.get("key"):
            from urllib.parse import quote
            s["url"] = f"/api/mail/view?key={quote(s['key'])}&token={quote(token)}"
        out.append(s)
    return out


def sources_reply_text(sources: list[dict]) -> str:
    if not sources:
        return ""
    lines = []
    for i, s in enumerate(sources, 1):
        who = s.get("vendor") or s.get("from_name") or s.get("from") or "?"
        contact = s.get("from_name") or s.get("from") or ""
        seg = f"{i}. {who}"
        if contact and contact != who:
            seg += f" ({contact})"
        seg += f" — \"{(s.get('subject') or '(no subject)')[:60]}\" — {_fmt_when(s.get('when') or '')[:12]}"
        roles = ", ".join(s.get("roles") or [])
        if roles:
            seg += f" — {roles}"
        if not s.get("key"):
            seg += " — (email no longer on file)"
        lines.append(seg)
    return "\n".join(lines)


# ==========================================================================
# 2. MaINbox FOLLOW-UP BRIDGE CLIENT
# ==========================================================================
def mainbox_online(max_age_s: float = 15.0) -> tuple[bool, str]:
    """Is the desktop app alive? Its heartbeat is rewritten every 4s."""
    try:
        with open(BRIDGE_HEARTBEAT, encoding="utf-8") as f:
            hb = json.load(f)
        age = time.time() - float(hb.get("ts") or 0)
        return age <= max_age_s, str(hb.get("app_version") or "")
    except Exception:  # noqa: BLE001
        return False, ""


def followups_snapshot() -> dict:
    """The desktop's published queue (all lanes), plus liveness."""
    online, ver = mainbox_online()
    items: list = []
    published = 0.0
    try:
        with open(BRIDGE_FOLLOWUPS, encoding="utf-8") as f:
            snap = json.load(f)
        if isinstance(snap, dict):
            items = snap.get("items") or []
            published = float(snap.get("published_at") or 0)
        elif isinstance(snap, list):
            items = snap
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("followups snapshot unreadable: %s", e)
    now = datetime.now()
    for it in items:
        try:
            due = datetime.fromisoformat(it.get("due", "") or "")
            it["overdue"] = due <= now
            it["due_display"] = due.strftime("%m/%d %I:%M %p")
        except Exception:  # noqa: BLE001
            it["overdue"] = False
            it["due_display"] = ""
    items.sort(key=lambda i: i.get("due") or "9999")
    return {"ok": True, "items": items, "mainbox_online": online,
            "mainbox_version": ver, "published_at": published,
            "bridge_dir": BRIDGE_DIR}


def followup_command(name: str, args: dict, wait: float | None = None) -> dict:
    """Queue a command for the desktop and wait (briefly) for its answer.
    Returns the desktop's result, or {"queued": True} if it hasn't answered
    yet (the app is closed or busy) -- the command file stays and is applied
    when MaINbox next ticks."""
    online, _ = mainbox_online()
    try:
        os.makedirs(BRIDGE_CMD_DIR, exist_ok=True)
        os.makedirs(BRIDGE_RES_DIR, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"bridge folder unavailable: {e}"}
    cid = f"{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    cmd = {"id": cid, "name": name, "args": args or {}, "ts": time.time(),
           "from": "voice"}
    path = os.path.join(BRIDGE_CMD_DIR, cid + ".json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cmd, f)
        os.replace(tmp, path)          # atomic: the desktop never sees a half file
    except OSError as e:
        return {"ok": False, "error": f"couldn't queue command: {e}"}
    res_path = os.path.join(BRIDGE_RES_DIR, cid + ".json")
    deadline = time.time() + (BRIDGE_WAIT_S if wait is None else wait)
    if not online:
        deadline = min(deadline, time.time() + 1.0)
    while time.time() < deadline:
        if os.path.isfile(res_path):
            try:
                with open(res_path, encoding="utf-8") as f:
                    res = json.load(f)
                try:
                    os.remove(res_path)
                except OSError:
                    pass
                return res if isinstance(res, dict) else {"ok": True, "result": res}
            except (OSError, ValueError):
                time.sleep(0.15)
                continue
        time.sleep(0.25)
    return {"ok": True, "queued": True, "id": cid, "mainbox_online": online,
            "message": ("MaINbox is busy — it'll apply this on its next tick."
                        if online else
                        "MaINbox isn't running — queued; it applies the moment MaINbox opens.")}


# -- natural-language due times ("tomorrow 9am", "in 2 hours", "friday 3pm")
_WD = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
       "saturday": 5, "sunday": 6, "mon": 0, "tue": 1, "tues": 1, "wed": 2,
       "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_due(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    t = (text or "").strip().lower()
    if not t:
        return None
    try:                                   # ISO from the phone's picker
        return datetime.fromisoformat(text.strip())
    except ValueError:
        pass
    m = re.search(r"\bin\s+(\d+|an?)\s*(min(?:ute)?s?|hours?|hrs?|days?|weeks?)\b", t)
    if m:
        n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        u = m.group(2)
        delta = (timedelta(minutes=n) if u.startswith("min") else
                 timedelta(hours=n) if u.startswith(("h",)) else
                 timedelta(days=n) if u.startswith("day") else timedelta(weeks=n))
        return (now + delta).replace(second=0, microsecond=0)
    # day part
    day = None
    if re.search(r"\btomorrow\b", t):
        day = now.date() + timedelta(days=1)
    elif re.search(r"\btoday\b", t):
        day = now.date()
    elif re.search(r"\bnext week\b", t):
        day = now.date() + timedelta(days=(7 - now.weekday()) % 7 or 7)
    else:
        mw = re.search(r"\b(" + "|".join(sorted(_WD, key=len, reverse=True)) + r")\b", t)
        if mw:
            target = _WD[mw.group(1)]
            ahead = (target - now.weekday()) % 7
            if ahead == 0 and not re.search(r"\bthis\b", t):
                ahead = 7
            day = now.date() + timedelta(days=ahead)
    md = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if md:
        yr = int(md.group(3)) if md.group(3) else now.year
        if yr < 100:
            yr += 2000
        try:
            day = datetime(yr, int(md.group(1)), int(md.group(2))).date()
            if not md.group(3) and day < now.date():
                day = day.replace(year=yr + 1)
        except ValueError:
            pass
    # time part
    hh, mm = None, 0
    mt = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", t)
    if mt:
        hh = int(mt.group(1)) % 12
        mm = int(mt.group(2) or 0)
        if mt.group(3).startswith("p"):
            hh += 12
    else:
        mt = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
        if mt:
            hh, mm = int(mt.group(1)), int(mt.group(2))
        elif re.search(r"\bnoon\b", t):
            hh = 12
        elif re.search(r"\b(morning)\b", t):
            hh = 9
        elif re.search(r"\b(afternoon)\b", t):
            hh = 14
        elif re.search(r"\b(evening|tonight)\b", t):
            hh = 18
        elif re.search(r"\bend of (?:the )?day\b|\beod\b", t):
            hh = 16
    if day is None and hh is None:
        return None
    if day is None:
        day = now.date()
        cand = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
        if cand <= now:
            cand += timedelta(days=1)
        return cand
    if hh is None:
        hh = 9                              # a day with no time -> 9 AM
        cand = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
        if cand <= now:                     # "today" after 9 AM -> in an hour
            cand = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return cand
    return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)


_FU_TIME_WORDS = (r"(?:tomorrow|today|tonight|next week|noon|morning|afternoon|evening|"
                  r"end of (?:the )?day|eod|in \d+ \w+|in an? \w+|"
                  r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*|"
                  r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}(?::\d{2})?\s*(?:am|pm))")


def split_note_and_due(text: str) -> tuple[str, datetime | None]:
    """'call george about the panel tomorrow at 9' -> ('call george about the
    panel', tomorrow 09:00). The time phrase is stripped from the note."""
    t = (text or "").strip()
    due = parse_due(t)
    if due is None:
        return t, None
    note = re.sub(r"\b(?:on|at|by|for)?\s*" + _FU_TIME_WORDS + r"(?:\s*(?:at|@)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\s*",
                  " ", t, flags=re.IGNORECASE)
    note = re.sub(r"\s{2,}", " ", note).strip(" ,.-")
    return note or t, due


# ==========================================================================
# 3. DURABLE STATE + DUE WATCHER
# ==========================================================================
_state_write_lock = threading.Lock()


def state_save(session: dict, notifs: list, notif_id: int) -> None:
    """Persist the bits of the voice session + the alert list that should
    survive a server restart. Cheap and atomic; called after every change."""
    keep_keys = ("last", "last_xref_part", "last_refs", "last_priced_item",
                 "brain_session", "brain_pending", "last_full_reply",
                 "suggested_vendors", "last_sources", "last_call_transcript")
    sess = {}
    for k in keep_keys:
        v = session.get(k)
        if v is None:
            continue
        try:
            json.dumps(v)
            sess[k] = v
        except (TypeError, ValueError):
            continue
    data = {"saved_at": time.time(), "session": sess,
            "notifs": list(notifs), "notif_id": int(notif_id)}
    with _state_write_lock:
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
        except OSError:
            pass
        tmp = STATE_FILE + ".tmp"
        last = None
        for attempt in range(4):              # a sync client may hold the file briefly
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, STATE_FILE)
                return
            except OSError as e:
                last = e
                time.sleep(0.2 * (attempt + 1))
        try:                                  # last resort: plain overwrite
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            log.warning("state save failed: %s (after %s)", e, last)


def state_load() -> dict:
    for path in (STATE_FILE, _LEGACY_STATE_FILE):   # legacy: pre-v0.10.2 location
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("state load failed (%s): %s", path, e)
    return {}


class DueWatcher(threading.Thread):
    """Raises a phone alert when a MaINbox follow-up comes due (and again if
    it's still open an hour later), and one when the desktop goes offline /
    comes back. Alerts are keyed by (id, due) so a snooze re-arms them."""

    def __init__(self, push, interval: float = 30.0):
        super().__init__(name="mbb-due-watcher", daemon=True)
        self.push = push
        self.interval = interval
        self.fired: dict[str, float] = {}     # key -> last fired ts
        self.fire_count: dict[str, int] = {}  # key -> how many times alerted
        self.was_online: bool | None = None

    def run(self) -> None:
        time.sleep(5)
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("due watcher: %s", e)
            time.sleep(self.interval)

    def tick(self) -> None:
        snap = followups_snapshot()
        online = snap.get("mainbox_online", False)
        if self.was_online is not None and online != self.was_online:
            if online:
                self.push("MaINbox is back online",
                          "Follow-ups are syncing again.", kind="mainbox")
            # (no alert on going offline -- the tab shows it; closing the
            #  app at night shouldn't buzz the phone)
        self.was_online = online
        now = time.time()
        for it in snap.get("items") or []:
            if it.get("status") not in ("open", "Active", None):
                continue
            due_ts = it.get("due_ts")
            if not due_ts or due_ts > now:
                continue
            key = f"{it.get('id')}|{it.get('due')}"
            last = self.fired.get(key)
            n_fired = self.fire_count.get(key, 0)
            # v0.10.2: due -> alert; still open an hour later -> one nudge;
            # after that once a day. (It used to nudge every hour, forever:
            # 65 alerts piled up overnight for one open follow-up.)
            gap = 0 if n_fired == 0 else 3600 if n_fired == 1 else 86400
            if last is None or now - last >= gap:
                self.fired[key] = now
                self.fire_count[key] = n_fired + 1
                who = it.get("vendor") or it.get("group") or ""
                title = f"⏰ Follow-up due: {(it.get('subject') or it.get('note') or '')[:70]}"
                body = ((who + " — ") if who else "") + (it.get("note") or "")[:160]
                self.push(title, body, kind="followup", ref=it.get("id"))
        # forget keys that vanished (completed/cancelled/snoozed)
        live = {f"{i.get('id')}|{i.get('due')}" for i in (snap.get("items") or [])}
        for k in list(self.fired):
            if k not in live:
                self.fired.pop(k, None)
                self.fire_count.pop(k, None)
