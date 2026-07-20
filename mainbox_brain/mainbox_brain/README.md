# MaINbox Brain — v0.1

The **headless core** of the assistant we scoped: a conversational quote-routing
engine. It parses a natural-language request, resolves which vendors to send it
to (from your line card + your sourcing history), runs the confirm-and-send
flow, and drafts/sends the RFQ.

This is deliberately **the trunk, not the whole tree**. It's built to sit
behind an HTTP layer that an Android (or any) client calls. The call/VoIP layer
and the wake-word layer bolt onto this later — none of them work without this
piece first, which is why it's v0.1.

```
  Android client  ──HTTP──►  ┌─────────────────────────────┐
  (thin: UI,                 │        MaINbox Brain         │
   notifications,            │  parse → resolve → confirm   │
   wake word,                │     → draft/send RFQ         │
   calendar)                 └──────────┬──────────────────┘
                                        │
            ┌───────────────────────────┼───────────────────────────┐
            ▼                           ▼                           ▼
     Microsoft Graph              gemma3 @ tillium-bridge      line card + vendors
     (mail + calendar)            (LLM, optional)              (catalog + sourcing)
```

## Run it now (no credentials, no GPU host)

```bash
python -m mainbox_brain.demo                # scripted walkthrough
python -m mainbox_brain.demo --interactive  # type your own requests
python -m mainbox_brain.demo --no-llm       # skip LLM probing entirely
```

Stdlib only. If `tillium-bridge` or a local Ollama are reachable they're used;
otherwise the regex paths run and the flow still completes. That degradation
contract is the same one MaINbox lives by.

## What's real vs. stub vs. integration point

| Piece | Status | File |
|---|---|---|
| Request parsing (regex-first, LLM-optional) | **real** | `parser.py` |
| Manufacturer catalog (your line card) | **real, seeded** | `catalog.py` |
| Vendor → manufacturer → contact resolver + scoring | **real** | `resolver.py` |
| Confirm flow state machine (yes / subset / +extras / now-or-draft) | **real** | `conversation.py` |
| RFQ email drafting | **real** | `rfq.py` |
| Meeting + follow-up intent detection (on text) | **real** | `intent.py` |
| LLM client (gemma3 → local → none) | **real** | `llm.py` |
| Sent-history "learn where I procure" signal | **stubbed** (canned data) | `graph_client.py` |
| Send / draft / calendar | **stubbed** (prints) | `graph_client.py` |
| Microsoft Graph live calls | **integration point** (skeleton + notes) | `graph_client.py` |

## The one data distinction that matters

- **`catalog.py`** = brands American Power *sells* (from the line card). This is
  the **manufacturer** layer. Seeded for you.
- **`vendors.py`** = suppliers you *buy from* and their contacts (Mark@Brazil,
  Thea@PipeAndWire …). The line card does **not** contain this. **You own and
  maintain this file.** The example entries are placeholders — swap in real
  suppliers, real `lines`, real emails.

The resolver bridges them: spec → category → manufacturers (catalog) → suppliers
carrying those lines (vendors) → contact. It additionally boosts any supplier
you've actually quoted that category before (the Sent-Items signal).

## Making it live

**1. Microsoft Graph — Tier 3 setup (exact steps).**

   a. `pip install msal` (the only dependency).
   b. Register the app — https://entra.microsoft.com → Applications → App
      registrations → **New registration**:
      - Name: `MaINbox Brain`
      - Supported account types: *Accounts in this organizational directory only*
        (or *Any organizational directory* if you'll test with multiple tenants)
      - Redirect URI: leave blank. Register.
   c. On the app's **Overview** page, copy the **Application (client) ID**.
   d. **Authentication** → scroll to *Advanced settings* →
      **Allow public client flows** → **Yes** → Save. (Required for device-code login.)
   e. **API permissions** → Add a permission → Microsoft Graph → *Delegated* →
      add `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `User.Read`.
      If a tenant admin is required to consent, hit *Grant admin consent*.
   f. Point the brain at it and run:
      ```powershell
      $env:MAINBOX_GRAPH_CLIENT_ID="<your client id>"
      py -m mainbox_brain.demo_graph            # login + mine, read-only
      py -m mainbox_brain.demo_graph --ask      # then run live quote requests
      ```
      First run prints a code + `microsoft.com/devicelogin`; sign in once and the
      token caches at `~/.mainbox_graph_token.json` (refreshes silently after).
      If your org locks the tenant down, set
      `MAINBOX_GRAPH_AUTHORITY=https://login.microsoftonline.com/<tenant-id>`.

   Safety defaults in `demo_graph.py`: mining is read-only, and "send now" is
   downgraded to creating a **draft** until you flip `ALLOW_REAL_SEND = True`.

**2. LLM.** Already wired for Ollama. Defaults point at `tillium-bridge:11434`
(gemma3:27b) then `localhost:11434`. Override with env vars in `config.py`. Pass
an `LLMClient` into `parse_request(...)` / `detect_meeting(...)` /
`mine(..., llm=...)` to upgrade the fuzzy cases; leave it out and the
deterministic paths run.

**3. Catalog at scale.** The seed covers the wire/cable/conduit/fittings/gear
lines in detail and a lighting sample. To cover everything, bulk-load
`catalog.MANUFACTURERS` from your existing ~34,500-product SQLite DB — it already
carries per-SKU vendor provenance, a stronger signal than brand tags.

## Roadmap (the order we agreed on)

1. **This brain + Graph** — ship the email/calendar assistant. Lots of
   "are you free Wednesday?" already arrives as text, so the intent layer pays
   off immediately with zero telephony.
2. **Thin Android client** — Kotlin (or Flutter). Foreground "Hey MaINbox" wake
   word is the easy first tier; background listening = a foreground service with
   a persistent notification; "anytime" can't cold-start itself, so re-arm on a
   tap and/or register App Actions so "Hey Google, ask MaINbox…" routes in.
3. **VoIP / telephony layer** — the bigger lift, and the single piece that
   unlocks *both* the live call-assistant (consent-announced, server-side
   transcription — sidesteps Android's call-audio block and PA two-party
   consent) *and* the vendor-quote phone app. Same infra, two payoffs.

## Layout

```
mainbox_brain/
  config.py        hosts, models, identity
  models.py        dataclasses (LineItem, Vendor, Contact, …)
  catalog.py       line-card manufacturers + category keyword map
  vendors.py       YOUR suppliers + contacts (placeholders to replace)
  llm.py           Ollama client, 3-tier fallback
  parser.py        request text → LineItems
  resolver.py      items → ranked vendors with contacts + reasons
  rfq.py           RFQ email drafter
  intent.py        meeting / follow-up detection on text
  conversation.py  confirm-and-send state machine
  graph_client.py  mail/calendar: interface + stub + Graph skeleton
  demo.py          end-to-end CLI
```


## v0.8 reply-mining upgrade

The brain now has a second memory stream: **vendor replies**.  Sent-history tells
it who you asked; reply-mining tells it what vendors actually answered with.
That unlocks questions like:

```text
what did Cooper quote on TR26342DVSW?
who had 12/2 MC in stock?
what was the lead time on 4" PVC?
did anyone no-quote the Okonite cable?
```

Desktop export + mine flow:

```powershell
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db
py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db
py -m mainbox_brain.ask --db mainbox.db
```

By default, reply-mining is now **vendor-list filtered**. The exporter can use
`--vendors-db mainbox.db` to only export messages from known vendor emails/domains.
The miner also runs in vendor-only mode by default, so unrelated customer/internal
emails are skipped even if they contain a dollar amount or ETA-like wording. Use
`--all-senders` only for troubleshooting or a one-time broad test.

The reply miner stores conservative evidence facts in SQLite instead of guessing:
price, unit, extended price, stock status, ETA/lead time, alternate, and no-quote
signals.  Answers now prefer mined vendor replies for price/lead-time questions
and fall back to outgoing RFQ history only when no reply facts exist.

Useful checks:

```powershell
py -m mainbox_brain.store reply-count --db mainbox.db
py -m mainbox_brain.store reply-find "12/2 MC" --db mainbox.db
```

HTTP additions:

```text
GET /health                       includes reply_records
GET /reply/search?q=12/2+MC        searches mined reply facts
POST /reply/mine                  Graph mode: fetch Inbox replies and persist facts
POST /ask {"text":"what price did Cooper quote TR26342DVSW?"}
```

Safety note: quote facts are only as exact as the vendor email text.  The answer
layer shows the evidence line and confidence so you can sanity-check expiration,
freight, quantity breaks, and substitutions before ordering.
