# MaINbox Brain — Command Reference (v0.19)

## How to run these

Run everything from the **project root** — the folder that contains the
`mainbox_brain` subfolder (i.e. `...\CHATGPT MB BRAIN\mainbox_brain`).

- Commands written `py -m mainbox_brain.something` are **modules** — always use
  the `-m` form and the dotted name. (`py mainbox_brain\something.py` will fail.)
- Commands written `py export_*.py` are **scripts** run directly.
- `--db mainbox.db` points at your database. If you always use the same file in
  the same folder you can usually leave it off, but it's safest to include it.

One-time install for attachment mining:

```powershell
py -m pip install pypdf openpyxl python-docx xlrd==1.2.0
```

(`xlrd==1.2.0` is only needed for legacy `.xls` files.)

---

## 1. First-time setup (do once)

```powershell
:: A) export your sent mail from Outlook (last 12 months)
py export_sent_outlook.py 12 sent_export.json

:: B) learn the vendor registry from who you RFQ
py -m mainbox_brain.store exclude highliteelectrical ipjs ej1899 berson5287@gmail.com
py -m mainbox_brain.corpus sent_export.json --db mainbox.db
py -m mainbox_brain.curated apply --db mainbox.db
```

`exclude` drops yourself / non-vendors. `corpus` builds the registry.
`curated apply` layers in researched vendor names + categories.

---

## 2. Daily use — ask the brain

```powershell
py -m mainbox_brain.ask --db mainbox.db
```

Add `--llm` to route natural-language questions through your local model
(gemma3 on tillium-bridge), falling back to plain parsing if it's unreachable:

```powershell
py -m mainbox_brain.ask --db mainbox.db --llm
```

On startup it prints which tier is active, e.g.
`(LLM router active: gemma3:27b@http://tillium-bridge:11434)`.

**Check the LLM connection** without entering the console (tests each tier):

```powershell
py -m mainbox_brain.llm
```

It probes each tier in two phases: a fast reachability check (`/api/tags`, lists
installed models) and a timed generation test. This distinguishes "host
unreachable" from "host up but model slow/missing." Output shows whether
`tillium-bridge` (primary) and `localhost` (fallback) are reachable, which models
each has installed, and which tier `ask --llm` would use.

Defaults: primary `gemma3:27b @ http://tillium-bridge:11434`, fallback
`gemma3:27b @ http://localhost:11434`. Timeouts: 4s to connect, **45s to
generate** (a 27B model cold-loading into VRAM on its first request is slow;
warm calls return in a second or two). Override with:
- `MAINBOX_TRIAGE_HOST` / `MAINBOX_TRIAGE_MODEL` — primary host/model
- `MAINBOX_LOCAL_OLLAMA` / `MAINBOX_LOCAL_MODEL` — fallback host/model
- `MAINBOX_LLM_TIMEOUT` (generate) / `MAINBOX_LLM_CONNECT_TIMEOUT` (connect)

> If the browser shows "Ollama is running" but the check times out, the server
> is fine — the model just needs longer to load. Raise `MAINBOX_LLM_TIMEOUT`.
> If the fallback reports the model "NOT installed here," that local machine
> doesn't have it; either `ollama pull` it or point `MAINBOX_LOCAL_MODEL` at a
> model it does have. The primary tier (tillium-bridge) is what matters.

Inside the console you can type things like:
- `price and availability for 2,000ft of 12/2 MC`
- `who do I usually ask for strut`
- `what did we last pay for southwire 2" watertight hubs`
- `yes` / `just markh` / `Stephanie at Warshaw` / `no` to confirm or drop a send
- `quit` to exit

---

## 3. Reply & attachment mining (the price/quote data)

This is the two-step pipeline that fills the brain with actual vendor prices,
lead times, stock, and attachment quotes.

```powershell
:: A) export received mail AND save quote attachments
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files

:: B) mine bodies + attachments into the database
py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db --attachments quote_files
```

Step A saves PDF/Excel/CSV/Word attachments into `quote_files\` and records which
email each came from. Step B extracts price/stock/lead facts from both the email
bodies and those files.

Useful flags on the exporter (`export_replies_outlook.py`):
- `--attachments quote_files`  save quote files into this folder
- `--min-attachment-kb 8`      skip tiny images (signature logos); default 8
- `--keep-images`              also save image attachments (for OCR later)
- `--body-limit N`             max characters of body to keep
- `--vendors-db mainbox.db`    use the registry to filter to vendor mail

Useful flags on `reply_corpus`:
- `--attachments quote_files`  also mine the saved attachment files
- `--all-senders`              mine ALL senders, not just known vendors
  (troubleshooting / one-time broad mining only)

> Note: attachments are only mined from **known-vendor** senders. A scanned PDF
> with no text layer is flagged `needs-OCR` and skipped (that's the SmartScan /
> Ollama-vision path).

---

## 4. Vendor list maintenance (corrections — all permanent, survive re-mines)

```powershell
:: drop a vendor (key = vendor_id OR a contact email)
py -m mainbox_brain.store exclude <vendor_id_or_email> --db mainbox.db

:: fix a display name
py -m mainbox_brain.store rename theaenterprises "Thea Enterprises" --db mainbox.db

:: register a vendor you buy DIRECT but never RFQ (e.g. Southwire via a rep)
py -m mainbox_brain.store add-vendor southwire "Southwire" southwire.com --db mainbox.db
::   - 3rd arg can be a domain (recognizes all mail from it) or a specific email
```

After `add-vendor`, re-run the reply pipeline (section 3) so that vendor's
confirmations/invoices get mined.

---

## 5. Inspect / search the brain

```powershell
:: show the learned vendor registry
py -m mainbox_brain.store vendors --db mainbox.db

:: show your recorded corrections
py -m mainbox_brain.store list --db mainbox.db

:: recall which vendors you RFQ'd a product to (from sent mail)
py -m mainbox_brain.store find 8400 connector --db mainbox.db

:: search mined vendor replies for prices / ETAs / stock / PO numbers
py -m mainbox_brain.store reply-find 12/2 MC --db mainbox.db
py -m mainbox_brain.store reply-find 1-5/8 strut --db mainbox.db
::   results read:  06-08-2026  Versabar Corp - Joanne - P000020235 - $185/c
::
:: exact / family matching:
::   reply-find cond1-g        part numbers match exactly (not "COND BE 10")
::   ask console: ...for "cond1-"   quoted -> family (cond1-g, cond1-b, ...)
::   ask console: ...for "cond1-g"  quoted -> just that part

:: count how many vendor replies are mined
py -m mainbox_brain.store reply-count --db mainbox.db
```

---

## 6. Test / preview attachment extraction (does NOT save to the brain)

Point it at a single file or a whole folder to see what would be extracted.
Good for checking a new vendor's quote format before committing.

```powershell
py -m mainbox_brain.attachment_miner quote.pdf
py -m mainbox_brain.attachment_miner quote_files\
py -m mainbox_brain.attachment_miner pricesheet.xlsx --limit 30
```

- `--subject "..."`  give a subject line as fallback product context
- `--limit N`        max facts shown per file (default 20)

> This is preview only. A loose file has no sender, so it can't be attributed to
> a vendor — saving into the brain happens via the pipeline in section 3.

---

## 7. Outlook exporters (Windows, run on your machine)

```powershell
:: sent mail  ->  [months] [output file]   (defaults: 12, sent_export.json)
py export_sent_outlook.py 12 sent_export.json

:: received mail + attachments
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files
```

---

## 7b. Reading a SHARED mailbox (e.g. the sales inbox)

Both exporters can target a shared mailbox instead of your personal one. You
must have access to it in Outlook (delegate or full-access permission).

**Method A — `--mailbox` (recommended).** Uses Outlook's GetSharedDefaultFolder;
works even if the mailbox isn't shown in your folder list, as long as you have
access. The exporter reads that mailbox's Sent (sent exporter) or Inbox
(replies exporter).

```powershell
:: shared SENT items  ->  build the team-wide vendor registry
py export_sent_outlook.py 12 sent_export.json --mailbox sales@americanpoweresc.com
py -m mainbox_brain.corpus sent_export.json --db mainbox.db
py -m mainbox_brain.curated apply --db mainbox.db

:: shared INBOX + attachments  ->  mine team-wide prices/quotes
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files --mailbox sales@americanpoweresc.com
py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db --attachments quote_files
```

**Method B — `--folder` (replies exporter only).** Walks the Outlook folder
tree by name. Use this if the shared mailbox already appears in your folder list.
The path is backslash-separated, mailbox name first:

```powershell
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files --folder "Sales Mailbox\Inbox"
```

You can also combine `--mailbox` with `--folder` to read a **subfolder** of the
shared Inbox (e.g. a "Quotes" subfolder):

```powershell
py export_replies_outlook.py 12 received_export.json --mailbox sales@americanpoweresc.com --folder "Quotes" --vendors-db mainbox.db --attachments quote_files
```

---

## Quick cheat sheet

| Task | Command |
|---|---|
| Ask the brain | `py -m mainbox_brain.ask --db mainbox.db` |
| Ask, with local LLM | `py -m mainbox_brain.ask --db mainbox.db --llm` |
| Export sent mail | `py export_sent_outlook.py 12 sent_export.json` |
| Build registry | `py -m mainbox_brain.corpus sent_export.json --db mainbox.db` |
| Apply curated names | `py -m mainbox_brain.curated apply --db mainbox.db` |
| Export replies + files | `py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files` |
| Mine replies + files | `py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db --attachments quote_files` |
| Preview a file/folder | `py -m mainbox_brain.attachment_miner quote_files\` |
| Show vendors | `py -m mainbox_brain.store vendors --db mainbox.db` |
| Search reply prices | `py -m mainbox_brain.store reply-find <product> --db mainbox.db` |
| Drop a vendor | `py -m mainbox_brain.store exclude <id_or_email> --db mainbox.db` |
| Rename a vendor | `py -m mainbox_brain.store rename <id> "<Name>" --db mainbox.db` |
| Add a direct-buy vendor | `py -m mainbox_brain.store add-vendor <id> "<Name>" <domain> --db mainbox.db` |

---

## Typical end-to-end refresh

```powershell
:: 1. refresh the registry from sent mail
py export_sent_outlook.py 12 sent_export.json
py -m mainbox_brain.corpus sent_export.json --db mainbox.db
py -m mainbox_brain.curated apply --db mainbox.db

:: 2. (once) register any direct-buy manufacturers
py -m mainbox_brain.store add-vendor southwire "Southwire" southwire.com --db mainbox.db

:: 3. refresh prices from received mail + attachments
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files
py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db --attachments quote_files

:: 4. use it
py -m mainbox_brain.ask --db mainbox.db
```
