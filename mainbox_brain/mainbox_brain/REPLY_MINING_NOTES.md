# Reply-mining implementation notes

This patch adds reply-mining to MaINbox Brain so answers can use vendor responses,
not just outgoing RFQs.

## New files

- `mainbox_brain/reply_miner.py`
  - Parses received vendor replies into structured facts.
  - Extracts price, unit, extended price, stock status, lead time, ETA, alternate, and no-quote signals.
  - Stores the original evidence line and confidence with every fact.

- `mainbox_brain/reply_corpus.py`
  - Loads a received-mail JSON/JSONL export or a folder of text test emails.
  - Mines replies and saves them into `mainbox.db`.

- `export_replies_outlook.py`
  - Read-only Outlook COM export for received emails.
  - Produces `received_export.json` for `reply_corpus`.
  - Supports `--vendors-db mainbox.db` so only known vendor sender emails/domains are exported.

## Database changes

`store.py` now creates and migrates this table automatically:

```sql
reply_records(
  source_key primary key,
  vendor_id,
  vendor_name,
  from_email,
  from_name,
  subject,
  received_at,
  body_excerpt,
  items json,
  facts json,
  quote_status,
  confidence
)
```

Re-running the reply export does not duplicate records because `source_key` is
upserted.

## New commands

```powershell
py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db
py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db
py -m mainbox_brain.store reply-count --db mainbox.db
py -m mainbox_brain.store reply-find "12/2 MC" --db mainbox.db
```

## Answer improvements

`/ask` and `mainbox_brain.ask` now prefer reply facts for questions like:

- `what did Cooper quote on TR26342DVSW?`
- `what was the lead time on 12/2 MC?`
- `who had 4 PVC in stock?`
- `did anyone no-quote the Okonite cable?`

If no reply fact exists, the brain falls back to outgoing RFQ history and says
clearly that it does not have a confirmed price/ETA yet.

## HTTP additions

- `GET /health` includes `reply_records`
- `GET /reply/search?q=12/2+MC`
- `GET /replies/search?q=12/2+MC`
- `POST /reply/mine` fetches Inbox replies when running the server with `--graph`
- `POST /ask` automatically uses reply facts when available

## Safety behavior

The answer layer does not invent price or lead-time information.  It shows the
vendor, date, evidence line, and confidence so the user can verify expiration,
freight terms, quantity breaks, and alternates before ordering.

Reply mining is vendor-list filtered by default. The miner only accepts messages
from exact known vendor emails or known non-public vendor domains in `mainbox.db`.
This keeps customer/internal/newsletter emails from becoming quote memory just
because they contain price-like or ETA-like language. Use `--all-senders` only
when intentionally troubleshooting.

---

## v0.13 attachment mining (PDF / Excel / CSV / Word)

The body miner reads what a vendor typed; many real quotes arrive as files.
v0.13 captures and mines them.

New: `mainbox_brain/attachment_miner.py` extracts text from each attachment and
runs it through the SAME reply fact extractor, so an attached quote yields the
same price/unit/stock/lead facts as a typed one. Tables are flattened to
`cell | cell | cell` lines; each fact's evidence is tagged with `[filename]`.

Optional dependencies, degrading per type (core brain stays stdlib-only):

    py -m pip install pypdf openpyxl python-docx

Capture + mine:

    py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files
    py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db --attachments quote_files

Behavior:
  - The exporter saves only PDF/Excel/CSV/Word attachments (skips signature
    logos/icons by extension and a size floor). Each record gains an
    `attachments` list of saved file paths.
  - Attachments are first-class: a vendor email with a generic body but a real
    attached quote still becomes a record.
  - Text-based PDFs and Excel are mined directly. SCANNED/image PDFs and image
    attachments are flagged `ocr_needed` and skipped here — that is where
    MaINbox's SmartScan / Ollama-vision OCR plugs in; this module does not
    reimplement OCR.
  - Table header rows are dropped so column labels don't become facts.

Limitations (tune against real samples): vendor PDF tables vary widely and may
extract with interleaved columns; Excel price cells without a `$` or per-unit
marker can be missed; `.doc` (old binary Word) and scans need OCR.

---

## v0.15 attachment tuning (real vendor documents)

Tuned against real files: a Topaz invoice, a Southwire order confirmation, an
ITM net price list, and a scanned L.H. Dottie packing list.

  - Per-hundred / per-thousand pricing written as "$828.01 / 100 EA" or
    "/ 1000" is now recognized (-> /c, /m). Critical: $828.01/100EA is $8.28
    each, not $828.
  - Header/column-aware extraction for structured price sheets and quote
    tables (Excel/CSV): maps columns and prefers the NET cost column.
  - Labeled part-number lines ("Material No.: 266TZ", "Customer Part No.:
    27817") now enrich the current line item instead of splitting it -- both
    the vendor part and your internal SKU become searchable.
  - Guards: phone numbers are no longer mistaken for part numbers; weights and
    order totals ("Net Weight: 55.000 LB", "Subtotal:") are no longer read as
    prices.
  - Scanned PDFs (no embedded text) are flagged ocr_needed for SmartScan.

Order confirmations and invoices are CONFIRMED prices (what was actually paid)
-- more authoritative than quotes. A net price LIST is standing catalog data,
not a dated quote; consider routing it to catalog grounding rather than the
dated reply history.

---

## v0.17 spreadsheet column alignment + address/term guards

Tuned against a customer PO (.xls) and a Gardner Bender price sheet (.xlsx).

  - Spreadsheet flattening now preserves COLUMN POSITIONS (empty cells kept as
    placeholders) and strips embedded newlines, so header columns line up with
    data columns. Before, dropping empty cells shifted columns and the wrong
    one was read as the price (e.g. a PACK QTY of 25000 read as $25000).
  - Package-style columns (BOX/JAR/BAG/CASE) are recognized as the price unit.
  - Address guard: a number followed by a street suffix ("@ 84 ST") is not a
    price. Phone numbers already excluded.
  - Stock-term guards: instructional/quoted stock language is not a status --
    'nothing is to be backordered', email "no stock" quantities. Real "out of
    stock" still registers.

Note: a sheet may have several price columns (e.g. standard vs special/effective
price); the miner takes the first true price column. Tell me which tier you want
if it should prefer another.

---

## v0.19 registering direct-buy vendors

The vendor registry is learned from SENT mail (who you RFQ). A manufacturer you
buy DIRECT but never RFQ -- e.g. Southwire, when you usually buy it through a rep
-- never gets registered, so their order confirmations/invoices are filtered out
of reply mining. Register them once:

    py -m mainbox_brain.store add-vendor southwire "Southwire" southwire.com

This recognizes all mail from that domain (or pass a specific address), takes
effect immediately, and survives re-mines. Then re-run the attachment pipeline
and their prices become searchable:

    py export_replies_outlook.py 12 received_export.json --vendors-db mainbox.db --attachments quote_files
    py -m mainbox_brain.reply_corpus received_export.json --db mainbox.db --attachments quote_files
