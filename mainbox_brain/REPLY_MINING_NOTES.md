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

---

## v0.23 purchase-order numbers

Extracts the American Power PO number (always starts with P0000, e.g.
P000020235) from vendor confirmations/invoices and email bodies, and stamps it
on each fact. A PO number means an actual order was placed -- the "what did we
pay" signal, stronger than a quote.

`reply-find` and the ask console now answer in the form:

    06-08-2026  Versabar Corp - Joanne - P000020235 - $185/c
    05-30-2026  ABB - Antonio - P000019562 - $144.24/c

(date - vendor - contact - PO# - price; stock/lead/ETA appended when present).

---

## v0.24 tighter search + quoted/exact matching

Fixes loose matching where a part number like "cond1-g" matched unrelated items
sharing a word (e.g. "SCH40 COND BE 10"), because tokenization split it into
{cond, 1-g} and the generic "cond" fragment scored a match.

  - A bare part-number-like token (has a digit, no spaces) now matches as a
    SUBSTRING against each fact's own product text -- so "cond1-g" finds COND1-G
    and nothing else.
  - Quoted phrases are exact substrings: "cond1-" matches the family (cond1-g,
    cond1-b); "cond1-g" matches just that. Works in the ask console too
    (quotes are preserved from the question).
  - Matching is now per-fact (a fact's own item + evidence line), so a query no
    longer matches every line in a multi-product email.
  - Multi-word descriptions ("1-5/8 strut") still use token overlap.

---

## v0.25 search quality: stale-data flag, tighter recall, separator tolerance

Driven by real-data research: in a 3,058-message inbox, COND1-G appears exactly
once (June 2025); HUB1-G and SS1-G appear zero times. The old junk matches were
loose-token noise; the year-old cond1-g is the only cond1-g actually mined.

  - The same exact/substring matching from v0.24 now also applies to store.find
    (RFQ recall), so a no-data query like SS1-G no longer falls back to 50%
    "saddle washer" matches. It returns a clean "I don't have it."
  - Separator-tolerant part matching: "cond1-g" / "cond1g" matches COND1-G,
    COND 1-G, COND1G (compares with separators stripped, length >= 3).
  - Stale-data flag: answers note when the best match is old
    (>= 8 months -> "worth re-quoting"; >= 4 months -> "(about N months old)"),
    so a year-old quote is never presented as if it were current.
  - Lead-time answers require an actual lead time / ETA (not just "in stock");
    if the product matched but no lead time is on record, the answer says so
    plainly instead of padding with unrelated "info" facts.

Note: recent quotes for slow-moving parts live in ATTACHMENTS and the SALES
inbox, not email bodies -- run the attachment pipeline and --mailbox to widen
coverage. This is a data-coverage gap, not a ranking bug.

---

## v0.25 separator-proof search + vendor aliases

Investigation against a real 3,071-record DB: searching "cond1-g" found only a
year-old hit because recent quotes came from attachment price sheets where the
hyphen was stripped ("COND1G") -- a literal hyphenated search missed them.

  - Exact/substring matching now ignores separators: "cond1-g" matches
    "COND1-G", "COND1G", "COND 1-G". A family query ending in a separator
    ("cond1-") requires a non-digit suffix and keeps spaces as boundaries, so
    it matches COND1-G/COND1-B but NOT "COND-100" or "2 Cond, 1/2".

  - Vendor aliases: `store add-alias <vendor_id> <alias> [alias2 ...]`. A
    dotted alias (bizzaro.com) matches the sender domain; any other (Bizzaro,
    B&B, Brindisi) matches the sender display name and search queries. So
    searching "bizzaro" returns Bandbelec, and mail from a Bizzaro name/domain
    is attributed to bandbelec on the next mine. List with `store aliases`,
    drop with `store remove-alias <alias>`.

KNOWN DATA-QUALITY ISSUE (needs the source PDFs to fix): attachment price
sheets like 000000000426*.pdf extract poorly -- part numbers lose hyphens and
double up ("COND4 COND4G"), and the per-C price column is missed (only 2 of 168
Bandbelec attachment facts captured a unit), with extended totals sometimes read
as the unit price. The column mapping must be tuned against the actual PDFs.

---

## v0.26 multi-source price answers + catalog price file + customer/job

Investigation (real catalog + 3,071-record DB): the multi-source price answer
and catalog lookup existed in the tree but were NOT in the shipped v0.25 zip
(catalog_lookup.py was missing). v0.26 ships them.

A price question now answers from three sources, newest of each:
  - Last QUOTE (no PO) -- with "for <customer>" when the quote names a
    registered customer/job, plus a detected City ST ZIP.
  - Last PURCHASE-ORDER price (fact carries a P0000 PO number).
  - PRICE FILE from the ~34,500-product catalog (net price + effective date).

Catalog is read via MAINBOX_CATALOG_DB (same var SmartScan uses); if unset the
answer just omits the price-file line. Verified: 1/2 EMT -> Southwire #81
$114.81/100 (eff 11/05/2025); liquidtight -> Southwire #6108 $858.39/100.

Customer/job registry (reliable, user-taught -- never guesses a customer):
  store add-customer "Haugland" "Bender Electric"
  store customers
  store remove-customer "Haugland"
Quotes whose subject/evidence names a registered customer show "for <name>".
Verified: a Haugland JFK quote now reads 'Last quoted for Haugland ...'.

STILL OPEN (needs the actual PDFs): attachment price-sheet extraction grabs
extended totals instead of per-line unit prices (e.g. the Haugland/JFK quote
read $51,000 instead of $85/C). The multi-source structure is correct; the
per-line price accuracy on those PDFs is not, until the column mapping is tuned
against the real documents.

---

## v0.27 update command

Single command to keep the brain current. Runs the full pipeline:
  1. Export Sent Items from Outlook COM
  2. Mine sent RFQs into the vendor registry
  3. Export vendor replies + attachments from Outlook COM
  4. Mine replies into the price/lead-time database

Config is saved in the brain database so you only set it once.

---

## v0.29 inch-mark-aware quoted search + similar-items fallback + price-increase alerts

Tested against the user's full live data (7,460 records, both mailboxes).

  - Quoted search understands inch marks: '"3/4" red emt"' is ONE phrase
    (the 4" is an inch, not a closing quote). Rule: a quote preceded by a
    digit is content unless it's the last candidate (so a plain "3/4" still
    parses). Multiword phrases also match vendor-jammed text ('3/4"EMT').
  - If an exact/quoted search finds nothing, the answer now relaxes to
    word-overlap and SAYS SO ("No exact match for ... — closest similar
    items instead:"), rather than silently showing loose matches. On the
    real db this surfaced actual 3/4" Red S.S. EMT fittings (Gumersell).
  - Price-increase alerts: when answering a price, if that vendor sent a
    price-increase notice AFTER the reported price's date, the answer warns
    ("⚠ Heads up: <vendor> sent a price-increase notice on <date> ...").
    Detection from subjects (Pricing Announcement / Price Increase /
    Adjustment / Notification). 31 real notices found in the live db.
  - store add-customer ignores flag-looking args (a stray --list had been
    registered as a customer; remove with: store remove-customer "--list").

---

## v0.30 price-statement routing + best-tier similar results

Two fixes from live testing of 'last price of "3/4 red emt"':

  - Intent routing: statements like 'last price of X' / 'price of 12/2 mc'
    were falling into the send-RFQ flow (which offered vendor contacts)
    because price questions required question phrasing. Price/lead-time
    wording now routes to an ANSWER unless a send-ish verb is used
    ('get me a quote on X', 'send rfq for X', 'quote 500 ft of X' still
    start the send flow).
  - Similar-items fallback keeps only the best-overlap tier: items matching
    ALL query words (Bridgeport '3/4 IN Red S.S. EMT Conn 231-SR') are no
    longer diluted by recent two-of-three matches ('RED WASH',
    'EMT COMPCOUPLING'). Evidence is sorted by match quality, then date.
  - 'Last quoted'/'Last purchase-order price' lines now show WHAT was
    quoted ('at $1.51 — EMT Conduit Fittings 3/4 IN Red S.S. Emt Conn.').

---

## v0.31 Outlook-style search (all words required + specificity ranking)

Live failure: 'last purchase order cost for DR20WHI?' answered with a
WATERTIGHT HUB on the PO line and DR20BLKTR in evidence. Two causes, both
fixed by studying how Outlook search behaves (all terms ANDed across fields):

  - Question/commerce words (purchase, order, cost, po, pricing, latest,
    current...) are stripped from the product, so they can't become search
    terms. 'last purchase order cost for DR20WHI?' -> product 'DR20WHI'.
  - Token search is now AND semantics: EVERY query word must be present in
    the record (item, evidence line, subject, vendor). Ranking then prefers
    facts whose OWN product text contains the words (0.6*coverage +
    0.4*specificity), so a word that only appears in the subject ('Purchase
    Order ...') can't promote an unrelated line item, and DR20BLKTR can't
    ride on dr+20 fragments.
  - The 'similar items' fallback keeps the old any-overlap behavior
    (require_all=False) and stays clearly labeled; it now also fires for
    unquoted multi-word queries that strict search can't satisfy.

Result on live data: DR20WHI answers with only DR20WHI facts ($2.32/ea PO
P000019516); unquoted '3/4 red emt' now finds the Bridgeport red fittings
directly. 'watertight hub' tightened from 30 to 22 hits (dropped partials).

---

## v0.32 conjunction/filler words no longer pollute search

Live failure: 'price and availability for 2097w' (no quotes) returned random
items, while the quoted form worked. Cause: the word 'and' leaked into the
product ('and 2097w'), making a 2-token AND search that nothing satisfied, so
the relaxed fallback matched on 'and' OR '2097w' fragments.

  - extract_product now strips conjunctions/fillers (and, or, with, please,
    give/show/find, p&a, ...). 'price and availability for 2097w' -> '2097w'.
  - _query_spec drops a _SEARCH_FILLER set from token queries, so even if a
    filler word reaches search it can't constrain results.
  - A query made up ONLY of filler now finds nothing (was: returned recent
    records). find_replies returns recent activity only for a truly blank query.

Result: 'price and availability for 2097w' now returns the SAME 3 correct
Brazill 2097W hits as the quoted form. Quoted vs unquoted agree across
2097w/dr20whi/cond1-g; multiword stays strict (all words required).

---

## v0.33 server /refresh (Outlook COM, no Graph) — phone-ready

The brain server (server.py) gains a background refresh so a thin phone client
can keep the db current without Graph:

  POST /refresh {months?, scope?, replies_only?}  -> runs the `update` pipeline
       (Outlook COM) in a background subprocess; returns 202 immediately with a
       job id. A second call while one is running is a no-op (already_running).
  GET  /refresh/status  -> {status: idle|running|done|failed, before, after,
       added, started_at_iso, finished_at_iso, log_tail, error}

  - Runs as a subprocess (COM is happier in its own process); on success the
    server reloads its cached records/vendors so /vendors and /search reflect
    new data (reply searches already read fresh per-request).
  - server --auto-refresh <minutes> keeps the db fresh on a timer, so the phone
    mostly reads already-current data and POST /refresh is just a nudge.
  - The old Graph /reply/mine now points users to /refresh.

ARCHITECTURE NOTE: refresh needs Outlook COM, so the server runs on the Windows
desktop (not tillium-bridge); it still reaches tillium-bridge for the LLM. Phone
talks to the desktop over Tailscale. No Microsoft Graph / Azure app needed.

---

## v0.34 substitute finder + online research offer

New: ask for a substitute/equivalent and the brain scans your catalog for
cross-manufacturer options; if it comes up short, it offers to research
equivalents with the LLM.

  - Intents: "substitute/alternate/equivalent/cross-ref/replacement for X",
    "what can I use instead of X" -> substitute_query. "research X" /
    "look up X online" -> research_query. ('last price of X' still routes to
    price, not substitute.)
  - answer_substitute(): catalog cross-references via catalog_lookup.substitutes
    + a "vendors who've quoted this item" section from mined replies. Offers the
    research path when there are no matches, fewer than 3, or the best match is
    weak (top score < 0.7 -- catches free-text false hits like cable STRIPPERS
    matching '12/2 MC cable').
  - research_substitute(): asks the configured LLM (gemma3) for manufacturer
    cross-references, CLEARLY labeled AI-generated / verify-before-ordering.
    NOTE: this is the model's own knowledge, not a live web search; structured
    so a real web backend can slot in later. Graceful message if the LLM is
    unreachable.
  - Console: a plain "yes" after the offer runs the research on the remembered
    item; the phone can POST "research X" directly.

catalog_lookup.substitutes hardening (tested on the 34.5k-product catalog):
  - _features now keeps single-digit trade sizes (1", 2", 3" were being dropped
    by a len>1 filter -> '1 inch ... connector' returned nothing; fixed).
  - When the query describes the item, its features win over a contradicting
    anchor ('3/4 EMT SS connector' stays set-screw even if best() resolves to a
    compression part).
  - Size must match as a token (so '1' doesn't pull '1/2'/'10'); conflicting
    connection type (compression vs set-screw) is excluded; matches below 0.4
    overlap are dropped as noise.

No regressions: prior intents + strict search unchanged on live data.

---

## v0.35 counterparty classification + cost/sell tagging (customer-side foundation)

Groundwork to mine the CUSTOMER side of the mailbox (RFQs, POs, our quotes),
not just vendor replies. The data is in email; the pipeline was just vendor-only
by construction. This adds the framework to separate the two safely.

  - classify_counterparty(store, email, name) -> vendor | customer | internal |
    unknown. Uses the vendor registry (vendor wins) and the customer/job
    registry (v0.26); our own domain = internal; everything else = unknown.
  - ReplyFact gains `direction`: cost (we pay a vendor) | sell (we quote a
    customer) | "". Every mined fact is tagged from the counterparty.
  - VendorReplyRecord gains `counterparty_type`; persisted in a new
    reply_records.counterparty_type column (auto-migrates existing dbs).
  - parse_vendor_reply: skips our own internal mail; vendor_only=True still
    keeps ONLY vendors (production path unchanged); vendor_only=False now also
    keeps customer/unknown senders so the customer side can be mined.
  - find_replies(direction='cost'|'sell') filters by price side. LENIENT on
    legacy data: facts with no tag always pass, so older records are never
    hidden; once re-mined, cost/sell separation is exact.

VALIDATION: vendor path is byte-for-byte unchanged -- 1975 priced facts / 2909
records on the real inbox, all now tagged direction=cost; classifier verified on
the live registry (real vendors->vendor, Verde/Haugland->customer,
stranger->unknown). Migration + existing answers (DR20WHI) confirmed on a copy
of the live db.

STILL NEEDED (honest): customer-side EXTRACTION is untested -- no customer email
to validate against yet. The classifier identifies customers and tags facts, but
whether _extract_facts pulls good prices/items from real customer RFQ/PO formats
is unknown. Next step needs an UNFILTERED sales-mailbox export to mine, validate,
and tune, then wire a customer pass into the `update` pipeline.

---

## v0.36 customer classification hardened on real sales-mailbox data

Validated the v0.35 framework against Steve's real sales inbox (8,324 emails,
3 months) and fixed what the real data exposed:

  - Customer-registry matching is now separator-insensitive: 'Bender Electric'
    matches benderelectric.com (was missing -> classified unknown). Domain match
    is a prefix test and name match is a prefix test, so short keys like 'tore'
    no longer false-match 'store.com'.
  - classify_counterparty(unknown_default=...) + mine_replies/parse_vendor_reply
    customer_default flag: the SALES-mailbox pass treats otherwise-unrecognized
    senders as customers (that inbox's non-vendor, non-internal mail is
    overwhelmingly contractors). On real data this moves the inbox to
    55% customer / 29% vendor / 16% internal (was 51% 'unknown').
  - Internal (own-domain) check ordered first; vendor registry still wins for
    everyone else.

Vendor canary unchanged (2909 records / 1975 priced).

FINDINGS for the customer-side build (validated against real attachments):
  - Customer RFQ + PO data is in ATTACHMENTS, not bodies (bodies are cover
    notes / clarifying Q&A). Outbound quote bodies carry a price only ~1% of
    the time -- sell prices live in attached quote PDFs.
  - Customer PO PDFs DO carry sell prices + items + quote refs (e.g. Bender PO
    W21414: 192 ENP4250-NA @ $92.39, ref quote S100098480; Kojo PO: structured
    QTY/UOM/DESC/UNIT/EXT). So margin is computable from inbound POs (sell) +
    the vendor side we already mine (cost) -- no sent attachments needed.
  - A line-item parser using "prices carry decimals" + qty*unit~=ext extracts
    the UNIT price correctly across 3 real PO formats (Kojo/Bender/Forest).
    Descriptions + recall need per-format refinement (177 POs, 136 takeoffs, no
    single dominant format) -- the same heterogeneous-format challenge as the
    vendor price sheets. Document-level capture (PO#/customer/quote#/total/job)
    is robust now.

---

## v0.37 customer-document miner (the sell side) — validated on real data

The customer side is now mineable end to end. Built + validated against Steve's
real sales mailbox (8,324 emails) and customer PO attachments.

NEW mainbox_brain/customer_docs.py:
  - parse_line_items(text): extracts qty / description / unit price from customer
    POs/quotes. Prices carry decimals (part numbers/item codes are integers) and
    qty*unit~=extended, which picks the UNIT (sell) price -- not the extended
    total -- across the varied formats (Kojo / Bender / Forest / ...).
  - extract_header(text, subject): PO#, quote#, total, job. PO# requires a digit
    (rejects false matches like "ORDERED"/"POWER") and the subject is tried
    first (cleanest). Robust regardless of line-item layout.
  - mine_customer_document(path, subject) -> (header, sell-tagged facts, note),
    gated by looks_like_customer_doc so random specs/certs are skipped.
  - mine_customer_records(records, store, attachments_dir): classifies each
    sales email; for customers, mines attachments into a sell-tagged
    VendorReplyRecord (counterparty_type='customer'). Body is a cover note, so
    it's NOT mined (avoids job-name/boilerplate noise). On a 63-email batch:
    46 records, 194 sell-priced line items, 18 customers.

NEW mainbox_brain/mine_customers.py (CLI):
    py -m mainbox_brain.mine_customers sales_inbox.json sales_files --db mainbox.db
  Adds the customer/sell side to the db; cost (vendor) data untouched.

Query + answer:
  - store.customer_orders(customer, product): customer purchase records (sell).
  - store.find_replies(direction='sell'|'cost') already separates the two; sell
    queries resolve (ENP4250 -> Bender $92.39 sell; cost=0/sell=1).
  - intents: "what did <customer> order/buy", "<customer> purchase history",
    "what did we sell <customer>" -> answer_customer_orders (lists POs with line
    items + sell prices). Guarded so first-person ("what did WE buy from X")
    stays a vendor/cost question.

Margin is now possible (sell from customer PO + cost from vendor side) -- not yet
wired into an answer; that's the next step.

Vendor canary unchanged (2909 / 1975). Known rough edge: line-item descriptions
on some PO formats include a trailing job-id row or clip a leading part-number
digit -- per-format refinement, same as the vendor price sheets.

---

## v0.38 "we"/"I" defaults to the user; clarify only on a real name collision

Refinement to customer-order routing (per Steve):

  - "what did WE order" / "what did I buy" defaults to the USER's own (American
    Power) purchasing -- a vendor/cost question -- not a customer lookup. The
    screenshot case "what did we last pay for 12 stranded wire" stays a cost
    answer.
  - EXCEPTION: if a registered customer's name actually BEGINS with that word
    (e.g. a customer literally named "We Are Good Electric"), the brain asks
    which was meant before answering, rather than guessing. The collision test
    matches the customer's FIRST WORD, so "Welsbach" does NOT collide with "we".
  - classify(text, llm, store=None) now optionally takes the store to check the
    customer registry for that collision; ask.py and server.py pass it. Without
    a store, behavior is unchanged (back-compatible).

Vendor canary unchanged (2909 / 1975).

---

## v0.39 cost/sell separation in answers, PO relevance, fuzzy customer matching

Fixes from real-use screenshots:

  - COST answers no longer list CUSTOMERS. "what did we pay/quoted" excludes
    sell-side (customer) records (direction='sell'); a customer's order price is
    not what we paid. (This was the "it listed customers" bug.)
  - PO line can't drift to a different product than the quote. "12 str" matched
    both '#12 Str CU THHN' (wire, /mft) and a 'FLEX STR CONN' (connector, /c) --
    same words. Quote/PO are now chosen by match quality then recency, and if
    the best quote and best PO share no DISTINCTIVE words (beyond the query),
    the PO line is dropped as a different item. dr20whi still shows both.
  - "we"/"I" + time words: "what did we LAST pay" now correctly reads as the
    user (cost), not a customer named "we last" -- filler words (last, recently,
    just, ...) are stripped from the extracted customer.

Customer-scoped questions + fuzzy names:
  - "what did <customer> pay/buy/order for <product>" routes to the customer
    (sell) side scoped to that customer + product. ("pay/paid" added; the
    first-person guard keeps "what did WE pay" a cost question.)
  - Customer names resolve fuzzily: "allan brightway" -> "Allan Briteway", with
    company-suffix tolerance ('Utility'/'Electric' ignored) and typo tolerance
    (difflib). The answer notes the match: 'Orders from X (matched "...")'.
  - If several DIFFERENT customers could match, the brain asks which (same
    company across multiple domains collapses to one, so it doesn't over-ask).
  - Customer records are now keyed by the company from the email domain (not the
    individual sender's name), so they're searchable by company. RE-MINE the
    sales inbox to pick this up.

Note: 'tpz 533' was a token-fragment false match (the words 'tpz' and '533'
appeared separately in an unrelated record); the relevance + AND-search work
reduces these, but a re-mine with cleaner part extraction helps most.

Vendor canary unchanged (2909 / 1975).

---

## v0.40 search ranking (recency) + two-panel wire price-sheet extraction

Two fixes, driven by "12 str" not surfacing the recent good Thea quote.

1) RANKING regression (from v0.39) fixed. "what did we LAST pay/quote" now
   ranks by RECENCY among near-top matches, not by score alone. v0.39 picked the
   highest-scoring line, so a clean older quote ('#12 Str CU THHN', 4/24) buried
   a more recent one (Thea 6/2) that scored a hair lower because its row had
   extra tokens. Headline = most recent within 0.25 of the top score; evidence
   is newest-first. The v0.39 distinctive-word PO-drop and cost/sell filter are
   kept (so '12 str' still drops the FLEX STR CONN connector PO).

2) ROOT CAUSE of bad '12 str' results: the Thea/Colonial "WIRE SHEETS" are
   TWO-PANEL price sheets (THHN/THWN on the left, Romex/UF on the right). Each
   text line packs a LEFT product and a RIGHT product:
       12 THHN STR 24 $200.86    10/2 ROMEX REELS 130 $898.32
   The old generic parser mashed both into one line, read '$10' (from '10/2') as
   the price, and let a stray '12' (from the Romex '12/3' or a weight) match a
   '12 str' search -- pulling 3 AWG and 16 TFFN rows by accident.
   NEW attachment_miner.looks_like_wire_sheet + _wire_sheet_facts: detects the
   sheet by its 'LBS/M' header and splits each line at its '$ NET' decimals
   (weights are integers, so the decimals are unambiguously the prices),
   recovering each product with its correct per-M net price. Each fact's source
   line is just that product (+price), so a Romex fact can't match a THHN search
   and the incidental weight can't match either.
   Result on the real sheets: '12 str' -> ONLY '12 THHN STR' at $200.86/M (was
   '$10' + 3 false rows); Colonial sheet -> '12 THHN STR' $183.55/M. 60 clean
   priced products per sheet. No new dependency (works on the existing text).

   ACTION: re-mine the vendor side (`update`) to apply the wire-sheet parser to
   stored Thea/Colonial records. The ranking fix is query-time (immediate).

Vendor canary unchanged (2909 / 1975).

---

## v0.41 wire/pipe material distinction + customer grouping by company (domain)

Issue 1 -- same size, different material (wire gauge 12 can be stranded/solid/
copper/aluminum; pipe sizes repeat across EMT/PVC/RGD):

  - Synonym normalization in search (store._norm_tokens): stranded=str, solid=
    sol, copper=cu, aluminum=al. So "12 thhn solid" and "12 thhn sol" match the
    same fact, and solid stays distinct from stranded. Search canary unchanged.
  - Section-aware price-sheet parser (attachment_miner): the Eagle/Rowe-style
    sheets put the type in SECTION HEADERS ("THHN/THWN-2 STRANDED BUILDING WIRE"
    / "...SOLID...") with rows that are just "12 $0.1846 $184.60". The old parser
    lost that context, so a '12' under STRANDED and a '12' under SOLID were
    indistinguishable (and it headlined the wrong gauge). New _wire_table_facts
    propagates the section into each product name -> "12 AWG THHN THWN-2
    STRANDED" ($184.60/MFT) vs "12 AWG THHN THWN-2 SOLID" ($173.30/MFT). Uses
    pdfplumber (optional) for layout-ordered text so headers sit above their
    rows; falls back cleanly if pdfplumber isn't installed.
  - Two formats now handled: two-panel THHN|Romex (v0.40) and sectioned STR/SOL
    price lists (v0.41).
  - Result: "12 thhn solid" -> $173.30/MFT SOLID; "12 thhn stranded"/"12 thhn
    str" -> $184.60/MFT STRANDED.

Issue 2 -- customer abbreviations / one company many people:
  - Customer records are grouped by EMAIL DOMAIN. Every person at ej1899.com is
    the single customer 'EJ', so typing "EJ" finds the company and pulls ALL its
    orders -- not a disambiguation list of employees. _resolve_customer now
    builds one entry per company (domain core), matches the query against the
    domain + names, and disambiguates by COMPANY only when genuinely different
    companies match (e.g. two different 'Allan' firms).

ACTIONS: pip install pdfplumber  (for sectioned wire sheets);  re-mine the
vendor side (`update`) to reparse wire price sheets; re-mine customers
(`mine_customers`) so records carry the company-from-domain identity.

Vendor canary unchanged (2909 / 1975).

---

## v0.42 conversational sessions (back-and-forth for the voice/phone app)

NEW intents.InfoSession: a stateful front-end around the question-answering
intents so the brain holds a conversation instead of treating every turn as
isolated.  Per turn, call session.answer(text) -> reply (or None to fall through
to the quote pipeline).  It tracks:

  - PENDING CLARIFICATION: when the brain asks "Which product should I check the
    price for?", the next plain answer ("red emt") resumes the original request
    rather than starting a new search. (This was the broken case.)
  - FOLLOW-UPS via remembered context: after "what did we pay for dr20whi",
    "what's the lead time?" / "what's the price?" reuse dr20whi (pronoun/empty
    products are filled from the last item discussed).
  - CUSTOMER DISAMBIGUATION resume: "what did allan pay for X" -> "which Allan?"
    -> "briteway" resolves and answers.
  - TOPIC CHANGE: a clearly new question mid-clarification ("who do we buy X
    from") abandons the pending one and answers the new query.
  - RESEARCH "yes": a plain yes after a substitute-research offer runs it.

Wired into ask.py (console: one session for the REPL) and server.py (/ask now
returns a `session` id and `pending` flag; phone clients pass the id back each
turn to keep context; sessions expire on the same TTL as quote sessions).

Vendor canary unchanged (2909 / 1975).

---

## v0.43 learning layer (knowledge accumulation with provenance)

NEW knowledge.py + InfoSession integration. The brain accumulates knowledge so
it answers more questions over time -- but every learned item carries a SOURCE
and DATE and is labeled (taught / researched), and NOTHING overrides mined
vendor data. A procurement decision is never made on an unverified figure; the
learned answer appears AFTER the honest "I don't have confirmed data".

Three kinds of learned knowledge (in mainbox.db):
  - aliases (vocabulary you teach): "'gal' means galvanized" -> merged into the
    search synonym map so future questions match your shorthand. Applies live.
  - facts (taught + cached research): "remember that Bandbelec lead time is ~3
    weeks" -> stored by topic, surfaced when the data is silent on that topic.
    Research results (LLM cross-refs) are cached and reused/dated.
  - gaps: questions the brain couldn't answer are logged so you can see what to
    add next (store gaps).

Teaching is natural language (works in console + phone /ask via InfoSession):
  - "<x> means <y>" / "<x> stands for <y>" / "we call <y> <x>" -> alias
  - "remember that ..." / "note that ..." / "fyi ..."          -> fact
  - "forget <x>" / "forget that <x>"                            -> remove

Answer flow now: data -> catalog -> learned fact -> offer to research. When the
brain has no answer it logs the gap and (for product questions, if the LLM is
reachable) offers "research <x> online? (yes)".

Review/manage: store learned | learned-facts | learned-aliases | gaps |
forget "<x>"  (all accept --db).

HONEST SCOPE: "online research" is still the LLM's own knowledge (gemma3),
cached -- NOT live web browsing. A real web-search backend can plug into
_research()/research_substitute() later; the caching + provenance plumbing is
already there. Learning is human-in-the-loop by design (you teach/correct;
the brain doesn't silently rewrite its own facts), which is the safe model for
procurement.

Vendor canary unchanged (2909 / 1975); search + _norm_tokens now instance-based
to fold in learned aliases.
