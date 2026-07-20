"""
Curated vendor knowledge -- researched, not mined.

The miner infers categories from what you happened to TYPE in emails; this
file records what each vendor ACTUALLY sells, from web research (line cards,
company sites) plus signature blocks found in your own mail threads.

Sources of note:
  - Brazill Brothers: full-line manufacturers rep (Southwire, nVent CADDY,
    Legrand, Milbank, Appleton, Atkore, MB Cable) + Brazill Lite Tech lighting
  - Thea Enterprises: electrical-construction rep (Hubbell RACO/Bell/Wiegmann,
    Penn-Union, Plastibond, Robroy, Remee, Vanguard) + lighting & controls
  - Gumersell Cashdan: Unistrut rep (strut/supports)
  - Versabar Corp: Wesanco-ZSI / Ideal Tridon (strut, hangers, clamps)
  - All Current: distributor-only redistributor (fittings, hazloc, enclosures)
  - Lex Associates: Western Tube, Kraloy, IPEX, Picoma (conduit + fittings)

Apply (writes corrections + updates an existing db in place):
    py -m mainbox_brain.curated apply
    py -m mainbox_brain.curated apply --db path.db
    py -m mainbox_brain.curated show           # preview without touching anything

Entries absent here keep their mined categories untouched. Mined categories
are EVIDENCE of what you buy; curation removes noise, it doesn't punish a
vendor for a category research didn't mention -- where research and mining
disagree, this file errs toward the union for distributors/reps and toward
research for single-line manufacturers.
"""
from __future__ import annotations
import sys

# category vocabulary (strings; matches models.Category plus 'tools')
W, WC, MC = "building_wire", "wire_cable", "mc_cable"
CO, FI, GE = "conduit", "fittings", "gear"
TR, BX, LI, TO = "transformer", "boxes_enclosures", "lighting", "tools"

#: vendor_id -> (display name, categories) -- None categories = keep mined
CURATED: dict[str, tuple[str, list[str] | None]] = {
    # -- the big reps/distributors (researched) -------------------------------
    "brazill":        ("Brazill Brothers & Assoc.", [W, WC, MC, FI, CO, GE, BX, LI]),
    "brazillbrothers":("Brazill Brothers & Assoc. (alt domain)", [W, WC, MC, FI, CO, GE, BX, LI]),
    "theaenterprises":("Thea Enterprises", [FI, BX, CO, WC, W, LI, TR]),
    "cooper-electric":("Cooper Electric Supply", [W, WC, MC, CO, FI, GE, BX, TR, LI]),
    "globeelec":      ("Globe Electric Supply", [W, WC, MC, CO, FI, GE, BX]),
    "warshawinc":     ("Warshaw Inc.", [W, WC, MC, CO, FI, GE, BX, LI]),
    "gumcash":        ("Gumersell Cashdan (Unistrut)", [FI, CO]),
    "versabar":       ("Versabar Corp (Wesanco-ZSI)", [FI]),
    "wesanco-zsi":    ("Wesanco-ZSI", [FI]),
    "lexnj":          ("Lex Associates", [CO, FI]),
    "allcurrent":     ("All Current Electrical Sales", [FI, CO, GE, BX]),
    "rowe-sales":     ("Rowe Sales", [CO, FI, MC, WC]),
    "tri-techsales":  ("Tri-Tech Sales Associates", [CO, FI, WC]),
    "daminsales":     ("Damin Sales", [LI, W, WC, BX]),
    "ammoelectric":   ("Ammo International", [BX, CO, FI, MC]),
    "lasallereps":    ("LaSalle Representatives", None),
    "sepco-usa":      ("SEPCO", [CO, FI]),

    # -- wire & cable ----------------------------------------------------------
    "omnicable":      ("Omni Cable", [WC, W, MC]),
    "prioritywire":   ("Priority Wire & Cable", [WC, W, MC]),
    "azwireandcable": ("AZ Wire & Cable", [WC, W, MC]),
    "okonite":        ("The Okonite Co.", [WC]),
    "radix-wire":     ("Radix Wire & Cable", [WC]),
    "remee":          ("Remee Wire & Cable", [WC, W]),
    "kristechwire":   ("Kris-Tech Wire", [WC, W]),
    "lakecable":      ("Lake Cable", [WC]),
    "twcablellc":     ("TW Cable", [WC]),
    "champwire":      ("Champion Wire & Cable", [WC]),
    "bizzbrin":       (None, [WC, W]),          # King Wire connection; name unknown
    "cableandconnections": ("Cable & Connections", [WC, FI]),

    # -- fittings / supports / fasteners / grounding ---------------------------
    "allfasteners":   ("AllFasteners USA", [FI]),
    "greaves-usa":    ("Greaves USA", [FI]),
    "burndy":         ("Burndy", [FI]),
    "idealtridon":    ("Ideal Tridon", [FI]),
    "harger":         ("Harger Lightning & Grounding", [FI]),
    "stifirestop":    ("STI Firestop", [FI]),
    "nova-anchor":    ("Nova Anchor", [FI]),
    "mason-ind":      ("Mason Industries", [FI]),
    "mutualscrew":    ("Mutual Screw & Supply", [FI]),
    "amftgs":         ("American Fittings", [FI]),
    "mulberrymetal":  ("Mulberry Metal Products", [BX, FI]),

    # -- conduit ----------------------------------------------------------------
    "orbia":          ("Orbia (Dura-Line)", [CO]),
    "duraline":       ("Dura-Line", [CO]),

    # -- gear / fuses / devices ---------------------------------------------------
    "selectainc":     ("Selecta Products", [GE, FI]),
    "fuseco":         ("Fuseco", [GE]),
    "leviton":        ("Leviton", [LI, GE]),
    "hubbell":        ("Hubbell", [GE, FI, LI, BX]),

    # -- lighting -----------------------------------------------------------------
    "gothamlgt":      ("Gotham Lighting Supply", [LI]),
    "barronltg":      ("Barron Lighting Group", [LI]),
    "encorelighting": ("Encore Lighting", [LI]),
    "electriclighting": (None, [LI]),
    "illuminationsinc": ("Illuminations", [LI]),
    "lutron":         ("Lutron", [LI]),
    "satco":          ("SATCO", [LI]),
    "ledvance":       ("LEDVANCE", [LI]),
    "rablighting":    ("RAB Lighting", [LI]),

    # -- boxes / enclosures --------------------------------------------------------
    "fsrinc":         ("FSR Inc.", [BX, FI]),
    "oldcastle":      ("Oldcastle Infrastructure", [BX]),

    # -- tools ------------------------------------------------------------------------
    "sbdinc":         ("Stanley Black & Decker", [TO]),
    "kleintools":     ("Klein Tools", [TO]),
}

#: vendors that research showed are NOT vendors at all
SUGGESTED_EXCLUSIONS: dict[str, str] = {
    "forestelectric": "Forest Electric = EMCOR electrical CONTRACTOR (a customer)",
    "orders": "orders@canals.ai = procurement portal, not a vendor contact",
    "allbrightelectric": "flagged by Steve as not a vendor",
}


def apply(db_path: str) -> None:
    import json
    from .store import Store
    store = Store(db_path)

    # 1. record as corrections (so future re-mines re-apply automatically)
    existing = {(k, key) for _i, k, key, _v, _t in store.corrections()}
    n_new = 0
    for vid, (name, cats) in CURATED.items():
        if name and ("rename", vid) not in existing:
            store.add_correction("rename", vid, name)
            n_new += 1
        if cats is not None and ("categories", vid) not in existing:
            store.add_correction("categories", vid, json.dumps(sorted(cats)))
            n_new += 1
    for vid, why in SUGGESTED_EXCLUSIONS.items():
        if ("exclude", vid) not in existing:
            store.add_correction("exclude", vid)
            print(f"  excluding {vid}: {why}")
            n_new += 1

    # 2. update the already-saved registry in place (no re-mine needed)
    cur = store.db.cursor()
    updated = 0
    for vid, (name, cats) in CURATED.items():
        row = cur.execute("SELECT 1 FROM vendors WHERE vendor_id=?", (vid,)).fetchone()
        if not row:
            continue
        if name:
            cur.execute("UPDATE vendors SET name=? WHERE vendor_id=?", (name, vid))
        if cats is not None:
            cur.execute("DELETE FROM vendor_categories WHERE vendor_id=?", (vid,))
            for c in cats:
                cur.execute("INSERT OR IGNORE INTO vendor_categories VALUES (?,?)", (vid, c))
        updated += 1
    for vid in SUGGESTED_EXCLUSIONS:
        cur.execute("DELETE FROM vendors WHERE vendor_id=?", (vid,))
        cur.execute("DELETE FROM contacts WHERE vendor_id=?", (vid,))
        cur.execute("DELETE FROM vendor_categories WHERE vendor_id=?", (vid,))
        cur.execute("DELETE FROM records WHERE vendor_id=?", (vid,))
    store.db.commit()
    print(f"Recorded {n_new} new correction(s); updated {updated} stored vendor(s); "
          f"removed {len(SUGGESTED_EXCLUSIONS)} non-vendor(s).")


def main() -> None:
    args = sys.argv[1:]
    db_path = "mainbox.db"
    if "--db" in args:
        db_path = args[args.index("--db") + 1]
    if not args or args[0] == "show":
        for vid, (name, cats) in sorted(CURATED.items()):
            print(f"  {vid:<20} {name or '(keep mined name)':<38} "
                  f"{cats if cats is not None else '(keep mined categories)'}")
        print("\nSuggested exclusions:")
        for vid, why in SUGGESTED_EXCLUSIONS.items():
            print(f"  {vid:<20} {why}")
        return
    if args[0] == "apply":
        apply(db_path)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
