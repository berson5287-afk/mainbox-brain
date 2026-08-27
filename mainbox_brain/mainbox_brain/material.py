"""
material -- the Brain's material typing, backed by the mined rules file.

v0.11 (2026-08-27): built from Steve's own data (41K RFQ/reply item lines,
34.5K catalog SKUs, 10.9K sent messages) by the taxonomy research workflow:
42 fine categories in 9 groups, 479 brand aliases, and a curated vendor map
(which vendor gets asked for which material, with counts). The regex engine
lives in material_classify.py; material_rules.json is the data. Both are
shared byte-for-byte with the MaINbox desktop app (mainbox_material.py).

Public surface (everything else in the Brain goes through these):
    classify_category(text) -> fine category id, or a group id when only a
                               coarse guess is possible, or Category.UNKNOWN
    group_of(category)      -> the group ("wire_cable", "raceway", ...)
    vendor_categories(vendor_id) -> {category: rfq_line_count} from the
                               curated vendor map (vendor_id = domain label)
"""
from __future__ import annotations
import logging

log = logging.getLogger("mainbox_brain.material")
_CLS = None
_VENDOR_MAP: dict[str, dict] = {}
_GROUPS: dict[str, str] = {}
_LOAD_ERR = ""

try:
    from . import material_classify as _CLS
    _GROUPS = dict(_CLS.GROUP_OF)
    for dom, rec in (_CLS._R.get("vendor_map") or {}).items():
        vid = dom.split(".", 1)[0].lower()
        cats = {k: int(v) for k, v in (rec.get("categories") or {}).items() if int(v or 0) > 0}
        cur = _VENDOR_MAP.setdefault(vid, {"name": rec.get("name", ""), "categories": {},
                                           "confidence": rec.get("confidence", "")})
        for k, v in cats.items():
            cur["categories"][k] = cur["categories"].get(k, 0) + v
except Exception as e:  # noqa: BLE001
    _LOAD_ERR = f"{type(e).__name__}: {e}"
    log.warning("material rules unavailable (%s) -- keyword fallback in use", _LOAD_ERR)

GROUPS = ("wire_cable", "raceway", "fittings", "boxes_enclosures", "gear",
          "lighting", "devices", "connectors_grounding", "hardware_misc")

# legacy coarse ids -> group, so old stored records keep matching sensibly
_LEGACY_GROUP = {"mc_cable": "wire_cable", "building_wire": "wire_cable",
                 "wire_cable": "wire_cable", "conduit": "raceway", "fittings": "fittings",
                 "gear": "gear", "transformer": "gear", "boxes_enclosures": "boxes_enclosures",
                 "lighting": "lighting"}


def available() -> bool:
    return _CLS is not None


def classify(text: str):
    """(category_or_group_or_None, score, reasons) straight from the engine."""
    if _CLS is None:
        return None, 0.0, [_LOAD_ERR]
    try:
        return _CLS.classify(text or "")
    except Exception as e:  # noqa: BLE001
        return None, 0.0, [f"classify error: {e}"]


def classify_category(text: str) -> str:
    cat, _score, _why = classify(text)
    if not cat:
        return "unknown"
    if cat.startswith("group:"):
        return cat[6:] or "unknown"
    return cat


def group_of(category: str) -> str:
    c = (category or "").lower()
    if c in _GROUPS:
        return _GROUPS[c]
    if c in GROUPS:
        return c
    return _LEGACY_GROUP.get(c, "")


def categories_in_group(group: str) -> set[str]:
    return {c for c, g in _GROUPS.items() if g == group}


def vendor_categories(vendor_id: str) -> dict[str, int]:
    rec = _VENDOR_MAP.get((vendor_id or "").lower())
    return dict(rec["categories"]) if rec else {}


def vendor_map() -> dict[str, dict]:
    return _VENDOR_MAP
