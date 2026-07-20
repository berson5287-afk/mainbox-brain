"""Learned-knowledge layer: the brain accumulates knowledge over time so it
answers more questions and gets better -- WITHOUT pretending researched or
user-taught facts are confirmed vendor pricing.

Everything learned carries PROVENANCE (source + date) and never overrides mined
vendor data; it fills in where the data is silent, and is labeled as taught or
researched so a procurement decision is never made on an unverified figure.

Three kinds of learned knowledge (all in mainbox.db):
  - aliases:  vocabulary you teach ("'gal' means galvanized"), merged into search
              so future questions match your shorthand.
  - facts:    cached research results + facts you teach ("remember that ..."),
              keyed by topic, reused instead of re-derived.
  - gaps:     questions the brain couldn't answer, logged so you can see what to
              add next.
"""
from __future__ import annotations
__version__ = "0.47"
import re
import sqlite3
from datetime import datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_aliases (
    term       TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL,
    source     TEXT DEFAULT 'user',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS learned_facts (
    topic      TEXT PRIMARY KEY,
    answer     TEXT NOT NULL,
    source     TEXT DEFAULT 'user',
    created_at TEXT,
    votes      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS question_gaps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT,
    tried      TEXT,
    seen       INTEGER DEFAULT 1,
    resolved   INTEGER DEFAULT 0,
    created_at TEXT,
    last_at    TEXT
);
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 /\-]", "", (s or "").lower())).strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Knowledge:
    """Wraps the same SQLite db the Store uses (pass a connection or a path)."""

    def __init__(self, db) -> None:
        self.db = sqlite3.connect(db) if isinstance(db, str) else db
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # -- aliases (vocabulary) ----------------------------------------------
    def learn_alias(self, term: str, canonical: str, source: str = "user") -> None:
        t, c = _norm(term), _norm(canonical)
        if not t or not c or t == c:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO learned_aliases(term,canonical,source,created_at) "
            "VALUES(?,?,?,?)", (t, c, source, _now()))
        self.db.commit()

    def aliases(self) -> dict:
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT term, canonical FROM learned_aliases")}

    def forget_alias(self, term: str) -> bool:
        cur = self.db.execute("DELETE FROM learned_aliases WHERE term=?", (_norm(term),))
        self.db.commit()
        return cur.rowcount > 0

    # -- facts (cached research + taught) ----------------------------------
    def learn_fact(self, topic: str, answer: str, source: str = "user") -> None:
        t = _norm(topic)
        if not t or not (answer or "").strip():
            return
        self.db.execute(
            "INSERT OR REPLACE INTO learned_facts(topic,answer,source,created_at,votes) "
            "VALUES(?,?,?,?,COALESCE((SELECT votes FROM learned_facts WHERE topic=?),0))",
            (t, answer.strip(), source, _now(), t))
        self.db.commit()

    def lookup_fact(self, query: str):
        """Return (topic, answer, source, created_at) for the best topic match,
        or None.  Exact normalized match first, then containment either way."""
        q = _norm(query)
        if not q:
            return None
        row = self.db.execute(
            "SELECT topic,answer,source,created_at FROM learned_facts WHERE topic=?",
            (q,)).fetchone()
        if row:
            return row
        best = None
        for r in self.db.execute(
                "SELECT topic,answer,source,created_at FROM learned_facts"):
            t = r[0]
            if t and (t in q or q in t):
                if best is None or len(t) > len(best[0]):
                    best = r
        return best

    def facts(self, limit: int = 100) -> list:
        return self.db.execute(
            "SELECT topic,answer,source,created_at,votes FROM learned_facts "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def forget_fact(self, topic: str) -> bool:
        q = _norm(topic)
        cur = self.db.execute(
            "DELETE FROM learned_facts WHERE topic=? OR instr(topic,?)>0 OR instr(?,topic)>0",
            (q, q, q))
        self.db.commit()
        return cur.rowcount > 0

    def vote_fact(self, topic: str, delta: int) -> None:
        self.db.execute("UPDATE learned_facts SET votes=votes+? WHERE topic=?",
                        (delta, _norm(topic)))
        self.db.commit()

    # -- gaps (unanswered questions) ---------------------------------------
    def log_gap(self, question: str, tried: str = "") -> None:
        q = (question or "").strip()
        if not q:
            return
        row = self.db.execute(
            "SELECT id, seen FROM question_gaps WHERE question=? AND resolved=0",
            (q,)).fetchone()
        if row:
            self.db.execute("UPDATE question_gaps SET seen=?, last_at=? WHERE id=?",
                            (row[1] + 1, _now(), row[0]))
        else:
            self.db.execute(
                "INSERT INTO question_gaps(question,tried,seen,created_at,last_at) "
                "VALUES(?,?,1,?,?)", (q, tried, _now(), _now()))
        self.db.commit()

    def gaps(self, limit: int = 40) -> list:
        return self.db.execute(
            "SELECT question, seen, created_at, last_at FROM question_gaps "
            "WHERE resolved=0 ORDER BY seen DESC, last_at DESC LIMIT ?", (limit,)).fetchall()

    def resolve_gap(self, question: str) -> None:
        self.db.execute("UPDATE question_gaps SET resolved=1 WHERE question=?",
                        ((question or "").strip(),))
        self.db.commit()


# ---------------------------------------------------------------------------
# Parse natural-language teaching out of a user message.
# ---------------------------------------------------------------------------
_FORGET_RE = re.compile(
    r"^\s*(?:forget|delete|remove|unlearn)\s+(?:that\s+|about\s+|the\s+)?(.+)", re.I)
_REMEMBER_RE = re.compile(
    r"^\s*(?:remember|note|keep in mind|for (?:the )?future|fyi|jot down|"
    r"make a note)(?:\s+that)?\s*[:,]?\s+(.+)", re.I)
# "<A> means <B>", "<A> is the same as <B>", "<A> stands for <B>", "<A> = <B>",
# "we call <B> <A>"
_ALIAS_RES = [
    re.compile(r"^\s*(.+?)\s+(?:means|stands for|is short for|is the same as|"
               r"is another (?:word|name) for|=)\s+(.+?)\s*$", re.I),
    re.compile(r"^\s*we\s+call\s+(.+?)\s+(.+?)\s*$", re.I),   # we call <canonical> <term>
]

# v0.47: natural teaching of brand lineage, substitutes, and rep relationships.
_PRONOUNS = {"we", "i", "they", "you", "it", "he", "she", "who", "customer",
             "the customer", "someone"}
_CLAUSE_END = r"(?=\s+(?:so|and|but|because|since|now|then)\b|[,.!?;]|$)"
_LINEAGE_RES = [
    # passive FIRST: "Versabar is owned by Wesanco", "Topaz was acquired by
    # Southwire" -- must precede the active form, whose open match would
    # otherwise swallow "was acquired" and capture "by <owner>" as the brand.
    (re.compile(r"\b([A-Za-z][\w&.\- ]{0,30}?)\s+(?:is|was|got)\s+"
                r"(?:owned|bought|acquired|purchased)\s+by\s+"
                r"([A-Za-z][\w&.\- ]{0,30}?)" + _CLAUSE_END, re.I), "brand_first"),
    # active: "Wesanco owns Versabar", "Southwire bought/acquired Topaz"
    (re.compile(r"\b([A-Za-z][\w&.\- ]{0,30}?)\s+(?:now\s+)?"
                r"(?:owns|bought|acquired|purchased)\s+(?!by\b)"
                r"([A-Za-z][\w&.\- ]{0,30}?)" + _CLAUSE_END, re.I), "owner_first"),
]
_XREF_RES = [
    # "add Z as a sub for Q", "make Z an equivalent of Q", "save Z as a cross for Q"
    re.compile(r"\b(?:add|make|set|save|use)\s+(.+?)\s+as\s+(?:a\s+|an\s+)?"
               r"(?:sub(?:stitute)?|cross(?:[\- ]?ref(?:erence)?)?|equivalent|"
               r"equal|replacement|alt(?:ernate)?)\s+(?:for|of|to)\s+(.+?)\s*[.!?]?\s*$",
               re.I),
    # "Z is a sub for Q", "Z subs for Q", "Z replaces Q", "Z can replace Q"
    re.compile(r"^\s*(?:please\s+)?(.+?)\s+(?:is\s+(?:a\s+|an\s+)?"
               r"(?:sub(?:stitute)?|cross|equivalent|replacement)\s+(?:for|of|to)|"
               r"subs\s+for|replaces|can\s+replace)\s+(.+?)\s*[.!?]?\s*$", re.I),
    # "use Z instead of Q"
    re.compile(r"^\s*(?:please\s+)?use\s+(.+?)\s+instead\s+of\s+(.+?)\s*[.!?]?\s*$",
               re.I),
]
_REP_RES = [
    # "Brazill Brothers reps Topaz", "Brazill Brothers is the rep for Topaz"
    (re.compile(r"^\s*(?:please\s+)?(.+?)\s+(?:reps|represents|is\s+the\s+rep\s+"
                r"(?:for|of))\s+(.+?)\s*[.!?]?\s*$", re.I), "rep_first"),
    # "Topaz is repped by Brazill Brothers"
    (re.compile(r"^\s*(.+?)\s+is\s+rep(?:ped|resented)\s+by\s+(.+?)\s*[.!?]?\s*$",
                re.I), "brand_first"),
]


def _clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip(" .?!,\"'")).strip()


def _namey(s: str, max_words: int = 4) -> bool:
    """A plausible company/brand token: short, has letters, not a pronoun."""
    s = _clean_name(s)
    return (bool(s) and len(s.split()) <= max_words
            and bool(re.search(r"[A-Za-z]", s))
            and s.lower() not in _PRONOUNS)


def parse_teaching(text: str):
    """Return a teaching action or None:
      ('forget',  target)
      ('lineage', brand, owner)        v0.47: '<owner> owns/bought <brand>'
      ('xref',    src, equiv)          v0.47: 'add <equiv> as a sub for <src>'
      ('fact',    topic, statement)    (rep relations produce facts too)
      ('alias',   term, canonical)
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _FORGET_RE.match(t)
    if m:
        return ("forget", m.group(1).strip(" .?!"))
    # v0.47: brand lineage -- checked early so 'remember that X bought Y' and
    # 'X owns Y so please remember this' both land here, not in generic facts.
    for rx, order in _LINEAGE_RES:
        m = rx.search(t)
        if m:
            a, b = _clean_name(m.group(1)), _clean_name(m.group(2))
            # strip a courtesy/lead-in that the open match may have swallowed
            a = re.sub(r"^(?:please\s+|remember\s+that\s+|note\s+that\s+|fyi\s+)+",
                       "", a, flags=re.I).strip()
            if _namey(a) and _namey(b):
                owner, brand = (a, b) if order == "owner_first" else (b, a)
                return ("lineage", brand, owner)
    # v0.47: user-declared substitutes -> curated store
    for rx in _XREF_RES:
        m = rx.search(t)
        if m:
            equiv, src = _clean_name(m.group(1)), _clean_name(m.group(2))
            if (equiv and src and len(equiv.split()) <= 6
                    and len(src.split()) <= 6):
                return ("xref", src, equiv)
    # v0.47: rep relationships -> facts keyed to the brand
    for rx, order in _REP_RES:
        m = rx.match(t)
        if m:
            a, b = _clean_name(m.group(1)), _clean_name(m.group(2))
            rep, brand = (a, b) if order == "rep_first" else (b, a)
            if _namey(rep) and _namey(brand):
                return ("fact", brand, f"{brand} is repped by {rep}")
    for i, rx in enumerate(_ALIAS_RES):
        m = rx.match(t)
        if m:
            a, b = m.group(1).strip(" .?!\"'"), m.group(2).strip(" .?!\"'")
            # both sides should be short vocabulary, not whole sentences
            if 1 <= len(a.split()) <= 4 and 1 <= len(b.split()) <= 5:
                if i == 1:          # "we call <canonical> <term>" -> term=b, canon=a
                    return ("alias", b, a)
                return ("alias", a, b)
    m = _REMEMBER_RE.match(t)
    if m:
        stmt = m.group(1).strip()
        return ("fact", _fact_topic(stmt), stmt)
    return None


def _fact_topic(statement: str) -> str:
    """A short key for a taught fact: the noun-ish lead-in before 'is/are/=/:'."""
    m = re.match(r"^(.*?)\s+(?:is|are|=|:|usually|typically|has|have|takes)\b",
                 statement, re.I)
    topic = m.group(1) if m else statement
    # drop leading articles
    topic = re.sub(r"^(the|a|an|our|my)\s+", "", topic.strip(), flags=re.I)
    return topic.strip()[:80] or statement[:80]
