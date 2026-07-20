# MaINbox Cross-Reference Toolkit

A local-first toolkit for looking up part-number cross-references (competitor
part → equivalent house-brand part) for electrical distribution. It combines a
correctable local knowledge store with the ability to **teach** the tool how to
read any vendor's public cross-reference web tool by clicking through it once —
no per-vendor code required.

## What's in it

- **`cross_reference.py`** — a local, correctable SQLite store of part
  equivalences. You confirm the ones that are right and reject the ones that are
  wrong; your corrections become permanent and authoritative.
- **`site_teacher.py`** — teach the tool a vendor's cross-reference page by
  doing one search and clicking the columns. Saves a reusable JSON "recipe."
- **`generic_xref.py`** — replays a taught recipe to look up any part on that
  vendor's tool.
- **`xref_dispatch.py`** — the single entry point: checks your local store
  first, then any taught vendor sites, and merges the results.

## Quick start

```bash
# one-time: install the browser engine the teacher/driver use
pip install playwright
playwright install chromium

# look up a part across everything you know
python xref_dispatch.py "QO130"

# teach a new vendor's cross-reference tool
python site_teacher.py teach "https://example-vendor.com/cross-reference" --name "ExampleVendor"

# look it up on that newly taught site
python generic_xref.py examplevendor "257"

# correct what you find — your fixes are permanent
python cross_reference.py confirm "257" "EQ-257"
python cross_reference.py reject  "257" "WRONG-PART"
```

---

## Purpose & Acceptable Use

This toolkit is intended for **looking up publicly available, factual
cross-reference data** — for example, the manufacturer cross-reference tools
that electrical-component vendors publish for their customers. It is a
general-purpose browser-automation aid in the same family as Playwright,
Selenium, and similar tools.

**You are responsible for how you use it.** By using this toolkit you agree that:

1. **You will only use it to access data you have the right to access.** Use it
   on publicly available pages and information you are permitted to view.

2. **You will comply with the terms of service of any website you use it on.**
   Many sites' terms address automated access. It is your responsibility to know
   and follow the terms of any site you point this tool at. If a site's terms
   prohibit automated access, do not use this tool on that site.

3. **You will not use it to circumvent access controls or technical
   protections.** This tool is not intended for bypassing login walls, CAPTCHAs,
   rate limits, bot-detection, or any other access-control measure, and should
   not be used to do so.

4. **You will not use it on services that prohibit automated querying.** Some
   major services (search engines, social networks, and others) explicitly
   forbid automated access in their terms. This tool is not designed or intended
   for those services.

5. **You will use it considerately.** Keep request volumes low and reasonable.
   Do not use this tool in a way that could disrupt or overload a website.

This toolkit performs one lookup at a time and does not include features for
high-volume or parallel scraping, by design.

## What this tool is *not* for

It is **not** a general web-scraping or data-harvesting tool, and it is **not**
intended for collecting personal data, circumventing site protections, or
accessing any service whose terms prohibit automated access. The maintainers do
not endorse or support such uses.

## No warranty / not legal advice

This toolkit is provided "as is," without warranty of any kind. Cross-reference
results — whether from the local store or a vendor tool — are for informational
purposes and may contain errors; **verify equivalences against manufacturer
documentation before relying on them for orders or installations.** Nothing in
this document is legal advice. If you intend to distribute or deploy this tool,
consult a qualified attorney about your specific situation.

## License

<add your chosen license here — e.g. MIT — see note below>
