#!/usr/bin/env python3
"""
cross_reference.py - correctable breaker cross-reference store for MaINbox Brain.

Tier 1 of the substitute resolver: a local SQLite table of part equivalences.
Checked BEFORE any web call - instant, free, offline. Seeded with researched
breaker data, but every row is a *suggestion* the user can confirm, correct, or
reject; corrections become the source of truth and seeding never overwrites them.

Equivalence is not binary - the schema records the TYPE of equivalence plus a
caveat, because "fits electrically" is not the same as "UL-listed for that panel"
(NEC 110.3(B) - using a non-listed breaker can void the panel listing):

    same_line_variant  - same line, different mounting (QO plug-on vs QOB bolt-on)
    ul_classified      - UL-classified & listed to replace in the target panel
                         (Siemens QD / Eaton CHQ for QO; Eaton CL for Homeline)
    same_oem_rebrand   - same product, different nameplate (Cutler-Hammer = Eaton)
    spec_equivalent    - same electrical rating; may fit physically but NOT listed
                         for cross-panel use - reference only

stdlib only. No external deps.

    python cross_reference.py seed
    python cross_reference.py lookup QO130
    python cross_reference.py add QO130 "Square D" QOM2130 "Square D" --type same_line_variant
    python cross_reference.py confirm QO130 QD130
    python cross_reference.py reject  QO130 THQL1130
    python cross_reference.py stats

Import: from cross_reference import lookup, confirm, reject, add, seed, resolve
"""

from __future__ import annotations

import os
import re
import sys
import sqlite3
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone

__version__ = "0.2.0"  # graph lookup: bidirectional + transitive (via X), mfr-tolerant; rejected never resurrects

DB_PATH = os.environ.get("XREF_DB", "cross_references.db")

# --- Equivalence types ------------------------------------------------------
T_VARIANT = "same_line_variant"
T_CLASSIFIED = "ul_classified"
T_REBRAND = "same_oem_rebrand"
T_SPEC = "spec_equivalent"
_TYPE_RANK = {T_VARIANT: 0, T_CLASSIFIED: 1, T_REBRAND: 2, T_SPEC: 3}
_VALID_TYPES = set(_TYPE_RANK)

# --- Caveats (one per equivalence kind) -------------------------------------
CAV_STD_FAMILY = ("Standard 1-inch type - physically similar within this group, but match "
                  "breaker brand to panel brand for UL listing/code (NEC 110.3(B)). Eaton CL "
                  "series is the UL-classified option listed for cross-panel use.")
CAV_QO_SPEC = ("Same electrical rating only. Square D QO uses a proprietary bus - these do NOT "
               "fit a QO panel. For a QO panel use a QO breaker or a UL-classified QO "
               "replacement (Siemens QD / Eaton CHQ).")
CAV_QO_CLASSIFIED = ("UL-classified to replace QO in QO panels. Verify the exact catalog number "
                     "and that the panel model is on the classified compatibility list; some "
                     "AHJs reject classified breakers.")
CAV_HOM_SPEC = ("Same electrical rating only. Homeline uses its own bus - not a physical drop-in "
                "for other panels. Eaton CL series is UL-classified for Homeline panels.")
CAV_VARIANT = ("Same line - mounting differs (e.g. QO plug-on vs QOB bolt-on). Confirm the panel "
               "accepts this mounting style.")
CAV_REBRAND = ("Same product, different nameplate - Cutler-Hammer and Eaton parts are identical "
               "(Eaton acquired Cutler-Hammer in 1978).")

# --- Manufacturer aliasing (lineage / acquisitions) -------------------------
_MFR_ALIASES = {
    "CUTLER-HAMMER": "Eaton", "CUTLER HAMMER": "Eaton", "CUTLERHAMMER": "Eaton",
    "WESTINGHOUSE": "Eaton", "BRYANT": "Eaton", "CHALLENGER": "Eaton",
    "ITE": "Siemens", "GOULD": "Siemens", "BULLDOG": "Siemens",
    "SQUARE D": "Square D", "SQUARED": "Square D", "SCHNEIDER": "Square D",
    "SCHNEIDER ELECTRIC": "Square D",
    "GENERAL ELECTRIC": "GE", "ABB": "GE",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_part(part: str) -> str:
    """Uppercase, strip separators, drop a trailing consumer-pack 'CP'."""
    p = re.sub(r"[\s\-_/.]", "", (part or "").upper())
    if p.endswith("CP") and len(p) > 4:   # QO130CP -> QO130
        p = p[:-2]
    return p


def canon_mfr(mfr: str | None) -> str:
    """Map brand lineage/aliases to a canonical manufacturer name."""
    if not mfr:
        return ""
    key = re.sub(r"\s+", " ", mfr.strip()).upper()
    return _MFR_ALIASES.get(key, mfr.strip())


@dataclass
class Equivalent:
    part: str
    mfr: str
    line: str
    equiv_type: str
    confidence: float
    status: str
    caveat: str
    source: str

    def display(self) -> str:
        return f"{self.mfr} {self.part}".strip()


# --- Schema / connection ----------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_references (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_part    TEXT NOT NULL,
    src_mfr     TEXT NOT NULL,
    src_line    TEXT,
    src_poles   INTEGER,
    src_amps    INTEGER,
    equiv_part  TEXT NOT NULL,
    equiv_mfr   TEXT NOT NULL,
    equiv_line  TEXT,
    equiv_type  TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0.5,
    status      TEXT NOT NULL DEFAULT 'suggested',
    caveat      TEXT,
    source      TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(src_part, src_mfr, equiv_part, equiv_mfr)
);
CREATE INDEX IF NOT EXISTS idx_xref_src ON cross_references(src_part);
"""


def _connect(db: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# --- Core lookup (tier 1 of the substitute resolver) ------------------------
def _edges(conn, np: str, cm: str) -> list[tuple]:
    """All non-rejected edges touching a node, BOTH directions.
    Returns [(other_part, other_mfr, equiv_type, confidence, status,
              caveat, source, line)].  v0.2.0: an equivalence taught one way
    (or with no manufacturer) is still found from the other side; rows with
    empty src/equiv mfr match any manufacturer."""
    out = []
    q = ("SELECT * FROM cross_references WHERE src_part=? "
         "AND status != 'rejected'")
    a: list = [np]
    if cm:
        q += " AND (src_mfr=? OR src_mfr='')"
        a.append(cm)
    for r in conn.execute(q, a):
        out.append((r["equiv_part"], r["equiv_mfr"] or "", r["equiv_type"],
                    r["confidence"], r["status"], r["caveat"] or "",
                    r["source"] or "", r["equiv_line"] or ""))
    q2 = ("SELECT * FROM cross_references WHERE equiv_part=? "
          "AND status != 'rejected'")
    a2: list = [np]
    if cm:
        q2 += " AND (equiv_mfr=? OR equiv_mfr='')"
        a2.append(cm)
    for r in conn.execute(q2, a2):
        out.append((r["src_part"], r["src_mfr"] or "", r["equiv_type"],
                    r["confidence"], r["status"], r["caveat"] or "",
                    r["source"] or "", r["src_line"] or ""))
    return out


def _rejected_partners(conn, np: str) -> set:
    """Pairs the user explicitly rejected for this part — never resurrected,
    not even through a transitive chain."""
    rej = set()
    for r in conn.execute("SELECT equiv_part FROM cross_references "
                          "WHERE src_part=? AND status='rejected'", (np,)):
        rej.add(r["equiv_part"])
    for r in conn.execute("SELECT src_part FROM cross_references "
                          "WHERE equiv_part=? AND status='rejected'", (np,)):
        rej.add(r["src_part"])
    return rej


def lookup(part: str, mfr: str | None = None,
           db: str | None = None, transitive: bool = True) -> list[Equivalent]:
    """Return ranked equivalents for a part from the local store.

    v0.2.0: equivalence is a GRAPH — if A≡C and B≡A, looking up any of the
    three shows the other two. Directly-linked parts come first; parts
    reached through one intermediate carry a "via X" caveat and the weaker
    of the two confidences. Rejected pairs never appear, directly or via a
    chain.

    Ranking: user-confirmed first, then by equivalence type (variant >
    classified > rebrand > spec), then by confidence.
    """
    np = normalize_part(part)
    cm = canon_mfr(mfr) if mfr else ""
    conn = _connect(db)
    try:
        rejected = _rejected_partners(conn, np)
        found: dict[str, tuple] = {}       # norm_part -> best entry tuple
        direct = _edges(conn, np, cm)
        for (p, m, et, cf, st, cav, src, ln) in direct:
            k = normalize_part(p)
            if k == np or k in rejected:
                continue
            prev = found.get(k)
            rank = (0 if st == "confirmed" else 1, -cf)
            if prev is None or rank < prev[0]:
                found[k] = (rank, Equivalent(part=p, mfr=m, line=ln,
                                             equiv_type=et, confidence=cf,
                                             status=st, caveat=cav,
                                             source=src))
        if transitive:
            hop1 = list(found.items())
            for k1, (_, e1) in hop1:
                for (p, m, et, cf, st, cav, src, ln) in _edges(
                        conn, k1, canon_mfr(e1.mfr) if e1.mfr else ""):
                    k2 = normalize_part(p)
                    if k2 == np or k2 in found or k2 in rejected:
                        continue
                    via = f"{e1.mfr} {e1.part}".strip()
                    cf2 = min(cf, e1.confidence)
                    st2 = "confirmed" if (st == "confirmed"
                                          and e1.status == "confirmed")                         else "suggested"
                    found[k2] = ((2, -cf2), Equivalent(
                        part=p, mfr=m, line=ln, equiv_type=et,
                        confidence=cf2, status=st2,
                        caveat=(cav + "; " if cav else "") + f"via {via}",
                        source=src))
    finally:
        conn.close()

    entries = sorted(found.values(), key=lambda t: (
        t[0][0] if len(t[0]) == 2 else t[0][0],
        0 if t[1].status == "confirmed" else 1,
        _TYPE_RANK.get(t[1].equiv_type, 9),
        -t[1].confidence))
    return [e for _, e in entries]


def resolve(part: str, mfr: str | None = None, db: str | None = None) -> dict:
    """Tier-1 entry point for the substitute flow. Returns a dict the caller can
    branch on: if 'found' is False, fall through to the web research engine."""
    eqs = lookup(part, mfr, db=db)
    return {"part": part, "mfr": canon_mfr(mfr) if mfr else None,
            "found": bool(eqs), "equivalents": eqs}


# --- Correction loop (human-in-the-loop) ------------------------------------
def confirm(part: str, equiv_part: str, db: str | None = None) -> int:
    """User vouches for an equivalence: promote to confirmed, raise confidence,
    re-source to 'user'. Returns rows affected."""
    conn = _connect(db)
    try:
        cur = conn.execute(
            "UPDATE cross_references SET status='confirmed', "
            "confidence=MAX(confidence, 0.95), source='user', updated_at=? "
            "WHERE src_part=? AND equiv_part=?",
            (_now(), normalize_part(part), normalize_part(equiv_part)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def reject(part: str, equiv_part: str, db: str | None = None) -> int:
    """User rejects an equivalence: it will never be suggested again."""
    conn = _connect(db)
    try:
        cur = conn.execute(
            "UPDATE cross_references SET status='rejected', source='user', "
            "updated_at=? WHERE src_part=? AND equiv_part=?",
            (_now(), normalize_part(part), normalize_part(equiv_part)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def add(part: str, mfr: str, equiv_part: str, equiv_mfr: str,
        equiv_type: str = T_SPEC, caveat: str = "", src_line: str = "",
        equiv_line: str = "", src_poles: int | None = None,
        src_amps: int | None = None, confidence: float = 0.97,
        notes: str = "", db: str | None = None) -> int:
    """User adds (or overrides) an equivalence. Stored confirmed + source='user'
    so it ranks first and seeding will never clobber it."""
    if equiv_type not in _VALID_TYPES:
        raise ValueError(f"equiv_type must be one of {sorted(_VALID_TYPES)}")
    conn = _connect(db)
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO cross_references "
            "(src_part, src_mfr, src_line, src_poles, src_amps, equiv_part, "
            " equiv_mfr, equiv_line, equiv_type, confidence, status, caveat, "
            " source, notes, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'confirmed',?, 'user', ?, ?, ?) "
            "ON CONFLICT(src_part, src_mfr, equiv_part, equiv_mfr) DO UPDATE SET "
            " equiv_type=excluded.equiv_type, confidence=excluded.confidence, "
            " status='confirmed', caveat=excluded.caveat, source='user', "
            " notes=excluded.notes, updated_at=excluded.updated_at",
            (normalize_part(part), canon_mfr(mfr), src_line, src_poles, src_amps,
             normalize_part(equiv_part), canon_mfr(equiv_mfr), equiv_line,
             equiv_type, confidence, caveat, notes, now, now))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# --- Seed builder (researched starter data) ---------------------------------
# Standard 1-inch interchange family. {p}=poles, {a}=amps.
_STD_FAMILY = [
    ("Siemens", "QP",   lambda p, a: f"Q{p}{a}"),
    ("Murray",  "MP",   lambda p, a: f"MP{p}{a}"),
    ("GE",      "THQL", lambda p, a: f"THQL{p}1{a}"),
    ("Eaton",   "BR",   lambda p, a: f"BR{p}{a}"),
]
# UL-classified replacements made for Square D QO panels.
_CLASSIFIED_QO = [
    ("Siemens", "QD",  lambda p, a: f"QD{p}{a}"),
    ("Eaton",   "CHQ", lambda p, a: f"CHQ{a}" if p == 1 else f"CHQ{p}{a}"),
]
_SEED_AMPS = [15, 20, 25, 30, 40, 50, 60]
_SEED_POLES = [1, 2]


def _seed_rows() -> list[dict]:
    """Generate the researched seed. All rows: status suggested, source
    seed:research, modest confidence, caveat by type. The classified part-number
    patterns especially are best-effort and flagged for verification."""
    rows: list[dict] = []

    def row(sp, sm, sl, p, a, ep, em, el, et, conf, cav):
        rows.append(dict(
            src_part=normalize_part(sp), src_mfr=sm, src_line=sl,
            src_poles=p, src_amps=a, equiv_part=normalize_part(ep),
            equiv_mfr=em, equiv_line=el, equiv_type=et, confidence=conf, caveat=cav))

    for p in _SEED_POLES:
        for a in _SEED_AMPS:
            qo = f"QO{p}{a}"
            hom = f"HOM{p}{a}"
            fam = [(m, ln, fn(p, a)) for (m, ln, fn) in _STD_FAMILY]

            # Square D QO -> same-line bolt-on variant
            row(qo, "Square D", "QO", p, a, f"QOB{p}{a}", "Square D", "QOB",
                T_VARIANT, 0.80, CAV_VARIANT)
            # Square D QO -> spec equivalents in the standard family (NOT a fit)
            for (m, ln, num) in fam:
                row(qo, "Square D", "QO", p, a, num, m, ln, T_SPEC, 0.45, CAV_QO_SPEC)
            # Square D QO -> UL-classified QO replacements (legit, verify number)
            for (m, ln, fn) in _CLASSIFIED_QO:
                row(qo, "Square D", "QO", p, a, fn(p, a), m, ln,
                    T_CLASSIFIED, 0.50, CAV_QO_CLASSIFIED)

            # Square D Homeline -> spec equivalents + classified note
            for (m, ln, num) in fam:
                row(hom, "Square D", "Homeline", p, a, num, m, ln,
                    T_SPEC, 0.45, CAV_HOM_SPEC)
            row(hom, "Square D", "Homeline", p, a, f"CL{p}{a}", "Eaton", "CL",
                T_CLASSIFIED, 0.50, CAV_HOM_SPEC)

            # Standard family: mutual spec equivalents (each -> the others)
            for i, (m_i, ln_i, num_i) in enumerate(fam):
                for j, (m_j, ln_j, num_j) in enumerate(fam):
                    if i == j:
                        continue
                    row(num_i, m_i, ln_i, p, a, num_j, m_j, ln_j,
                        T_SPEC, 0.55, CAV_STD_FAMILY)
    return rows


def _upsert_seed(conn: sqlite3.Connection, r: dict) -> str:
    """Insert a seed row, or refresh it - but never touch a row the user has
    confirmed, rejected, or authored. Returns 'inserted' | 'updated' | 'kept'."""
    ex = conn.execute(
        "SELECT id, source, status FROM cross_references "
        "WHERE src_part=? AND src_mfr=? AND equiv_part=? AND equiv_mfr=?",
        (r["src_part"], r["src_mfr"], r["equiv_part"], r["equiv_mfr"])).fetchone()
    now = _now()
    if ex is None:
        conn.execute(
            "INSERT INTO cross_references "
            "(src_part, src_mfr, src_line, src_poles, src_amps, equiv_part, "
            " equiv_mfr, equiv_line, equiv_type, confidence, status, caveat, "
            " source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'suggested',?, 'seed:research', ?, ?)",
            (r["src_part"], r["src_mfr"], r["src_line"], r["src_poles"],
             r["src_amps"], r["equiv_part"], r["equiv_mfr"], r["equiv_line"],
             r["equiv_type"], r["confidence"], r["caveat"], now, now))
        return "inserted"
    if ex["source"] == "user" or ex["status"] in ("confirmed", "rejected"):
        return "kept"   # preserve human truth
    conn.execute(
        "UPDATE cross_references SET src_line=?, src_poles=?, src_amps=?, "
        "equiv_line=?, equiv_type=?, confidence=?, caveat=?, updated_at=? "
        "WHERE id=?",
        (r["src_line"], r["src_poles"], r["src_amps"], r["equiv_line"],
         r["equiv_type"], r["confidence"], r["caveat"], now, ex["id"]))
    return "updated"


def seed(db: str | None = None) -> dict:
    """Load/refresh the researched seed. Idempotent and non-destructive to user
    edits. Returns counts."""
    conn = _connect(db)
    tally = {"inserted": 0, "updated": 0, "kept": 0}
    try:
        for r in _seed_rows():
            tally[_upsert_seed(conn, r)] += 1
        conn.commit()
    finally:
        conn.close()
    return tally


def stats(db: str | None = None) -> dict:
    conn = _connect(db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM cross_references").fetchone()[0]
        by_status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM cross_references GROUP BY status").fetchall())
        by_type = dict(conn.execute(
            "SELECT equiv_type, COUNT(*) FROM cross_references GROUP BY equiv_type").fetchall())
    finally:
        conn.close()
    return {"total": total, "by_status": by_status, "by_type": by_type}


# --- CLI --------------------------------------------------------------------
def _print_lookup(part: str, mfr: str | None, db: str | None) -> None:
    res = resolve(part, mfr, db=db)
    if not res["found"]:
        print(f"No equivalents stored for '{part}'"
              f"{' (' + res['mfr'] + ')' if res['mfr'] else ''}. "
              f"-> tier-1 miss; fall back to web research.")
        return
    print(f"Equivalents for {part}"
          f"{' (' + res['mfr'] + ')' if res['mfr'] else ''}:\n")
    for e in res["equivalents"]:
        flag = {"confirmed": "[OK]", "suggested": "[?]"}.get(e.status, "")
        print(f"  {flag} {e.display():<16} {e.equiv_type:<18} "
              f"conf={e.confidence:.2f}  src={e.source}")
        if e.caveat:
            print(f"        ! {e.caveat}")
    print("\nConfirm/correct: cross_reference.py confirm|reject|add ...")


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="cross_reference.py",
                                 description="Correctable breaker cross-reference store.")
    ap.add_argument("--db", default=None, help=f"SQLite path (default {DB_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="load/refresh researched seed data")
    sub.add_parser("stats", help="show counts")

    lp = sub.add_parser("lookup", help="find equivalents for a part")
    lp.add_argument("part")
    lp.add_argument("mfr", nargs="?", default=None)

    cp = sub.add_parser("confirm", help="mark an equivalence as user-confirmed")
    cp.add_argument("part")
    cp.add_argument("equiv_part")

    rp = sub.add_parser("reject", help="suppress an equivalence")
    rp.add_argument("part")
    rp.add_argument("equiv_part")

    adp = sub.add_parser("add", help="add/override an equivalence (user truth)")
    adp.add_argument("part")
    adp.add_argument("mfr")
    adp.add_argument("equiv_part")
    adp.add_argument("equiv_mfr")
    adp.add_argument("--type", dest="equiv_type", default=T_SPEC,
                     choices=sorted(_VALID_TYPES))
    adp.add_argument("--caveat", default="")
    adp.add_argument("--notes", default="")

    args = ap.parse_args(argv[1:])

    if args.cmd == "seed":
        print("seed:", seed(args.db))
    elif args.cmd == "stats":
        s = stats(args.db)
        print(f"total: {s['total']}")
        print(f"by status: {s['by_status']}")
        print(f"by type:   {s['by_type']}")
    elif args.cmd == "lookup":
        _print_lookup(args.part, args.mfr, args.db)
    elif args.cmd == "confirm":
        n = confirm(args.part, args.equiv_part, args.db)
        print(f"confirmed {n} row(s).")
    elif args.cmd == "reject":
        n = reject(args.part, args.equiv_part, args.db)
        print(f"rejected {n} row(s).")
    elif args.cmd == "add":
        n = add(args.part, args.mfr, args.equiv_part, args.equiv_mfr,
                equiv_type=args.equiv_type, caveat=args.caveat,
                notes=args.notes, db=args.db)
        print(f"added/updated {n} row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
