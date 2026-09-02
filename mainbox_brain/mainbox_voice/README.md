# MaINbox Voice — v0.10.3 · Brain v0.11

## v0.10.3 — security + audit fixes (2026-09-02)
**SECURITY:** the static file handler resolved drive-absolute request paths
(`GET /C:/...`) outside `www/` with no token — any file on the PC was readable
from the tailnet. Fixed with absolute-path/colon rejection plus a commonpath
containment check (verified: 400 now, PWA unaffected). Also from the audit:
the due-alert watcher no longer resets its backoff on an empty/torn snapshot
(no re-storms); `mail_get`/`mail_open` resolve EntryIDs against every Outlook
store and require an exact EntryID match (Sales-mailbox emails now open, and a
wrong-store id can never show a stranger's mail); the bridge adapter (1.0.2)
never truncates a file in place when a rename is blocked (stale beats torn);
the PWA replays past one corrupt feed entry, separates display errors from
"offline", caps the saved conversation by bytes, and a token link from a new
server now updates the server URL too.


## v0.10.2 — server state moved out of OneDrive; alert nudges tamed (2026-08-27)
`voice_state.json` now lives in `%LOCALAPPDATA%\MaINbox\` (OneDrive held a lock
on the freshly written file, so every save failed with "Access is denied"). The
due-follow-up watcher alerts once at due, once an hour later, then daily — it used
to nudge every hour forever (65 alerts overnight for one open follow-up).


## Brain v0.11 — material typing from Steve's own data (2026-08-27)
The 9-bucket keyword table is gone. A research workflow mined 41K RFQ/reply
item lines, 34.5K catalog SKUs and 10.9K sent messages into
`mainbox_brain/material_rules.json` — **42 fine categories in 9 groups**
(conduit_emt vs conduit_pvc vs conduit_rigid_imc, fittings_emt/rigid/pvc/flex,
circuit_breakers, panelboards, disconnects, transformers, lugs, grounding,
wiring devices, lighting fixtures/lamps/controls, low-voltage cable, cord…),
**479 brand aliases** (Arlington, Bridgeport, Topaz, ILSCO, Burndy, Square D,
Eaton, Southwire…) and a **curated vendor map** (which domain gets your RFQs
for which material). Engine: `material_classify.py` — same file and data ship
in the MaINbox desktop app (`mainbox_material.py`, v4.2.100+) so both type
material identically. `catalog.category_for_text` delegates to it; the resolver
qualifies a vendor on exact-category history (≥2 RFQs) or the vendor map, with
same-family history as a tie-breaker only. Existing records were re-tagged
(554 of 1,095); the 15-minute auto-refresh re-mines with the new engine.
Full write-up: `MATERIAL_TAXONOMY_REPORT.md`. To change typing, edit the JSON
(word-bounded Python regexes, `negative` vetoes, `priority`) and copy it to
both trees.


## v0.10.1 — RFQ follow-up fixes (2026-08-27)
- **Real contact names.** "Markh at Brazill" is now "Mark Huddle at Brazill":
  the sent-mail miner only knew the mailbox; vendor *replies* carry the display
  name, so `Store.refresh_contact_names()` upgrades every mailbox-derived name
  from the most frequent reply name (runs after every re-mine; 50 fixed today).
- **Wrong vendors for MC cable.** "mc" matched inside *IMC* conduit and *300
  MCM*, so one stray RFQ tagged a conduit house as an MC-cable vendor. Short
  category tokens now need letter boundaries, 52 mis-tagged records were
  re-tagged, and the resolver needs ≥2 RFQs in a category before history alone
  qualifies a vendor.
- **"Set up a draft for Mark"** now drafts. The item is remembered from the
  Brain's own proposal ("For 10,000ft 122mc I have…"), a draft request with no
  item asks *"What should I quote from Mark?"* instead of leaking to the Brain,
  the Brain refuses to "save a contact" without an email, and the contact the
  Brain just suggested wins over other people at the same company ("thea" →
  Pipe & Wire Quotes, not a question about 8 Theas).
- Note: only ONE Brain server should own port 8585 — `start_brain_autosync.bat`
  is the one that re-mines; don't also run START_MAINBOX.bat's Brain line.


## What's new in v0.10.0 — Voice is now an extension of MaINbox

**Show me the reference.** Every price / stock / lead-time answer now carries the
emails it was built from (the Brain returns `sources`; `reply_records.source_key`
is the Outlook EntryID and is finally threaded through `store.py` → `intents.py`
→ `/ask`). The phone shows a tappable **source card** under the answer; say
*"show me the reference"*, *"show me the email"*, or *"where did that come from"*
and you get the list again. Tapping opens `/api/mail/view?key=…` — a phone page
rendered from the **live Outlook item** on the PC (fallback: the JSON export,
then the mined excerpt) with a button to pop it open in Outlook on the desktop.

**Always saved.** The conversation (bubbles, result rows, source cards, event
cards), the RFQ you're building (job, vendors, lines, note, the line you were
typing, edit mode), the Listen transcript, the alert list and the tab you were
on are all stored on the phone and come back after a reload, a background kill
or a restart. Saved on every change (debounced) and again the instant the app is
hidden. Server-side, alerts and the voice session now survive a server restart
too (`voice_state.json`). *Settings → Clear conversation* wipes the phone copy.

**Follow-ups tab.** A live mirror of MaINbox's follow-up queue (all three lanes),
synced through MaINbox's own file bridge (`%LOCALAPPDATA%\MaINbox\bridge`,
MaINbox v4.2.99+). View, snooze (quick or custom), mark complete, edit the note,
read the linked email, cancel, and create — by form or by voice (*"follow up with
Thea about the EMT quote tomorrow at 9"*, *"what follow-ups do I have?"*,
*"what's due today?"*). Every edit is applied **inside MaINbox on its main
thread**; the phone never writes MaINbox's files. If MaINbox is closed you still
see the last synced list and your edits are queued until it next opens. A
watcher thread raises a phone alert when a follow-up comes due (and again an
hour later if still open); tapping the alert opens the tab.

Also: RFQ card actions no longer throw (`loadRecent` never existed — six call
sites), confirmed cross-reference rows keep their spec-site `url`, alerts don't
re-fire as OS notifications on every reload, polling pauses while the app is in
the background and catches up the moment it's shown, and the service-worker
cache is bumped to v13. New endpoints: `GET /api/mail/view|get`, `/api/sources`,
`/api/followups`, `/api/state`; `POST /api/mail/open`, `/api/followups/cmd`
(`op`: create|snooze|cancel|complete|note). New module: `mbb_ext.py`.

---

Voice-driven phone app for MaINbox Brain, plus a structured RFQ/procurement
layer. PWA served by a Python server on the Brain PC over Tailscale HTTPS.
`phone -> Brain HTTP(S) server -> MaINbox`; the phone never touches files or the
bridge directly.

## Version history at a glance
- **v0.1** voice cross-reference lookups + corrections, call-notes→calendar
- **v0.2** HTTPS (Tailscale cert), RFQ builder + email + Quote Coverage handoff
- **v0.3** RFQ is the entity: schema-2 append-only timeline, multi-vendor,
  preview-before-send, internal notes
- **v0.4** lifecycle events (replied/awarded/po_sent/delivered/closed) with
  manual controls on the phone; honest attention/health flags; **voice RFQ
  status queries** ("which RFQs are waiting", "what needs attention")
- **v0.5 (this)** **automatic vendor-reply matching** — forward an inbound
  vendor email and it finds the RFQ + vendor, records a `replied` event,
  refreshes coverage, and notifies the phone, no clicks. Plus two voice-query
  fixes.

---

## What's new in v0.5.0

### Automatic reply matching — the loop's missing producer
The lifecycle already *consumed* `replied` events (timeline, health, voice
queries all react to them), but nothing *produced* them automatically. Now:

`POST /api/rfq/reply  {"subject","body","from"}`

matches the email to its RFQ and vendor, appends a `replied` event, rewrites
the coverage JSON, and pushes a phone notification. The matcher lives in a
separate pure module (`rfq_match.py`) so its behavior is pinned and reusable.

**How it matches** (strongest signal first):
- RFQ ref in the subject (`RE: RFQ VR-20260707-001`)
- RFQ ref in the body tag (`[Ref VR-20260707-001 ...]`) — every outgoing RFQ
  is stamped with this, so it still matches when a forward strips the subject

**Who it attributes the reply to**:
- sender = a vendor's exact address → that vendor
- sender domain = a vendor's domain (different mailbox, same vendor) → that
  vendor, slightly lower confidence
- RFQ has exactly one vendor → that vendor
- otherwise: recorded as a real reply on the RFQ, flagged "sender didn't match
  a known vendor" (so nothing is silently mis-attributed)

It **never guesses an RFQ from the vendor alone** (a vendor can have many open
RFQs) — no ref in the mail means no match. Re-forwarding the same email is
idempotent (a per-message key prevents double-recording).

### Wiring MaINbox to it (the one remaining integration)
MaINbox owns the Outlook inbox, so the only splice needed is: in the
after-import hook, forward each new message to the endpoint.
```python
import json, urllib.request

def forward_reply_to_voice(msg, host="127.0.0.1", port=8770, token="YOUR_TOKEN"):
    body = json.dumps({"subject": msg.subject, "body": msg.body,
                       "from": msg.sender_email}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://{host}:{port}/api/rfq/reply?token={token}",
            data=body, headers={"Content-Type": "application/json"}),
            timeout=4)
    except Exception as e:
        print("reply forward failed:", e)
```
Call it from `handle_new_email_after_import`. Use `https://` + the cert
hostname if the server runs TLS. That's the whole hop — matching, timeline,
coverage, and notification happen server-side. (Filter to likely vendor mail
first if you want to avoid forwarding everything; harmless if you don't, since
mail without a ref is a no-op.)

### Two voice-query fixes
- "which RFQs are waiting **for quotes**" no longer mis-filters to a vendor
  whose address happens to start with `quotes@`. Vendor-name matching now uses
  the **domain** only ("graybar"), never the email local-part.
- "everything **Graybar hasn't answered**" now recognized as an RFQ question
  (added answered/replied/responded to the triggers).

Honest limitation: "waiting" and "hasn't answered" are evaluated at the
**RFQ level**. If one vendor on a 3-vendor RFQ replies, that RFQ leaves the
"waiting" list even though the other two haven't answered. Per-vendor "who
specifically still owes me" is a later refinement, not in this version.

---

## Setup

### Files on the Brain PC
Copy `mainbox_voice/` next to your Brain scripts so these import:
`xref_common.py`, `xref_dispatch.py`, `cross_reference.py`, `generic_xref.py`,
and now `rfq_match.py` (ships in this folder). No pip installs; pure stdlib.

### HTTPS via Tailscale (once — makes the phone mic work)
1. Tailscale admin → DNS: enable MagicDNS + HTTPS Certificates.
2. `tailscale status` → your machine name (e.g. `brainpc.tail1234.ts.net`).
3. In `mainbox_voice/`: `mkdir certs && cd certs && tailscale cert <name>`
4. `python brain_voice_server.py` — cert auto-detected, banner prints the phone
   URL. **Use the hostname URL, not the 100.x IP** (the mic only unlocks when
   the name matches the cert). Certs renew ~90 days. No certs = plain http,
   typing only (Chrome blocks the mic on insecure origins).

### Phone
Open the printed URL in Chrome (token auto-fills) → menu → Add to Home screen.
Green dot = connected.

---

## The whole RFQ flow

**Build (RFQ tab):** job → vendor emails (comma-separated, or tap recent-vendor
chips) → add lines by typing or voice ("two hundred fifty feet of THHN #12" —
fills the boxes for review, then Add) → optional note → **Preview** shows the
exact email → **Send**.

**Send:** ref `VR-YYYYMMDD-###` assigned, one email rendered, sent to each
vendor via Outlook COM if this PC has Outlook, else queued for the companion
(`outlook_rfq_sender.py`) on the Outlook PC. Per-vendor failures never block the
others. Coverage JSON written; phone notified.

**Track:** each RFQ is a schema-2 object with an append-only timeline. Recent
RFQs show rollup status + a health dot; tap one for full history, to add
internal notes, and for manual lifecycle buttons (Awarded / PO sent /
Delivered / Closed) and per-vendor "replied". Automation appends the *same*
events, so nothing needs a schema change as more of the loop is automated.

**Ask by voice** (Ask tab): "which RFQs are waiting", "what needs attention",
"everything Graybar hasn't answered", "what's the status of VR-20260707-001",
"which RFQs are awarded". Also the original lookups ("what's equal to a Topaz
100" → confirm/reject by voice) and reminders ("remind me to call Graybar
tomorrow at 9").

---

## Tested vs. untested (v0.5.0)

**Tested here — reply matching, unit + over-HTTPS end to end:**
- Subject-ref match → exact vendor; body-tag match → domain-attributed vendor;
  unknown sender → recorded + flagged unattributed; sole-vendor attribution;
  two refs in one email → two independent matches; vendor-with-no-ref → no
  match (doesn't guess); ref for an unknown RFQ → no match with reason
- Re-forwarded email → skipped as duplicate (idempotent)
- Vendor status flips to `replied`, timeline + coverage update, phone notified
- Voice-query fixes: "waiting for quotes" no longer false-filters; "Graybar
  hasn't answered" now triggers; xref/reminder/confirm routing unaffected
- TLS handshake + plain-http fallback; `reply_match: true` in ping

**Carried, still true from earlier versions:** multi-vendor create, companion
per-vendor send with partial-failure handling, preview persists nothing, notes,
manual lifecycle events, attention flags.

**NOT testable here:** live Outlook COM send; `tailscale cert` itself
(equivalent self-signed cert used — server path identical); all Android/Chrome
behavior (mic, TTS, install, notifications, wake lock).

---

## Files
```
mainbox_voice/
  brain_voice_server.py     v0.5.0 — the server (run this)
  rfq_match.py              v0.1.0 — pure reply-matching engine (imported)
  outlook_rfq_sender.py     v0.2.0 — companion for the Outlook PC
  README.md
  www/  index.html  app.js  sw.js  manifest.webmanifest  icon-192/512.png
  certs/       (you create; drop `tailscale cert` output here)
  rfq_queue/   (auto; RFQ entities + coverage/ handoff)
```

## Roadmap — finishing the loop
1. **Two MaINbox splices** (need the MaINbox file — one short session): the
   Quote Coverage ingester (register/update coverage from the handoff JSONs),
   and the reply forwarder above. After these, RFQ → sent → **auto-matched
   reply** → coverage updated → award/PO tracked is fully closed.
2. **Follow-up nudges** — "Graybar hasn't replied in 3 days: call / remind /
   snooze" (the attention flags already compute this; this adds the one-tap
   action). Reuses MaINbox's follow-up scheduler.
3. **Per-vendor waiting** — the vendor-level refinement noted above.
4. **Deterministic quote comparison** — items quoted vs missing, totals, lead
   times (MaINbox already parses reply PDFs); AI commentary only after the math
   is trustworthy.
5. Then, once data has accumulated: vendor analytics/response history, a
   dashboard/Kanban tab, AI award recommendations, and the Job-as-top-level
   object. Deliberately deferred until the loop is closed and producing
   consistent data — building them on empty tables would be premature.

Dropped deliberately: email **open tracking** (corporate filters strip pixels;
read receipts are ignorable — `replied` is the first honest vendor signal).
