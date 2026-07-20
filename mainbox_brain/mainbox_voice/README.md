# MaINbox Voice — v0.5.0

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
