"""
Persistent store -- SQLite, the house pattern.

Solves the two problems the real-data run exposed:

  1. Mining results evaporated on exit -> now they persist in mainbox.db
  2. Your corrections ("E-J is a customer, exclude it") had nowhere to live
     -> now they're rows that survive and re-apply on every future mine

Tables:
    vendors / contacts / vendor_categories   -- the learned registry
    records                                   -- SentRecords (outgoing RFQ signal)
    reply_records                             -- mined vendor replies: price/ETA/stock/no-quote facts
    corrections                               -- your judgments, append-only

Corrections currently supported:
    exclude  <key>        drop a vendor (key = vendor_id OR a contact email)
    rename   <key> <new>  fix a display name (e.g. theaenterprises -> "Thea Enterprises")

CLI:
    py -m mainbox_brain.store exclude ej1899
    py -m mainbox_brain.store exclude berson5287@gmail.com
    py -m mainbox_brain.store rename theaenterprises "Thea Enterprises"
    py -m mainbox_brain.store find 8400 connector         # recall: who did I RFQ this to?
    py -m mainbox_brain.store reply-find 12/2 MC       # recall: vendor reply facts/prices/ETAs
    py -m mainbox_brain.store reply-count              # count mined replies
    py -m mainbox_brain.store list            # show corrections
    py -m mainbox_brain.store vendors         # show stored registry
Default db: mainbox.db in the current directory (override with --db PATH).
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .models import Vendor, Contact, SentRecord
from .history_miner import MiningResult, MinedVendor

DEFAULT_DB = "mainbox.db"

# Generic words that should never, on their own, constrain a token search.
# A query like 'and 2097w' must search as '2097w' (not require 'and' too).
_SEARCH_FILLER = {
    "and", "or", "the", "for", "of", "on", "to", "in", "at", "with",
    "any", "some", "me", "us", "my", "our", "is", "are", "was", "were", "be",
    "price", "prices", "pricing", "cost", "costs", "quote", "quoted", "availability",
    "available", "avail", "stock", "lead", "leadtime", "eta", "last", "latest",
    "current", "recent", "purchase", "order", "po", "much", "how", "what", "please",
}

# wire-type / material synonyms collapsed at tokenization so a search matches
# regardless of how it's abbreviated (and so solid stays distinct from stranded,
# copper from aluminum -- the attributes that tell same-size items apart)
_WIRE_SYNONYMS = {
    "stranded": "str", "strand": "str", "strd": "str",
    "solid": "sol",
    "copper": "cu", "coppr": "cu",
    "aluminum": "al", "aluminium": "al", "alum": "al",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sightings INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    confident INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contacts (
    vendor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    PRIMARY KEY (vendor_id, email)
);
CREATE TABLE IF NOT EXISTS vendor_categories (
    vendor_id TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (vendor_id, category)
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email TEXT NOT NULL,
    vendor_id TEXT,
    categories TEXT NOT NULL DEFAULT '[]',
    sent_at TEXT,
    items TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS reply_records (
    source_key TEXT PRIMARY KEY,
    vendor_id TEXT,
    vendor_name TEXT,
    from_email TEXT NOT NULL,
    from_name TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    received_at TEXT,
    body_excerpt TEXT DEFAULT '',
    items TEXT NOT NULL DEFAULT '[]',
    facts TEXT NOT NULL DEFAULT '[]',
    quote_status TEXT DEFAULT 'info',
    confidence REAL NOT NULL DEFAULT 0,
    counterparty_type TEXT DEFAULT 'vendor'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- 'exclude' | 'rename'
    key TEXT NOT NULL,           -- vendor_id or contact email
    value TEXT DEFAULT '',       -- new name for 'rename'
    created_at TEXT NOT NULL
);
"""


def parse_quoted_phrases(text: str) -> tuple[list[str], str]:
    """Split text into (quoted phrases, remaining free text) — inch-mark aware.

    Electrical sizes use the double-quote as an inch mark ("3/4\" red emt"),
    which naive pairing reads as an early close. Rule: a quote immediately
    preceded by a digit is an inch mark (content), not a delimiter — unless
    it's the only candidate left, in which case it closes the phrase (so a
    plain "3/4" still parses). Curly quotes are treated like straight ones.
    """
    norm = text.replace("\u201c", '"').replace("\u201d", '"')
    phrases: list[str] = []
    free: list[str] = []
    i, n = 0, len(norm)
    while i < n:
        ch = norm[i]
        if ch != '"':
            free.append(ch)
            i += 1
            continue
        candidates = [k for k in range(i + 1, n) if norm[k] == '"']
        if not candidates:
            free.append(norm[i + 1:])
            break
        closer = next((k for k in candidates if not norm[k - 1].isdigit()),
                      candidates[-1])
        phrase = norm[i + 1:closer].strip()
        if phrase:
            phrases.append(phrase)
        i = closer + 1
    return phrases, "".join(free)


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = str(path)
        # check_same_thread=False: the Brain's HTTP server (ThreadingHTTPServer)
        # handles each request on a different thread, and a conversational
        # session's Store outlives the thread that created it. Python's sqlite3
        # is built in serialized threadsafe mode, so one connection can be shared
        # across threads safely; this drops Python's own same-thread guard, which
        # was raising "SQLite objects created in a thread can only be used in that
        # same thread" on /ask.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(_SCHEMA)
        # search synonyms = built-in wire/material terms + any vocabulary the
        # user has taught ("'gal' means galvanized"), so learned shorthand
        # matches in future searches
        self._synonyms = dict(_WIRE_SYNONYMS)
        try:
            from .knowledge import Knowledge
            self._synonyms.update(Knowledge(self.db).aliases())
        except Exception:
            pass
        # migration for dbs created before the items column existed
        try:
            self.db.execute("ALTER TABLE records ADD COLUMN items TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already there
        # reply_records was added after v0.7; executes harmlessly for older dbs
        self.db.execute("""CREATE TABLE IF NOT EXISTS reply_records (
            source_key TEXT PRIMARY KEY,
            vendor_id TEXT,
            vendor_name TEXT,
            from_email TEXT NOT NULL,
            from_name TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            received_at TEXT,
            body_excerpt TEXT DEFAULT '',
            items TEXT NOT NULL DEFAULT '[]',
            facts TEXT NOT NULL DEFAULT '[]',
            quote_status TEXT DEFAULT 'info',
            confidence REAL NOT NULL DEFAULT 0
        )""")
        # cost-vs-sell tagging (added later): label each record's counterparty
        try:
            self.db.execute("ALTER TABLE reply_records ADD COLUMN "
                            "counterparty_type TEXT DEFAULT 'vendor'")
        except sqlite3.OperationalError:
            pass  # column already there
        self.db.commit()

    # -- corrections ---------------------------------------------------------
    def add_correction(self, kind: str, key: str, value: str = "") -> None:
        if kind not in {"exclude", "rename", "categories", "add_contact", "add_vendor", "alias", "customer"}:
            raise ValueError(f"unknown correction kind: {kind}")
        self.db.execute(
            "INSERT INTO corrections (kind, key, value, created_at) VALUES (?,?,?,?)",
            (kind, key.strip().lower(), value, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def corrections(self) -> list[tuple[int, str, str, str, str]]:
        return list(self.db.execute(
            "SELECT id, kind, key, value, created_at FROM corrections ORDER BY id"))

    def exclusions(self) -> set[str]:
        return {row[0] for row in
                self.db.execute("SELECT key FROM corrections WHERE kind='exclude'")}

    def renames(self) -> dict[str, str]:
        return {row[0]: row[1] for row in
                self.db.execute("SELECT key, value FROM corrections WHERE kind='rename'")}

    def category_overrides(self) -> dict[str, set[str]]:
        out = {}
        for key, value in self.db.execute(
                "SELECT key, value FROM corrections WHERE kind='categories'"):
            try:
                out[key] = set(json.loads(value))
            except (ValueError, TypeError):
                continue
        return out

    # -- vendor aliases (extra names/keywords for a vendor) -------------------
    def add_alias(self, vendor_id: str, alias: str) -> None:
        """Register an alternate name/keyword/domain for a vendor.

        e.g. add_alias("bandbelec", "Bizzaro") so searches for Bizzaro, and
        mail whose sender name or domain matches it, resolve to bandbelec.
        """
        alias = alias.strip()
        if not alias:
            return
        # de-dup: drop any existing mapping for this alias first
        self.db.execute("DELETE FROM corrections WHERE kind='alias' AND key=?",
                        (alias.lower(),))
        self.add_correction("alias", alias, vendor_id.strip().lower())

    def aliases(self) -> dict[str, str]:
        """{alias_text(lower): vendor_id}."""
        return {row[0]: (row[1] or "").lower() for row in
                self.db.execute("SELECT key, value FROM corrections WHERE kind='alias'")}

    def vendor_aliases(self) -> dict[str, list[str]]:
        """{vendor_id: [alias, alias, ...]} (reverse of aliases())."""
        out: dict[str, list[str]] = {}
        for alias, vid in self.aliases().items():
            out.setdefault(vid, []).append(alias)
        return out

    def remove_alias(self, alias: str) -> int:
        cur = self.db.execute("DELETE FROM corrections WHERE kind='alias' AND key=?",
                              (alias.strip().lower(),))
        self.db.commit()
        return cur.rowcount

    # -- customer / job names (so quotes can show who they were for) ----------
    def add_customer(self, name: str) -> None:
        """Register a customer or job name/keyword (e.g. 'Haugland',
        '383 Madison'). Used to label quotes with who they were for."""
        name = name.strip()
        if not name:
            return
        self.db.execute("DELETE FROM corrections WHERE kind='customer' AND key=?",
                        (name.lower(),))
        self.add_correction("customer", name, name)   # value keeps original case

    def customers(self) -> list[str]:
        return [row[0] for row in
                self.db.execute("SELECT value FROM corrections WHERE kind='customer' ORDER BY key")]

    def remove_customer(self, name: str) -> int:
        cur = self.db.execute("DELETE FROM corrections WHERE kind='customer' AND key=?",
                              (name.strip().lower(),))
        self.db.commit()
        return cur.rowcount

    # -- price-increase notices ------------------------------------------------
    _INCREASE_PAT = re.compile(
        r"price\s.{0,12}(increase|adjust|announce|chang|notification)"
        r"|pricing\s+announcement|new\s+pricing|price\s+increase", re.I)

    def increase_notices(self, vendor_id: str = "", after: str = "") -> list[dict]:
        """Price-increase notices in the mined mail, optionally for one vendor
        and only after a given ISO date. Detected from subjects like
        'Hubbell Pricing Announcement - Effective ...' or 'RE: Price increase'."""
        out = []
        for vid, vname, subject, when in self.db.execute(
                "SELECT vendor_id, vendor_name, subject, received_at FROM reply_records"):
            if vendor_id and (vid or "").lower() != vendor_id.lower():
                continue
            if after and (when or "") <= after:
                continue
            if subject and self._INCREASE_PAT.search(subject):
                out.append({"vendor_id": vid or "", "vendor": vname or vid or "?",
                            "subject": subject, "when": when or ""})
        out.sort(key=lambda d: d["when"])
        return out

    # -- applying corrections to a fresh mining result ------------------------
    def apply_corrections(self, result: MiningResult) -> int:
        """Mutate result in place; returns number of vendors removed."""
        excl = self.exclusions()
        renames = self.renames()
        overrides = self.category_overrides()
        removed = 0
        for vid in list(result.vendors.keys()):
            mv = result.vendors[vid]
            emails = {c.email.lower() for c in mv.vendor.contacts}
            if vid.lower() in excl or (emails & excl):
                del result.vendors[vid]
                result.records = [r for r in result.records if r.vendor_id != vid]
                result.excluded_customers.append(mv.vendor.name + " (correction)")
                removed += 1
                continue
            if vid.lower() in renames:
                mv.vendor.name = renames[vid.lower()]
            if vid.lower() in overrides:
                mv.categories = set(overrides[vid.lower()])
        # user-taught contacts survive re-mines
        from .models import Contact, Vendor
        from .history_miner import MinedVendor, MIN_SIGHTINGS
        # manually-registered vendors (e.g. manufacturers you buy direct, whom
        # you never RFQ, so sent-mail learning never sees them) survive re-mines
        for key, value in self.db.execute(
                "SELECT key, value FROM corrections WHERE kind='add_vendor'"):
            try:
                rec = json.loads(value)
            except (ValueError, TypeError):
                continue
            vid = (rec.get("vendor_id") or "").lower()
            if not vid:
                continue
            anchor = rec.get("email") or (
                f"sales@{rec['domain']}" if rec.get("domain") else "")
            name = rec.get("name") or vid.title()
            if vid in result.vendors:
                mv = result.vendors[vid]
                mv.vendor.name = name
                if anchor and anchor not in {c.email for c in mv.vendor.contacts}:
                    mv.vendor.contacts.append(Contact(name, anchor))
            else:
                contacts = [Contact(name, anchor)] if anchor else []
                result.vendors[vid] = MinedVendor(
                    vendor=Vendor(vendor_id=vid, name=name, contacts=contacts),
                    sightings=MIN_SIGHTINGS + 5, last_seen=None)
        for key, value in self.db.execute(
                "SELECT key, value FROM corrections WHERE kind='add_contact'"):
            try:
                rec = json.loads(value)
            except (ValueError, TypeError):
                continue
            mv = result.vendors.get(rec.get("vendor_id", ""))
            if mv and rec["email"] not in {c.email for c in mv.vendor.contacts}:
                mv.vendor.contacts.insert(0, Contact(rec["name"], rec["email"]))
        return removed

    # -- contact names ---------------------------------------------------------
    @staticmethod
    def _clean_person_name(raw: str) -> str:
        """'Meuse, Anthony' -> 'Anthony Meuse'; ALLCAPS -> Title; emails -> ''."""
        n = (raw or "").strip().strip('"')
        if not n or "@" in n:
            return ""
        if "," in n:
            last, first = [p.strip() for p in n.split(",", 1)]
            n = f"{first} {last}".strip()
        if n.isupper() or n.islower():
            n = n.title()
        return n

    def refresh_contact_names(self) -> int:
        """v0.9.1: the sent-mail miner names a contact from the greeting, else the
        email local-part ("markh" -> "Markh"). Vendor REPLIES carry the real
        display name ("Mark Huddle"); use the most frequent one whenever the
        stored name is only the local-part. Returns how many rows changed."""
        changed = 0
        rows = self.db.execute("SELECT vendor_id, name, email FROM contacts").fetchall()
        for vid, name, email in rows:
            local = (email or "").split("@")[0].lower()
            cur = (name or "").strip()
            derived = (not cur or " " not in cur and
                       re.sub(r"[^a-z0-9]", "", cur.lower()) in
                       (re.sub(r"[^a-z0-9]", "", local), re.split(r"[._\-]", local)[0]))
            if not derived:
                continue
            best = None
            for fn, cnt in self.db.execute(
                    "SELECT from_name, COUNT(*) FROM reply_records "
                    "WHERE lower(from_email)=? AND from_name!='' "
                    "GROUP BY from_name ORDER BY 2 DESC", (email.lower(),)):
                cand = self._clean_person_name(fn)
                if cand and " " in cand or (cand and cand.lower() != local):
                    best = cand
                    break
            if best and best != cur:
                self.db.execute("UPDATE contacts SET name=? WHERE vendor_id=? AND email=?",
                                (best, vid, email))
                changed += 1
        if changed:
            self.db.commit()
        return changed

    # -- persisting a mining result -------------------------------------------
    def save_result(self, result: MiningResult) -> None:
        """Replace stored registry/records with this (corrected) result."""
        cur = self.db.cursor()
        cur.execute("DELETE FROM vendors")
        cur.execute("DELETE FROM contacts")
        cur.execute("DELETE FROM vendor_categories")
        cur.execute("DELETE FROM records")
        for vid, mv in result.vendors.items():
            cur.execute(
                "INSERT INTO vendors VALUES (?,?,?,?,?,?)",
                (vid, mv.vendor.name, mv.sightings,
                 mv.last_seen.isoformat(timespec="seconds") if mv.last_seen else None,
                 1 if mv.confident else 0, mv.vendor.notes))
            for c in mv.vendor.contacts:
                cur.execute("INSERT OR IGNORE INTO contacts VALUES (?,?,?)",
                            (vid, c.name, c.email.lower()))
            for cat in mv.categories:
                cur.execute("INSERT OR IGNORE INTO vendor_categories VALUES (?,?)",
                            (vid, cat))
        for r in result.records:
            cur.execute(
                "INSERT INTO records (to_email, vendor_id, categories, sent_at, items) "
                "VALUES (?,?,?,?,?)",
                (r.to_email, r.vendor_id, json.dumps(sorted(r.categories)),
                 r.when.isoformat(timespec="seconds") if r.when else None,
                 json.dumps(r.items)))
        self.db.commit()
        try:
            self.refresh_contact_names()   # v0.9.1: real names from replies
        except Exception:
            pass

    # -- loading back -----------------------------------------------------------
    def load_vendors(self, confident_only: bool = True) -> dict[str, Vendor]:
        """Load the registry with contacts RANKED: most-frequent, most-recent
        correspondent first, so .primary_contact is the person you actually use."""
        where = "WHERE confident=1" if confident_only else ""
        out: dict[str, Vendor] = {}
        for vid, name, _s, _ls, _conf, notes in self.db.execute(
                f"SELECT * FROM vendors {where}"):
            contacts = [Contact(name=n, email=e) for _v, n, e in self.db.execute(
                "SELECT * FROM contacts WHERE vendor_id=?", (vid,))]
            freq: dict[str, tuple[int, str]] = {}
            for email, cnt, last in self.db.execute(
                    "SELECT to_email, COUNT(*), MAX(COALESCE(sent_at,'')) "
                    "FROM records WHERE vendor_id=? GROUP BY to_email", (vid,)):
                freq[email] = (cnt, last)
            contacts.sort(key=lambda c: freq.get(c.email, (0, "")), reverse=True)
            out[vid] = Vendor(vendor_id=vid, name=name, contacts=contacts,
                              notes=notes or "")
        return out

    def load_records(self) -> list[SentRecord]:
        out = []
        for to_email, vid, cats, sent_at, items in self.db.execute(
                "SELECT to_email, vendor_id, categories, sent_at, items FROM records"):
            when = datetime.fromisoformat(sent_at) if sent_at else None
            out.append(SentRecord(to_email=to_email, vendor_id=vid,
                                  categories=set(json.loads(cats)), when=when,
                                  items=json.loads(items or "[]")))
        return out

    # -- recall search ("who did I buy that one thing from?") -----------------
    # Different trust contract from suggestion: NO confidence floor. You're
    # explicitly searching your own history, so one-off vendors are exactly
    # what you want -- ranked by match quality + recency, with the evidence
    # (date, matched line) attached so you judge it yourself.
    def _norm_tokens(self, text: str) -> set[str]:
        """Tokenize with digit/letter boundary splits so '12/2mc' == '12/2 mc'.
        Wire-type/material synonyms and any user-taught aliases collapse so a
        search matches regardless of abbreviation: stranded=str, solid=sol,
        copper=cu, aluminum=al, plus whatever vocabulary you've taught."""
        text = re.sub(r"(?<=[0-9])(?=[a-z])|(?<=[a-z])(?=[0-9])", " ", text.lower())
        toks = {t for t in re.findall(r"[a-z0-9/\-]+", text) if len(t) > 1}
        syn = getattr(self, "_synonyms", _WIRE_SYNONYMS)
        return {syn.get(t, t) for t in toks}

    def _query_spec(self, query: str):
        """Parse a search query into (required_substrings, free_tokens).

        - "quoted text" -> required exact substring (case-insensitive). Use a
          short quote like "cond1-" to match a family (cond1-g, cond1-b); use
          "cond1-g" to match just that.
        - A bare part-number-like token (has a digit, no spaces, e.g. cond1-g)
          is also treated as a required substring, so it won't loosely match
          unrelated items that merely share a word like "cond".
        - Otherwise the words are free tokens scored by overlap (good for
          multi-word descriptions like "1-5/8 strut").
        """
        phrases, free = parse_quoted_phrases(query)
        required = [p.strip().lower() for p in phrases if p.strip()]
        free_l = free.lower().strip()
        if (not required and free_l and " " not in free_l
                and re.search(r"\d", free_l) and len(free_l) >= 3):
            required = [free_l]
        q_tokens = set() if required else self._norm_tokens(free)
        # filler words never constrain a search; if ONLY filler remains, the
        # query has no real search terms -> empty (so it finds nothing rather
        # than matching every record that happens to say 'price'/'and')
        if q_tokens:
            q_tokens = q_tokens - _SEARCH_FILLER
        return required, q_tokens

    def _score_hay(self, required, q_tokens, specific: str, broad: str | None = None,
                   require_all: bool = True):
        """Score a hit. `specific` is the fact's own product text (used for
        exact/substring matching so a part number can't match a different line);
        `broad` adds subject/vendor/items for token matching.

        Token matching is Outlook-style: by default ALL query words must be
        present somewhere in the record (AND semantics). Ranking then prefers
        facts whose OWN product text holds the words, so a word that only
        appears in the subject can't promote an unrelated line item.
        With require_all=False (the 'similar items' fallback), any overlap
        counts, scored by coverage."""
        if required:
            hl = specific.lower()
            hl_compact = re.sub(r"[^a-z0-9 ]", "", hl)   # keep spaces as boundaries
            for s in required:
                sl = s.lower()
                if sl in hl:                       # literal substring (with separators)
                    continue
                s_compact = re.sub(r"[^a-z0-9 ]", "", sl)
                if len(s_compact) < 3:
                    return None
                # a query ending in a separator (e.g. "cond1-") is a family
                # prefix: require the compact prefix to be followed by a
                # non-digit, so it matches COND1-G but not COND-100 / "2 Cond,1/2"
                if sl and not sl[-1].isalnum():
                    ok, start = False, 0
                    while True:
                        i = hl_compact.find(s_compact, start)
                        if i < 0:
                            break
                        nxt = hl_compact[i + len(s_compact): i + len(s_compact) + 1]
                        if nxt == "" or not nxt.isdigit():
                            ok = True
                            break
                        start = i + 1
                    if not ok:
                        return None
                elif s_compact not in hl_compact:   # full part: separators-stripped
                    # multiword phrases also try a no-space comparison, since
                    # vendors jam sizes into words ('3/4"EMT'); single tokens
                    # don't (a bare '3/4' must not match any stray '34')
                    if " " in s_compact.strip():
                        s_ns = s_compact.replace(" ", "")
                        h_ns = hl_compact.replace(" ", "")
                        if s_ns and s_ns in h_ns:
                            continue
                    return None
            return 1.0
        broad_tokens = self._norm_tokens(broad if broad is not None else specific)
        overlap = q_tokens & broad_tokens
        if not overlap:
            return None
        if require_all and overlap != q_tokens:
            return None                       # Outlook-style: every word must hit
        spec_tokens = self._norm_tokens(specific)
        spec_frac = len(q_tokens & spec_tokens) / len(q_tokens)
        cover_frac = len(overlap) / len(q_tokens)
        # coverage dominates; specificity breaks ties so the fact that actually
        # CONTAINS the words outranks one that merely shares a subject line
        return 0.6 * cover_frac + 0.4 * spec_frac

    @staticmethod
    def _recency_band_order(hits: list, band: float = 0.25) -> list:
        """recency-band ordering (v0.8.3): within `band` of the top score,
        newest first — so a year-old perfect match can't bury this month's
        record; below the band, score order.  Used by find/find_replies and
        their alias-merge paths."""
        if not hits:
            return hits
        top = max(h.get("score", 0) for h in hits)
        in_band = [h for h in hits if h.get("score", 0) >= top - band]
        rest = [h for h in hits if h.get("score", 0) < top - band]
        in_band.sort(key=lambda h: (h.get("when") or "", h.get("score", 0),
                                    h.get("fact_confidence", 0)), reverse=True)
        rest.sort(key=lambda h: (h.get("score", 0), h.get("when") or "",
                                 h.get("fact_confidence", 0)), reverse=True)
        return in_band + rest

    def _alias_variants(self, query: str, cap: int = 4) -> list[str]:
        """The query plus any user-taught product aliases, both directions.
        alias-expand: lets a short name ("12/2mc") find records stored under a
        vendor's long description ("12-2 SOL CU THHN ... 600V") and vice
        versa.  Aliases come from knowledge.learned_aliases (taught via /teach
        or natural-language teaching); both sides are stored normalized."""
        out = [query]
        try:
            from .knowledge import Knowledge, _norm as _kn
            qn = _kn(query)
            if qn:
                for term, canon in Knowledge(self.db).aliases().items():
                    if term == qn and canon not in out:
                        out.append(canon)
                    elif canon == qn and term not in out:
                        out.append(term)
                    if len(out) >= cap:
                        break
        except Exception:
            pass
        return out

    def find(self, query: str, limit: int = 8, _expand: bool = True) -> list[dict]:
        # alias-expand: search every taught variant of the query and merge
        if _expand:
            variants = self._alias_variants(query)
            if len(variants) > 1:
                seen, merged = set(), []
                for v in variants:
                    for h in self.find(v, limit=limit, _expand=False):
                        key = json.dumps({k: h.get(k) for k in
                                          ("item", "to", "vendor", "when")},
                                         sort_keys=True, default=str)
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(h)
                return self._recency_band_order(merged)[:limit]
        required, q_tokens = self._query_spec(query)
        if not required and not q_tokens:
            return []
        names = {vid: name for vid, name in
                 self.db.execute("SELECT vendor_id, name FROM vendors")}
        hits = []
        for to_email, vid, sent_at, items in self.db.execute(
                "SELECT to_email, vendor_id, sent_at, items FROM records"):
            for item in json.loads(items or "[]"):
                score = self._score_hay(required, q_tokens, item, item)
                if score is None:
                    continue
                if not required and score < 0.5:
                    continue
                hits.append({
                    "score": round(score + (0.001 if sent_at else 0), 3),
                    "item": item, "to": to_email,
                    "vendor": names.get(vid, vid or "?"),
                    "when": (sent_at or "")[:10],
                })
        hits = self._recency_band_order(hits)  # v0.8.3
        # dedupe identical (item,to) pairs keeping most recent
        seen, out = set(), []
        for h in hits:
            key = (h["item"].lower(), h["to"])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= limit:
                break
        return out

    # -- vendor reply mining ---------------------------------------------------
    def save_reply_records(self, replies, replace: bool = False) -> int:
        """Persist mined vendor replies.

        If replace=True, clears the reply table first.  Otherwise records are
        upserted by source_key so re-running an export does not duplicate facts.
        Accepts VendorReplyRecord objects from reply_miner, but is duck-typed to
        keep store.py independent from the miner module.
        """
        cur = self.db.cursor()
        if replace:
            cur.execute("DELETE FROM reply_records")
        n = 0
        for r in replies:
            facts = r.facts_json() if hasattr(r, "facts_json") else json.dumps(getattr(r, "facts", []))
            cur.execute(
                "INSERT INTO reply_records "
                "(source_key, vendor_id, vendor_name, from_email, from_name, subject, received_at, "
                " body_excerpt, items, facts, quote_status, confidence, counterparty_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_key) DO UPDATE SET "
                "vendor_id=excluded.vendor_id, vendor_name=excluded.vendor_name, "
                "from_email=excluded.from_email, from_name=excluded.from_name, "
                "subject=excluded.subject, received_at=excluded.received_at, "
                "body_excerpt=excluded.body_excerpt, items=excluded.items, facts=excluded.facts, "
                "quote_status=excluded.quote_status, confidence=excluded.confidence, "
                "counterparty_type=excluded.counterparty_type",
                (
                    getattr(r, "source_key", ""), getattr(r, "vendor_id", ""), getattr(r, "vendor_name", ""),
                    getattr(r, "from_email", ""), getattr(r, "from_name", ""), getattr(r, "subject", ""),
                    getattr(r, "when", None).isoformat(timespec="seconds") if getattr(r, "when", None) else None,
                    getattr(r, "body_excerpt", ""), json.dumps(getattr(r, "items", []) or [], ensure_ascii=False),
                    facts, getattr(r, "quote_status", "info"), float(getattr(r, "confidence", 0.0) or 0.0),
                    getattr(r, "counterparty_type", "vendor"),
                ),
            )
            n += 1
        self.db.commit()
        return n

    def reply_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM reply_records").fetchone()[0]

    def recent_replies(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT vendor_id, vendor_name, from_email, from_name, subject, received_at, items, facts, quote_status, confidence, counterparty_type, source_key "
            "FROM reply_records ORDER BY COALESCE(received_at,'') DESC LIMIT ?", (limit,)
        )
        return [self._reply_row_to_dict(row) for row in rows]

    def customer_names(self) -> list[str]:
        """Distinct customer names seen in mined customer (sell) records."""
        rows = self.db.execute(
            "SELECT DISTINCT vendor_name FROM reply_records "
            "WHERE counterparty_type='customer' AND vendor_name!=''")
        return [r[0] for r in rows if r[0]]

    def customer_identities(self) -> list[tuple]:
        """(vendor_name, email_domain) for every customer record, so callers can
        group records by company (domain) -- e.g. all of ej1899.com is one
        customer 'EJ' even though each sender is a different person."""
        rows = self.db.execute(
            "SELECT DISTINCT vendor_name, from_email FROM reply_records "
            "WHERE counterparty_type='customer' AND (vendor_name!='' OR from_email!='')")
        out = []
        for name, email in rows:
            dom = (email or "").split("@")[-1].lower().strip()
            out.append((name or "", dom))
        return out

    def customer_orders(self, customer: str = "", product: str = "", limit: int = 25) -> list[dict]:
        """Customer purchase records (the sell side) mined from inbound customer
        POs/quotes.  Filter by customer name (separator-insensitive substring)
        and/or product text.  Returns whole records (with their line-item facts),
        newest first."""
        rows = self.db.execute(
            "SELECT vendor_id, vendor_name, from_email, from_name, subject, received_at, items, facts, quote_status, confidence, counterparty_type, source_key "
            "FROM reply_records WHERE counterparty_type='customer' "
            "ORDER BY COALESCE(received_at,'') DESC")
        cnorm = re.sub(r"[^a-z0-9]", "", (customer or "").lower())
        pnorm = (product or "").lower().strip().strip('"')
        out = []
        for row in rows:
            rec = self._reply_row_to_dict(row)
            if cnorm:
                vn = re.sub(r"[^a-z0-9]", "", rec["vendor"].lower())
                dom = re.sub(r"[^a-z0-9]", "", rec["from"].split("@")[-1].lower())
                if cnorm not in vn and not vn.startswith(cnorm) and cnorm not in dom:
                    continue
            if pnorm:
                blob = (rec["subject"] + " " + " ".join(rec["items"]) + " " +
                        " ".join(f.get("item", "") for f in rec["facts"])).lower()
                if pnorm not in blob:
                    continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def _reply_row_to_dict(self, row) -> dict:
        row = list(row)
        # tolerate rows with or without the trailing counterparty_type column
        # (and, v0.9: the source_key that links an answer back to its email)
        ctype = "vendor"
        source_key = ""
        if len(row) >= 12:
            source_key = row[11] or ""
        if len(row) >= 11:
            ctype = row[10] or "vendor"
            row = row[:10]
        vid, vendor_name, from_email, from_name, subject, received_at, items, facts, quote_status, confidence = row
        try:
            parsed_items = json.loads(items or "[]")
        except (TypeError, ValueError):
            parsed_items = []
        try:
            parsed_facts = json.loads(facts or "[]")
        except (TypeError, ValueError):
            parsed_facts = []
        return {
            "vendor_id": vid or "",
            "vendor": vendor_name or vid or "?",
            "from": from_email or "",
            "from_name": from_name or "",
            "subject": subject or "",
            "when": (received_at or "")[:10],
            "items": parsed_items,
            "facts": parsed_facts,
            "status": quote_status or "info",
            "confidence": float(confidence or 0.0),
            "counterparty_type": ctype,
            # the Outlook EntryID (or content hash) of the email this came
            # from -- lets the phone show "the reference" for a price/stock
            "source_key": source_key,
        }

    def find_replies(self, query: str, limit: int = 8, require_all: bool = True,
                     direction: str = "", _expand: bool = True) -> list[dict]:
        """Search mined vendor replies for product/price/ETA facts.

        Returns one row per evidence fact when facts exist.  This is what powers
        questions like "what did Cooper quote on TR26342DVSW?" or "who had 4 PVC
        in stock?".  Results keep the evidence line so the UI can show why the
        answer was chosen.

        direction (optional): 'cost' or 'sell' to restrict to that price side.
        Facts with no direction tag (legacy data) always pass, so this never
        hides older records -- it only separates explicitly-tagged sell vs cost.
        """
        # alias-expand: also search under taught product aliases and merge,
        # so "12/2mc" surfaces records filed under a vendor's long description
        if _expand:
            variants = self._alias_variants(query)
            if len(variants) > 1:
                seen, merged = set(), []
                for v in variants:
                    for h in self.find_replies(v, limit=limit,
                                               require_all=require_all,
                                               direction=direction,
                                               _expand=False):
                        key = json.dumps({k: h.get(k) for k in
                                          ("source_key", "item", "part",
                                           "evidence", "vendor", "when",
                                           "price")},
                                         sort_keys=True, default=str)
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(h)
                return self._recency_band_order(merged)[:limit]
        required, q_tokens = self._query_spec(query)
        if not required and not q_tokens:
            # nothing searchable: recent activity only for a blank query;
            # an all-filler query ('price and availability') finds nothing
            return self.recent_replies(limit) if not (query or "").strip() else []
        valias = self.vendor_aliases()      # {vendor_id: [alias, ...]}
        hits = []
        rows = self.db.execute(
            "SELECT vendor_id, vendor_name, from_email, from_name, subject, received_at, items, facts, quote_status, confidence, counterparty_type, source_key "
            "FROM reply_records"
        )
        for row in rows:
            rec = self._reply_row_to_dict(row)
            subject = rec.get("subject", "")
            items = rec.get("items") or []
            facts = rec.get("facts") or []
            alias_str = " ".join(valias.get(rec.get("vendor_id", ""), []))
            base_text = " ".join([subject, rec.get("vendor", ""), rec.get("from", ""), alias_str] + items)
            if not facts:
                score = self._score_hay(required, q_tokens, base_text, base_text, require_all=require_all)
                if score is None:
                    continue
                hits.append({**rec, "score": round(score, 3), "item": items[0] if items else "", "line": subject})
                continue
            for fact in facts:
                fdir = (fact.get("direction") or "")
                # legacy facts (no direction) always pass; only an explicit
                # opposite tag is filtered out
                if direction and fdir and fdir != direction:
                    continue
                item = (fact.get("item") or "").strip()
                line = (fact.get("source_line") or "").strip()
                # aliases (e.g. Bizzaro -> bandbelec) join the BROAD text so a
                # vendor-name search resolves, but never the part-number text
                hay = " ".join([subject, rec.get("vendor", ""), rec.get("from", ""), alias_str, item, line])
                score = self._score_hay(required, q_tokens, f"{item} {line}", hay, require_all=require_all)
                if score is None:
                    continue
                if not required and not require_all and score < 0.34:
                    continue
                boosted = score + (0.15 if fact.get("unit_price") is not None else 0) + (0.08 if fact.get("lead_time") or fact.get("eta") else 0)
                hits.append({
                    **rec,
                    "score": round(min(boosted, 1.25), 3),
                    "item": item or (items[0] if len(items) == 1 else ""),
                    "line": line,
                    "unit_price": fact.get("unit_price"),
                    "unit": fact.get("unit") or "",
                    "ext_price": fact.get("ext_price"),
                    "lead_time": fact.get("lead_time") or "",
                    "eta": fact.get("eta") or "",
                    "availability": fact.get("availability") or "",
                    "fact_status": fact.get("status") or rec.get("status") or "info",
                    "fact_confidence": float(fact.get("confidence") or rec.get("confidence") or 0.0),
                    "po_number": fact.get("po_number") or "",
                    "direction": fdir,
                })
        # v0.8.3: the caller caps this list (limit=15), so pure score-first
        # ordering silently dropped RECENT records whenever an older line
        # scored a hair higher — the "year-old price" bug.
        hits = self._recency_band_order(hits)
        seen, out = set(), []
        for h in hits:
            key = (h.get("vendor"), h.get("from"), h.get("when"), h.get("line"), h.get("unit_price"), h.get("lead_time"), h.get("eta"))
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= limit:
                break
        return out

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))
        self.db.commit()

    def record_delivery_choice(self, mode: str) -> None:
        n = int(self.get_setting(f"delivery_{mode}", "0")) + 1
        self.set_setting(f"delivery_{mode}", str(n))

    def learned_delivery_default(self) -> str | None:
        """'draft' once you've chosen drafts >=3 times and twice as often as
        send. NEVER auto-learns 'send' -- sending stays explicit (high-risk)."""
        d = int(self.get_setting("delivery_draft", "0"))
        s = int(self.get_setting("delivery_send", "0"))
        if d >= 3 and d >= 2 * max(s, 1):
            return "draft"
        return None

    def vendor_categories(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for vid, cat in self.db.execute("SELECT vendor_id, category FROM vendor_categories"):
            out.setdefault(vid, set()).add(cat)
        return out

    def vendor_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    args = sys.argv[1:]
    db_path = DEFAULT_DB
    if "--db" in args:
        i = args.index("--db")
        db_path = args[i + 1]
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        sys.exit(1)

    store = Store(db_path)
    cmd, rest = args[0], args[1:]

    if cmd == "exclude" and rest:
        for key in rest:
            store.add_correction("exclude", key)
            print(f"Will exclude: {key}")
    elif cmd == "rename" and len(rest) >= 2:
        store.add_correction("rename", rest[0], rest[1])
        print(f"Will rename {rest[0]} -> {rest[1]!r}")
    elif cmd == "add-vendor" and len(rest) >= 2:
        # add-vendor <vendor_id> "<Name>" [domain-or-email]
        vid = rest[0].strip().lower()
        name = rest[1]
        anchor = rest[2].strip().lower() if len(rest) >= 3 else ""
        domain = anchor if anchor and "@" not in anchor else ""
        email = anchor if "@" in anchor else ""
        payload = json.dumps({"vendor_id": vid, "name": name,
                              "domain": domain, "email": email})
        store.add_correction("add_vendor", vid, payload)
        # take effect immediately (and re-apply on every future re-mine)
        anchor_email = email or (f"sales@{domain}" if domain else "")
        store.db.execute("INSERT OR IGNORE INTO vendors(vendor_id,name,sightings) "
                         "VALUES (?,?,?)", (vid, name, 99))
        store.db.execute("UPDATE vendors SET name=? WHERE vendor_id=?", (name, vid))
        if anchor_email:
            store.db.execute("INSERT OR IGNORE INTO contacts(vendor_id,name,email) "
                             "VALUES (?,?,?)", (vid, name, anchor_email))
        store.db.commit()
        where = f" (recognizes mail from {domain or email})" if anchor_email else ""
        print(f"Added vendor {vid} -> {name!r}{where}. Survives re-mines.")
    elif cmd == "add-alias" and len(rest) >= 2:
        # add-alias <vendor_id> <alias> [alias2] [alias3] ...
        vid = rest[0].strip().lower()
        known = {v for (v,) in store.db.execute("SELECT vendor_id FROM vendors")}
        if vid not in known:
            print(f"Note: no vendor '{vid}' yet. Aliases will still apply if/when it exists.")
        for alias in rest[1:]:
            store.add_alias(vid, alias)
        added = ", ".join(repr(a) for a in rest[1:])
        print(f"Aliased {added} -> {vid}. Searches and mail matching these now resolve to {vid}.")
    elif cmd == "remove-alias" and rest:
        n = store.remove_alias(rest[0])
        print(f"Removed {n} alias mapping for {rest[0]!r}." if n else f"No alias {rest[0]!r} found.")
    elif cmd == "aliases":
        va = store.vendor_aliases()
        if not va:
            print("No vendor aliases registered. Add with: "
                  "store add-alias <vendor_id> <alias> [alias2 ...]")
        for vid, al in sorted(va.items()):
            print(f"  {vid}: {', '.join(sorted(al))}")
    elif cmd == "add-customer" and rest:
        names = [n for n in rest if not n.startswith("-")]
        if not names:
            print("Usage: store add-customer \"<Name>\" [\"<Name2>\" ...]")
        for name in names:
            store.add_customer(name)
        if names:
            print(f"Registered customer/job: {', '.join(repr(n) for n in names)}. "
                  "Quotes whose subject/line names these will show 'for <name>'.")
    elif cmd == "remove-customer" and rest:
        n = store.remove_customer(rest[0])
        print(f"Removed customer {rest[0]!r}." if n else f"No customer {rest[0]!r} found.")
    elif cmd == "customers":
        cs = store.customers()
        print("  " + "\n  ".join(cs) if cs else
              "No customers registered. Add with: store add-customer \"<Name>\" [\"<Name2>\" ...]")
    elif cmd == "list":
        rows = store.corrections()
        if not rows:
            print("No corrections recorded.")
        for cid, kind, key, value, when in rows:
            extra = f" -> {value!r}" if value else ""
            print(f"  #{cid} [{when}] {kind} {key}{extra}")
    elif cmd == "find" and rest:
        hits = store.find(" ".join(rest))
        if not hits:
            print("No matching past RFQ lines found.")
        for h in hits:
            print(f"  [{h['when'] or '????-??-??'}] {h['item']!r} -> "
                  f"{h['vendor']} <{h['to']}> (match {int(h['score']*100)}%)")
    elif cmd == "reply-find" and rest:
        hits = store.find_replies(" ".join(rest))
        if not hits:
            print("No matching vendor reply facts found.")
        for h in hits:
            when = (h.get("when") or "")[:10]
            try:
                date_str = datetime.fromisoformat(when).strftime("%m-%d-%Y") if when else "??-??-????"
            except ValueError:
                date_str = when or "??-??-????"
            if h.get("unit_price") is not None:
                unit = f"/{h.get('unit')}" if h.get("unit") else ""
                price = f"${h['unit_price']:g}{unit}"
            else:
                price = h.get("fact_status") or h.get("status") or "info"
            # date  Vendor - Contact - PO# - $price
            segs = [h.get("vendor") or "?"]
            if h.get("from_name"):
                segs.append(h["from_name"])
            if h.get("po_number"):
                segs.append(h["po_number"])
            segs.append(price)
            line = f"  {date_str}  " + " - ".join(segs)
            extra = []
            if h.get("availability"):
                extra.append(h["availability"])
            if h.get("lead_time"):
                extra.append(f"lead {h['lead_time']}")
            if h.get("eta"):
                extra.append(f"ETA {h['eta']}")
            if extra:
                line += "  (" + ", ".join(extra) + ")"
            print(line)
            ev = (h.get("item") or h.get("line") or "").strip()
            if ev:
                print(f"      {ev[:84]}")
    elif cmd == "reply-count":
        print(f"{store.reply_count()} mined vendor reply record(s)")
    elif cmd in ("learned", "learned-facts", "learned-aliases", "gaps", "forget"):
        from .knowledge import Knowledge
        k = Knowledge(store.db)
        if cmd in ("learned", "learned-aliases"):
            al = k.aliases()
            print("Learned vocabulary:" if al else "No vocabulary taught yet "
                  "(teach with: \"<x>\" means <y>).")
            for term, canon in sorted(al.items()):
                print(f"  {term} -> {canon}")
        if cmd in ("learned", "learned-facts"):
            fs = k.facts()
            print("\nLearned facts:" if fs else "No facts learned yet "
                  "(teach with: remember that ...).")
            for topic, ans, source, when, votes in fs:
                print(f"  [{when[:10]} {source}] {topic}: {ans[:80]}")
        if cmd == "gaps":
            gs = k.gaps()
            print("Unanswered questions (gaps):" if gs else "No gaps logged.")
            for q, seen, created, last in gs:
                print(f"  ({seen}x, last {last[:10]}) {q}")
        if cmd == "forget" and rest:
            target = " ".join(rest)
            ok = k.forget_fact(target) or k.forget_alias(target)
            print(f"Forgotten {target!r}." if ok else f"Nothing learned about {target!r}.")
    elif cmd == "vendors":
        vendors = store.load_vendors(confident_only=False)
        if not vendors:
            print(f"No vendors stored in {db_path} yet — run the corpus miner "
                  f"with --db to populate it.")
        for vid, v in vendors.items():
            contacts = ", ".join(c.name for c in v.contacts[:5])
            print(f"  {vid:<24} {v.name:<26} contacts: {contacts}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
