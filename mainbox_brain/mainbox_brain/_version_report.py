"""MaINbox Brain version reporter (v0.51).

Called by ask.py when the user types 'version' or 'versions'.
Reports what is ACTUALLY INSTALLED (imports the live modules, reads their
__version__ attribute), so it catches stale files immediately.

Also checks the xref db for taught vendor sites and the lineage table row count
so Steve can confirm the knowledge base is current too.
"""
from __future__ import annotations
__version__ = "0.51"

import importlib, os, sqlite3, sys
from datetime import datetime

_MODULES = [
    # (label,              module path,                        expected version)
    ("intents",            "mainbox_brain.intents",            "0.49"),
    ("knowledge",          "mainbox_brain.knowledge",          "0.47"),
    ("cross_reference",    "mainbox_brain.cross_reference",    "0.47"),
    ("ask",                "mainbox_brain.ask",                "0.46"),
    ("store",              "mainbox_brain.store",              None),
    ("server",             "mainbox_brain.server",             None),
    ("update",             "mainbox_brain.update",             "0.47"),
    ("reply_corpus",       "mainbox_brain.reply_corpus",       "0.51"),
    ("attachment_miner",   "mainbox_brain.attachment_miner",   "0.51"),
    ("announcements",      "mainbox_brain.announcements",      "0.49"),
    ("export_replies",     "mainbox_brain.export_replies_outlook", "0.49"),
    ("xref_dispatch",      "mainbox_brain.xref_dispatch",      None),
    ("vendor_xref",        "mainbox_brain.vendor_xref",        None),
    ("web_research",       "mainbox_brain.web_research",       None),   # optional: needs httpx
]


def _get_ver(mod) -> str:
    return str(getattr(mod, "__version__", "?"))


def report(db_path: str = "mainbox.db") -> str:
    lines: list[str] = ["MaINbox Brain — installed versions",
                        "=" * 44]
    stale: list[str] = []
    for label, modpath, expected in _MODULES:
        try:
            mod = importlib.import_module(modpath)
            v = _get_ver(mod)
            ok = (expected is None or v == expected)
            flag = "  " if ok else "⚠ "
            lines.append(f"  {flag}{label:<22} {v}")
            if not ok:
                stale.append(f"{label} (have {v}, want {expected})")
        except Exception as e:
            err = str(e)[:50]
            lines.append(f"  ✗ {label:<22} (import error: {err})")
            if expected:
                stale.append(f"{label} (not importable)")

    # knowledge base state
    lines.append("")
    lines.append("Knowledge base")
    lines.append("-" * 44)
    try:
        from mainbox_brain import store as st_mod
        st = st_mod.Store(db_path)
        n_v = len(st.load_vendors(confident_only=False))
        n_r = len(st.load_records())
        n_rep = st.reply_count() if hasattr(st, "reply_count") else "?"
        # freshness
        try:
            age_s = max(0, datetime.now().timestamp() - os.path.getmtime(db_path))
            mins = int(age_s // 60)
            age = f"{mins} min ago" if mins < 90 else f"{age_s/3600:.1f} hr ago"
        except Exception:
            age = "unknown"
        lines.append(f"  mainbox.db         {n_v} vendors / {n_r} RFQ / "
                     f"{n_rep} reply records")
        lines.append(f"  last synced        {age}")
    except Exception as e:
        lines.append(f"  mainbox.db         (error: {e})")
    try:
        from mainbox_brain.cross_reference import DB_PATH, _MFR_ALIASES
        xdb = sqlite3.connect(DB_PATH)
        n_xref = xdb.execute("SELECT COUNT(*) FROM cross_references").fetchone()[0]
        n_lin  = xdb.execute("SELECT COUNT(*) FROM mfr_lineage").fetchone()[0]
        xdb.close()
        lines.append(f"  cross_references   {n_xref} rows, "
                     f"{n_lin} taught lineage entries")
        lines.append(f"  _MFR_ALIASES       {len(_MFR_ALIASES)} entries "
                     f"(built-in + taught)")
    except Exception:
        lines.append("  cross_references   (not connected)")
    try:
        from mainbox_brain.announcements import _conn
        adb = _conn(db_path)
        n_ann = adb.execute(
            "SELECT COUNT(*) FROM vendor_announcements").fetchone()[0]
        lines.append(f"  announcements      {n_ann} vendor notice(s) on record")
    except Exception:
        lines.append("  announcements      (table not yet created — run update once)")
    try:
        from mainbox_brain import xref_dispatch as xd
        sites = list(xd.known_sites())
        lines.append(f"  taught xref sites  {len(sites)}: {', '.join(sites)}")
    except Exception:
        lines.append("  taught xref sites  (xref_dispatch not importable)")

    lines.append("")
    if stale:
        lines.append("⚠  STALE FILES DETECTED — redeploy these:")
        for s in stale:
            lines.append(f"   • {s}")
    else:
        lines.append("✓  All versioned modules match expected versions.")
    return "\n".join(lines)


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "mainbox.db"
    print(report(db))
