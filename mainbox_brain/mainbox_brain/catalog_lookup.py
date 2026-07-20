"""Price-file lookup against the American Power product catalog.

This is the "price file" source for price questions: the ~34,500-product
SQLite catalog (manufacturer, part number, description, net/list price,
pricing unit, effective date).  It is read-only and entirely optional -- if
the catalog isn't configured or found, every function degrades to "no match"
so the rest of the brain keeps working on mined-email data alone.

Point it at your catalog with the MAINBOX_CATALOG_DB environment variable
(same variable SmartScan uses), e.g.

    setx MAINBOX_CATALOG_DB "C:\\path\\to\\american_power_catalog.db"
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

try:
    from . import config
except Exception:  # pragma: no cover - allow standalone import
    config = None


@dataclass
class CatalogPrice:
    manufacturer: str
    part_number: str
    description: str
    pricing_unit: str
    price_net: float | None
    price_list: float | None
    effective_date: str
    source_file: str = ""

    def as_dict(self) -> dict:
        return {
            "manufacturer": self.manufacturer,
            "part_number": self.part_number,
            "description": self.description,
            "pricing_unit": self.pricing_unit,
            "price_net": self.price_net,
            "price_list": self.price_list,
            "effective_date": self.effective_date,
            "source_file": self.source_file,
        }


def catalog_path() -> str:
    path = os.environ.get("MAINBOX_CATALOG_DB", "")
    if not path and config is not None:
        path = getattr(config, "CATALOG_DB", "") or ""
    return path


_conn: sqlite3.Connection | None = None
_tried = False


def _connect() -> sqlite3.Connection | None:
    global _conn, _tried
    if _conn is not None:
        return _conn
    if _tried:
        return None
    _tried = True
    path = catalog_path()
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # sanity: must have a products table
        conn.execute("SELECT 1 FROM products LIMIT 1")
        _conn = conn
        return _conn
    except sqlite3.Error:
        return None


def available() -> bool:
    return _connect() is not None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _row_to_price(r: sqlite3.Row) -> CatalogPrice:
    def g(k):
        try:
            return r[k]
        except (IndexError, KeyError):
            return None
    return CatalogPrice(
        manufacturer=g("manufacturer_name") or "",
        part_number=g("part_number") or "",
        description=g("description") or "",
        pricing_unit=(g("pricing_unit") or g("unit") or "").strip(),
        price_net=g("price_net"),
        price_list=g("price_list"),
        effective_date=(g("effective_date") or "").strip(),
        source_file=g("source_file") or "",
    )


_mfr_vocab: frozenset[str] | None = None


def manufacturers() -> frozenset[str]:
    """v0.45: distinct manufacturer names from the catalog, normalized via _norm
    for brand matching. Cached for the process; returns an empty set when no
    catalog is connected.

    Used by the cross-reference flow to recognize a leading brand in a query
    (e.g. 'Topaz' in 'Topaz 100') so the vendor tools can key on it. The cached
    connection is shared, so this never closes it.
    """
    global _mfr_vocab
    if _mfr_vocab is not None:
        return _mfr_vocab
    conn = _connect()
    if conn is None:
        _mfr_vocab = frozenset()
        return _mfr_vocab
    try:
        rows = conn.execute(
            "SELECT DISTINCT manufacturer_name FROM products "
            "WHERE manufacturer_name IS NOT NULL "
            "AND TRIM(manufacturer_name) <> ''").fetchall()
        _mfr_vocab = frozenset(
            n for n in (_norm(r["manufacturer_name"]) for r in rows) if n)
    except sqlite3.Error:
        _mfr_vocab = frozenset()
    return _mfr_vocab


def lookup(query: str, limit: int = 3) -> list[CatalogPrice]:
    """Best-effort catalog matches for a product query.

    A part-number-like query (has a digit) matches the catalog part number
    with separators ignored; otherwise the words must all appear in the
    description.  Rows with a net price and the most specific (shortest)
    description rank first.
    """
    conn = _connect()
    if conn is None:
        return []
    q = (query or "").strip()
    if not q:
        return []
    rows: list[sqlite3.Row] = []

    qn = _norm(q)
    looks_like_part = bool(qn and re.search(r"\d", qn) and " " not in q.strip())
    if looks_like_part:
        try:
            rows = conn.execute(
                "SELECT manufacturer_name, part_number, description, unit, pricing_unit, "
                "price_net, price_list, effective_date, source_file FROM products "
                "WHERE REPLACE(REPLACE(REPLACE(LOWER(part_number),'-',''),' ',''),'/','') LIKE ? "
                "OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(part_number_alt,'')),'-',''),' ',''),'/','') LIKE ? "
                "LIMIT 40",
                (f"%{qn}%", f"%{qn}%")).fetchall()
        except sqlite3.Error:
            rows = []

    if not rows:
        toks = [t for t in re.findall(r"[a-z0-9/]+", q.lower()) if len(t) > 1]
        if toks:
            where = " AND ".join("LOWER(description) LIKE ?" for _ in toks)
            params = [f"%{t}%" for t in toks]
            try:
                rows = conn.execute(
                    "SELECT manufacturer_name, part_number, description, unit, pricing_unit, "
                    "price_net, price_list, effective_date, source_file FROM products "
                    f"WHERE {where} LIMIT 60", params).fetchall()
            except sqlite3.Error:
                rows = []

    prices = [_row_to_price(r) for r in rows]
    # prefer rows that actually carry a net price, then most specific desc
    prices.sort(key=lambda p: (p.price_net is None, len(p.description)))
    return prices[:limit]


def best(query: str) -> CatalogPrice | None:
    hits = lookup(query, limit=1)
    return hits[0] if hits else None


# words that vary by manufacturer/branding/packaging/material -- dropped so
# they don't over-constrain a cross-manufacturer search
_SUB_DROP = {
    "red", "blue", "green", "yellow", "gray", "grey", "black", "white", "orange",
    "box", "of", "each", "ea", "pc", "pcs", "piece", "pieces", "bag", "jar",
    "carton", "case", "the", "and", "for", "with", "per", "usa", "domestic",
    "zinc", "steel", "stl", "aluminum", "alum", "malleable", "diecast", "die",
    "cast", "iron", "plated", "in", "inch", "deg", "degree",
}
# connection/variant synonyms -- matched loosely so SS == set screw, etc.
_VARIANTS = {
    "setscrew": {"ss", "setscrew", "set"}, "compression": {"comp", "compression"},
    "insulated": {"insulated", "insul"}, "raintight": {"raintight", "rt"},
}
_SIZE_RE = re.compile(r"\d+(?:-\d+/\d+|/\d+)?")
_NOUN_HINTS = ("connector", "conn", "coupling", "cplg", "elbow", "ell", "box",
               "strap", "bushing", "lug", "clamp", "hanger", "nipple", "bender",
               "fitting", "cover", "plate", "adapter", "reducer", "bell", "washer")


def _features(text: str) -> dict:
    # keep single digits (trade sizes like 1", 2", 3") -- only drop single
    # *letters*; the old len>1 filter was silently eating one-inch sizes
    toks = [t for t in re.findall(r"[a-z0-9/\-]+", (text or "").lower())
            if (len(t) > 1 or t.isdigit()) and t not in _SUB_DROP]
    sizes = [t for t in toks if _SIZE_RE.fullmatch(t)]
    words = [t for t in toks if t not in sizes]
    variants = {name for name, syns in _VARIANTS.items() if syns & set(words)}
    nouns = [w for w in words if any(h in w for h in _NOUN_HINTS)]
    noun = nouns[-1] if nouns else (words[-1] if words else "")
    return {"sizes": sizes, "words": set(words), "variant": variants, "noun": noun}


def substitutes(query: str, limit: int = 10) -> dict:
    """Find cross-manufacturer equivalents for an item.

    A part number is resolved to its description first. Matching is on the
    SIZE + product noun (the reliable signal), with brand/material words
    dropped; candidates are then ranked by how well the rest of the
    description and the connection variant (set-screw vs compression) line up.
    Returns {anchor, candidates}; each candidate carries a `same_mfr` flag.
    """
    conn = _connect()
    if conn is None:
        return {"anchor": None, "candidates": []}

    anchor = best(query)
    anchor_mfr = (anchor.manufacturer if anchor else "") or ""
    fq = _features(query)
    if fq["noun"] and fq["sizes"]:
        # the user described the item -- trust the query, not a possibly
        # contradicting anchor ('3/4 EMT SS connector' must stay set-screw
        # even if best() resolves to a compression part)
        f = fq
    else:
        # bare part number / vague text -- lean on the resolved description,
        # but keep any explicit variant the user did type
        src = anchor.description if (anchor and anchor.description) else query
        f = _features(src)
        if fq["variant"]:
            f["variant"] = fq["variant"]
    if not f["noun"] or not f["sizes"]:
        # not enough to anchor a search (need at least a size and a type word)
        return {"anchor": anchor.as_dict() if anchor else None, "candidates": []}

    # SQL prefilter: every size + the product noun must appear in the desc
    required = f["sizes"] + [f["noun"]]
    where = " AND ".join("LOWER(description) LIKE ?" for _ in required)
    params = [f"%{t}%" for t in required]
    try:
        rows = conn.execute(
            "SELECT manufacturer_name, part_number, description, unit, pricing_unit, "
            "price_net, price_list, effective_date, source_file FROM products "
            f"WHERE {where} LIMIT 600", params).fetchall()
    except sqlite3.Error:
        rows = []

    cand, seen = [], set()
    for r in rows:
        p = _row_to_price(r)
        key = (_norm(p.manufacturer), _norm(p.part_number))
        if key in seen:
            continue
        seen.add(key)
        cf = _features(p.description)
        # size must actually match as a token: '1' LIKE '%1%' also hits '1/2'
        # and '10', so require a shared parsed size to keep precision
        if f["sizes"] and not (set(f["sizes"]) & set(cf["sizes"])):
            continue
        # conflicting connection type excludes (compression vs set-screw)
        if f["variant"] and cf["variant"] and not (f["variant"] & cf["variant"]):
            continue
        overlap = len(f["words"] & cf["words"]) / max(1, len(f["words"]))
        # reward a candidate that shares the requested variant
        vbonus = 0.25 if (f["variant"] and (f["variant"] & cf["variant"])) else 0.0
        d = p.as_dict()
        d["same_mfr"] = bool(anchor_mfr) and _norm(p.manufacturer) == _norm(anchor_mfr)
        d["match"] = round(min(overlap + vbonus, 1.0), 2)
        cand.append(d)

    # drop weak matches -- a thin overlap ('PVC COUPLING' for a set-screw
    # coupling) is noise. When nothing clears the bar, the caller offers to
    # research equivalents instead.
    cand = [d for d in cand if d["match"] >= 0.4]
    cand.sort(key=lambda d: (d["same_mfr"], -d["match"], d["price_net"] is None))
    return {"anchor": anchor.as_dict() if anchor else None, "candidates": cand[:limit]}


if __name__ == "__main__":  # quick manual check
    import sys
    path = catalog_path()
    print(f"catalog: {path or '(not set)'}  available={available()}")
    if available() and len(sys.argv) > 1:
        for p in lookup(" ".join(sys.argv[1:])):
            net = f"${p.price_net:g}/{p.pricing_unit}" if p.price_net else "(no net price)"
            print(f"  {p.manufacturer} {p.part_number} | {p.description[:46]!r} | {net} | {p.effective_date}")
