"""
Manufacturer catalog -- the taxonomy layer.

Seeded from the American Power line card (Aug 2024). These are the brands the
company SELLS. The resolver uses this to turn a product spec into a set of
manufacturers, then asks vendors.py which suppliers carry those lines.

IMPORTANT distinction (this tripped us up once, worth stating in code):
    - This file = brands American Power sells.  (from the line card)
    - vendors.py = suppliers you buy from.      (you own / maintain this)
A brand here is only a *vendor* too if you happen to source it direct.

The card is heavily lighting; the electrical-distribution lines (wire/cable,
conduit, fittings, gear, transformers, boxes) are tagged in detail because
that's where the quote-routing demand lives. Lighting brands are bucketed.

You can replace/augment this seed by bulk-loading from your existing ~34,500
product SQLite DB (the six-vendor catalog expansion) -- that already carries
per-SKU vendor provenance, which is an even stronger signal than brand tags.
"""
from __future__ import annotations
from .models import Category, Manufacturer

C = Category

# --- Electrical-distribution lines (detailed) -------------------------------
_DISTRIBUTION: dict[str, set[str]] = {
    # Wire & cable
    "Southwire Company": {C.MC_CABLE, C.BUILDING_WIRE, C.WIRE_CABLE},
    "Encore Wire": {C.MC_CABLE, C.BUILDING_WIRE, C.WIRE_CABLE},
    "Service Wire Co., Inc.": {C.MC_CABLE, C.BUILDING_WIRE, C.WIRE_CABLE},
    "Houston Wire & Cable": {C.WIRE_CABLE, C.BUILDING_WIRE},
    "Priority Wire & Cable": {C.BUILDING_WIRE, C.WIRE_CABLE},
    "Colonial Wire & Cable Co.": {C.WIRE_CABLE, C.BUILDING_WIRE},
    "The Okonite Co.": {C.WIRE_CABLE},
    "Prysmian Cables": {C.WIRE_CABLE, C.BUILDING_WIRE},
    "LS Cable Superior Essex": {C.WIRE_CABLE, C.BUILDING_WIRE},
    "Northern Cables, Inc.": {C.MC_CABLE, C.WIRE_CABLE},
    "Omni Cable Corp": {C.WIRE_CABLE, C.BUILDING_WIRE},
    "Helukabel USA": {C.WIRE_CABLE},
    "King Wire, Inc.": {C.BUILDING_WIRE, C.WIRE_CABLE},
    "NSI Industries LLC": {C.FITTINGS, C.WIRE_CABLE},

    # Conduit & raceway
    "Cantex": {C.CONDUIT},
    "Wheatland Tube LLC": {C.CONDUIT},
    "Western Tube & Conduit": {C.CONDUIT},
    "Prime Conduit, Inc.": {C.CONDUIT},
    "Conduit Pipe Products": {C.CONDUIT},
    "National Pipe & Plastics, Inc.": {C.CONDUIT},
    "IPEX USA LLC": {C.CONDUIT},
    "Electri-Flex": {C.CONDUIT},
    "Anamet Electrical, Inc.": {C.CONDUIT, C.FITTINGS},
    "Atkore International": {C.CONDUIT, C.FITTINGS},
    "Multi Fittings / Kraloy": {C.CONDUIT, C.FITTINGS},

    # Fittings
    "American Fittings Corporation": {C.FITTINGS},
    "Bridgeport Fittings": {C.FITTINGS},
    "Arlington Industries": {C.FITTINGS, C.BOXES},
    "Topaz Electric": {C.FITTINGS},
    "L H Dottie Company": {C.FITTINGS},

    # Gear: panels, breakers, switchgear, meter sockets
    "Siemens Industry": {C.GEAR},
    "ABB, Inc. Power & Product": {C.GEAR},
    "Hubbell Electrical Products": {C.GEAR, C.FITTINGS},
    "Hubbell Power Systems": {C.GEAR},
    "Milbank Manufacturing Company": {C.GEAR},
    "East Coast Panelboard": {C.GEAR},
    "North American Breaker Co. LLC": {C.GEAR},
    "Breaker Brokers, Inc.": {C.GEAR},
    "MERSEN (Ferraz Shawmut)": {C.GEAR},
    "G & W Electric Company": {C.GEAR},

    # Transformers
    "Hammond Power Solutions": {C.TRANSFORMER},
    "MGM Transformer Co.": {C.TRANSFORMER},
    "Maddox Transformer": {C.TRANSFORMER},
    "Vantran Transformers": {C.TRANSFORMER},

    # Boxes & enclosures
    "E-Box Electrical Box Enclosures": {C.BOXES},
    "NB Electrical Enclosure, Inc.": {C.BOXES},
    "Penn Panel & Box Co.": {C.BOXES},
    "Allied Moulded Products, Inc.": {C.BOXES},
}

# --- Lighting (bucketed sample from the card) -------------------------------
# The card lists ~150 lighting brands; a representative slice is seeded so the
# LIGHTING category resolves. Add the rest as needed (or bulk-load from SQLite).
_LIGHTING_BRANDS = [
    "Acuity Brands", "Kichler Lighting LLC", "Cree Lighting", "RAB Lighting, Inc.",
    "Lutron Electronics Co., Inc.", "Signify Co. (Philip LG)", "Leviton Manufacturing",
    "Hubbell Wiring Systems", "Litecontrol Corporation", "Focal Point LLC",
    "WAC Lighting Co.", "Visual Comfort & Co.", "Keystone Technologies",
    "SATCO Products, Inc.", "Current, Powered by GE", "Nora Lighting",
]

MANUFACTURERS: dict[str, Manufacturer] = {}
for _name, _cats in _DISTRIBUTION.items():
    MANUFACTURERS[_name] = Manufacturer(_name, set(_cats))
for _name in _LIGHTING_BRANDS:
    MANUFACTURERS[_name] = Manufacturer(_name, {C.LIGHTING})


# --- Keyword -> category map (drives the regex parser) ----------------------
# Order matters: more specific tokens first. Matched against product text.
KEYWORD_CATEGORY: list[tuple[str, str]] = [
    ("mc", C.MC_CABLE),
    ("ac cable", C.MC_CABLE),
    ("armored", C.MC_CABLE),
    ("thhn", C.BUILDING_WIRE),
    ("thwn", C.BUILDING_WIRE),
    ("xhhw", C.BUILDING_WIRE),
    ("nm-b", C.BUILDING_WIRE),
    ("romex", C.BUILDING_WIRE),
    ("building wire", C.BUILDING_WIRE),
    ("thermostat wire", C.WIRE_CABLE),
    ("tray cable", C.WIRE_CABLE),
    ("so cord", C.WIRE_CABLE),
    ("soow", C.WIRE_CABLE),
    ("seoow", C.WIRE_CABLE),
    ("sjoow", C.WIRE_CABLE),
    ("cord", C.WIRE_CABLE),
    ("use", C.WIRE_CABLE),
    ("urd", C.WIRE_CABLE),
    ("cable", C.WIRE_CABLE),
    ("wire", C.BUILDING_WIRE),
    ("emt", C.CONDUIT),
    ("rmc", C.CONDUIT),
    ("imc", C.CONDUIT),
    ("rigid", C.CONDUIT),
    ("pvc conduit", C.CONDUIT),
    ("pvc", C.CONDUIT),
    ("conduit", C.CONDUIT),
    ("flex", C.CONDUIT),
    ("connector", C.FITTINGS),
    ("coupling", C.FITTINGS),
    ("strut", C.FITTINGS),
    ("fitting", C.FITTINGS),
    ("panelboard", C.GEAR),
    ("panel", C.GEAR),
    ("breaker", C.GEAR),
    ("switchgear", C.GEAR),
    ("disconnect", C.GEAR),
    ("meter socket", C.GEAR),
    ("meter", C.GEAR),
    ("transformer", C.TRANSFORMER),
    ("xfmr", C.TRANSFORMER),
    ("enclosure", C.BOXES),
    ("junction box", C.BOXES),
    ("j-box", C.BOXES),
    ("box", C.BOXES),
    ("fixture", C.LIGHTING),
    ("luminaire", C.LIGHTING),
    ("downlight", C.LIGHTING),
    ("led", C.LIGHTING),
    ("lighting", C.LIGHTING),
    ("light", C.LIGHTING),
]


def manufacturers_for_category(category: str) -> list[str]:
    """All seeded manufacturers that make a given category."""
    return sorted(n for n, m in MANUFACTURERS.items() if category in m.categories)


def category_for_text(text: str) -> str:
    """First-pass category inference from product text (no LLM)."""
    t = text.lower()
    for token, cat in KEYWORD_CATEGORY:
        if token in t:
            return cat
    return Category.UNKNOWN


def find_manufacturer(name: str) -> Manufacturer | None:
    return MANUFACTURERS.get(name)
