"""
Data models for the brain. Plain dataclasses, no ORM, no magic.

These are the nouns that flow through the system:

    QuoteRequest  -- what the user asked for, parsed into LineItems
      LineItem    -- one product + quantity + inferred category
    Manufacturer  -- a brand American Power SELLS (seeded from the line card)
    Vendor        -- a supplier you BUY from (you own this list; see vendors.py)
      Contact     -- a person at a vendor
    ResolvedVendor-- a vendor matched to a request, scored, with reasons
    EmailDraft    -- a ready-to-send / ready-to-draft RFQ
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# --- Categories -------------------------------------------------------------
# Coarse buckets the parser maps products into and the resolver matches on.
# Extend freely; the catalog and keyword map (catalog.py) drive everything.
class Category:
    """v0.11: categories are the FINE ids from material_rules.json (42 of them,
    e.g. conduit_emt, fittings_rigid, circuit_breakers) grouped into 9 groups.
    The constants below are kept for code that checks a coarse family; use
    Category.group_of(cat) to compare at group level."""
    MC_CABLE = "mc_cable"
    BUILDING_WIRE = "building_wire"
    WIRE_CABLE = "wire_cable"          # group id: any wire/cable
    CONDUIT = "raceway"                # group id (legacy value "conduit" still mapped)
    FITTINGS = "fittings"              # group id
    GEAR = "gear"                      # group id: panels, breakers, switchgear, meter sockets
    TRANSFORMER = "transformers"
    BOXES = "boxes_enclosures"         # group id
    LIGHTING = "lighting"              # group id
    UNKNOWN = "unknown"

    @staticmethod
    def group_of(category: str) -> str:
        from . import material
        return material.group_of(category)


@dataclass
class Contact:
    name: str
    email: str
    title: Optional[str] = None
    phone: Optional[str] = None


@dataclass
class Vendor:
    """A supplier you source from. NOT the same as a line-card brand."""
    vendor_id: str
    name: str
    contacts: list[Contact] = field(default_factory=list)
    # Manufacturer names (must match keys in catalog.MANUFACTURERS) this
    # supplier can quote. This is the join that powers resolution.
    lines: set[str] = field(default_factory=set)
    notes: str = ""

    @property
    def primary_contact(self) -> Optional[Contact]:
        return self.contacts[0] if self.contacts else None


@dataclass
class Manufacturer:
    name: str
    categories: set[str] = field(default_factory=set)


@dataclass
class LineItem:
    raw: str
    product_text: str
    quantity: Optional[float] = None
    unit: Optional[str] = None
    category: str = Category.UNKNOWN
    spec: dict = field(default_factory=dict)   # e.g. {"awg": "12", "conductors": "2"}

    def describe(self) -> str:
        qty = ""
        if self.quantity is not None:
            q = int(self.quantity) if float(self.quantity).is_integer() else self.quantity
            qty = f"{q:,}{self.unit or ''} ".replace(",", ",")
        return f"{qty}{self.product_text}".strip()


@dataclass
class QuoteRequest:
    raw_text: str
    items: list[LineItem] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ResolvedVendor:
    vendor: Vendor
    contact: Optional[Contact]
    matched_manufacturers: list[str] = field(default_factory=list)
    covered_items: list[str] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class EmailDraft:
    to: str
    subject: str
    body: str
    to_name: Optional[str] = None


@dataclass
class SentRecord:
    """A simplified record of a past outgoing RFQ, mined from Sent Items.

    The empirical signal: who you've ACTUALLY quoted for a given category.
    In production this comes from Microsoft Graph; here it comes from the stub.
    """
    to_email: str
    vendor_id: Optional[str]
    categories: set[str] = field(default_factory=set)
    when: Optional[datetime] = None
    items: list[str] = field(default_factory=list)   # parsed product texts
