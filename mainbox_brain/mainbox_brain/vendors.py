"""
Vendor registry -- the suppliers you BUY from, and who to contact.

>>> THIS IS THE FILE YOU OWN AND MAINTAIN. <<<

The line card (catalog.py) lists brands American Power *sells*. It does NOT
tell you which suppliers quote you, or their contacts. That knowledge lives
here, and in your Sent Items (see graph_client.py -> recent_sent), which is
the empirical ground truth the resolver leans on.

The entries below are seeded from the example you gave (Mark at Brazil, Thea
at PipeAndWire) plus one gear supplier, all as PLACEHOLDERS. Replace `lines`
with the manufacturer names (matching catalog.MANUFACTURERS keys) each
supplier can actually quote, and fix the contact emails.
"""
from __future__ import annotations
from .models import Vendor, Contact

VENDORS: dict[str, Vendor] = {
    "brazil": Vendor(
        vendor_id="brazil",
        name="Brazil Electrical Supply",      # placeholder name
        contacts=[Contact(name="Mark", email="mark@brazil-example.com", title="Sales")],
        lines={
            "Southwire Company",
            "Encore Wire",
            "Northern Cables, Inc.",
            "Service Wire Co., Inc.",
        },
        notes="Strong on MC and building wire. (placeholder data)",
    ),
    "pipeandwire": Vendor(
        vendor_id="pipeandwire",
        name="PipeAndWire",                    # placeholder name
        contacts=[Contact(name="Thea", email="thea@pipeandwire-example.com", title="Inside Sales")],
        lines={
            "Southwire Company",
            "Priority Wire & Cable",
            "Cantex",
            "Wheatland Tube LLC",
            "American Fittings Corporation",
        },
        notes="Wire + conduit + fittings. (placeholder data)",
    ),
    "gear_co": Vendor(
        vendor_id="gear_co",
        name="Keystone Gear Distributors",     # placeholder name
        contacts=[Contact(name="Dana", email="dana@gearco-example.com", title="Quotes")],
        lines={
            "Siemens Industry",
            "ABB, Inc. Power & Product",
            "Milbank Manufacturing Company",
            "North American Breaker Co. LLC",
        },
        notes="Panels, breakers, meter sockets. (placeholder data)",
    ),
}


def get_vendor(vendor_id: str) -> Vendor | None:
    return VENDORS.get(vendor_id)


def all_vendors() -> list[Vendor]:
    return list(VENDORS.values())


def vendors_carrying_manufacturer(manufacturer: str) -> list[Vendor]:
    return [v for v in VENDORS.values() if manufacturer in v.lines]


def find_vendor_by_name(fragment: str) -> Vendor | None:
    """Loose lookup so 'Mark', 'brazil', 'thea' etc. resolve to a vendor."""
    f = fragment.strip().lower()
    if not f:
        return None
    for v in VENDORS.values():
        if f in v.vendor_id or f in v.name.lower():
            return v
        for c in v.contacts:
            if f in c.name.lower():
                return v
    return None
