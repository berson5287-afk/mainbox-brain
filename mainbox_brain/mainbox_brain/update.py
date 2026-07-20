"""
Refresh the MaINbox Brain database from your live Outlook mailbox.

This is the ONE command you run to keep the brain current. It:
  1. Exports new sent RFQs from Outlook Sent Items
  2. Exports new vendor replies + attachments from Outlook Inbox
  3. Re-mines both into mainbox.db
  4. Prints a summary of what changed

Usage (first time — saves config so you never have to type it again):
    py -m mainbox_brain.update --db mainbox.db --months 12 ^
        --attachments quote_files ^
        --mailbox sales@americanpoweresc.com ^
        --scope both ^
        --save-config

Every run after that:
    py -m mainbox_brain.update

Scope picks which mailbox(es) to scan (Inbox + Sent of each):
    --scope personal   :: just your own mailbox
    --scope sales      :: just the shared mailbox (--mailbox address)
    --scope both       :: both
If you don't pass --scope and none is saved, it asks.

Or override anything on the fly:
    py -m mainbox_brain.update --months 3          :: only last 3 months
    py -m mainbox_brain.update --scope personal    :: this run, personal only
    py -m mainbox_brain.update --no-sent           :: skip sent/RFQ mine
    py -m mainbox_brain.update --no-replies        :: skip reply mine

Config is stored in the brain database (settings table) so it travels with
the db file. Use --show-config to see what's saved, --save-config to update.

Note: requires Outlook to be open on this machine (uses COM, same as MaINbox).
"""
from __future__ import annotations
__version__ = "0.47"

import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config helpers (stored in the brain db settings table)
# ---------------------------------------------------------------------------
CONFIG_KEY = "update_config"
CONFIG_DEFAULTS = {
    "db":          "mainbox.db",
    "months":      12,
    "attachments": "quote_files",
    "mailbox":     "",           # the SHARED mailbox address (e.g. sales@...)
    "scope":       "",           # "personal" | "sales" | "both" ("" = ask)
    "sent_json":   "sent_export.json",
    "recv_json":   "received_export.json",
}


def _load_config(db_path: str) -> dict:
    """Load saved update config from the db, merged over defaults."""
    cfg = dict(CONFIG_DEFAULTS)
    try:
        import sqlite3
        db = sqlite3.connect(db_path)
        row = db.execute("SELECT value FROM settings WHERE key=?",
                         (CONFIG_KEY,)).fetchone()
        if row:
            saved = json.loads(row[0])
            cfg.update(saved)
    except Exception:
        pass
    return cfg


def _save_config(db_path: str, cfg: dict) -> None:
    import sqlite3
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
               (CONFIG_KEY, json.dumps(cfg)))
    db.commit()
    print(f"  Config saved to {db_path}.")


def _show_config(cfg: dict) -> None:
    print("Current update config:")
    for k, v in cfg.items():
        print(f"  {k:<14} = {v!r}")


# ---------------------------------------------------------------------------
# Step runner helpers
# ---------------------------------------------------------------------------
def _hdr(msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def _run_exporter(script_path: str, args: list[str]) -> bool:
    """Run an exporter script in a subprocess; return True on success."""
    cmd = [sys.executable, str(script_path)] + args
    print("  >", " ".join(cmd))
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=False, text=True)
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"  [FAILED] exporter exited {result.returncode} ({elapsed:.1f}s)")
        return False
    print(f"  [OK] ({elapsed:.1f}s)")
    return True


def _attachment_dep_check() -> list[str]:
    """v0.47: attachment mining fails SILENTLY per-file when an extractor
    library is missing -- and with auto-refresh, the per-file notes scroll into
    a background log nobody reads. Check the libraries up front and report
    loudly so a missing dep can't quietly cost quote/PO data (like an order-ack
    PDF whose item never lands in the price db)."""
    missing = []
    for mod, pkg, kinds in (("pypdf", "pypdf", "PDF quotes/order acks"),
                            ("openpyxl", "openpyxl", "Excel quote sheets"),
                            ("docx", "python-docx", "Word quotes")):
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{pkg} not installed — {kinds} in attachments are "
                           f"NOT being read. Fix: py -m pip install {pkg}")
    return missing


def _run_miner(module: str, args: list[str]) -> bool:
    """Run a brain mining module; return True on success."""
    cmd = [sys.executable, "-m", module] + args
    print("  >", " ".join(cmd))
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=False, text=True)
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"  [FAILED] miner exited {result.returncode} ({elapsed:.1f}s)")
        return False
    print(f"  [OK] ({elapsed:.1f}s)")
    return True


def _db_stats(db_path: str) -> dict:
    try:
        import sqlite3
        db = sqlite3.connect(db_path)
        vendors = db.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
        replies = db.execute("SELECT COUNT(*) FROM reply_records").fetchone()[0]
        latest = db.execute(
            "SELECT received_at FROM reply_records "
            "ORDER BY COALESCE(received_at,'') DESC LIMIT 1"
        ).fetchone()
        latest_dt = (latest[0] or "")[:10] if latest else "?"
        return {"vendors": vendors, "replies": replies, "latest": latest_dt}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _sales_name(personal_name: str) -> str:
    """sent_export.json -> sent_sales_export.json (keep them separate)."""
    p = Path(personal_name)
    return str(p.with_name(p.stem + "_sales" + p.suffix))


def _targets_for_scope(scope: str, cfg: dict) -> list[dict]:
    """Build the list of mailboxes to process for the chosen scope.

    Each target: {label, mailbox, sent_json, recv_json}. A blank mailbox means
    the signed-in personal mailbox; a set mailbox means a shared one.
    """
    personal = {
        "label": "personal",
        "mailbox": "",
        "sent_json": cfg["sent_json"],
        "recv_json": cfg["recv_json"],
    }
    sales = {
        "label": f"shared ({cfg['mailbox']})" if cfg["mailbox"] else "shared",
        "mailbox": cfg["mailbox"],
        "sent_json": _sales_name(cfg["sent_json"]),
        "recv_json": _sales_name(cfg["recv_json"]),
    }
    if scope == "personal":
        return [personal]
    if scope == "sales":
        return [sales]
    return [personal, sales]   # both


def _resolve_scope(cfg: dict, argv: list[str]) -> str:
    """Decide which mailbox(es) to scan: CLI flag > saved config > prompt."""
    cli = _flag_value(argv, "--scope")
    if cli in {"personal", "sales", "both"}:
        return cli
    if cfg.get("scope") in {"personal", "sales", "both"}:
        return cfg["scope"]
    # not specified -> ask, if we have a terminal; else default sensibly
    if not sys.stdin.isatty():
        chosen = "both" if cfg.get("mailbox") else "personal"
        print(f"  (no scope set and not interactive — defaulting to {chosen})")
        return chosen
    print("\nWhich mailbox(es) should I update from?")
    print("  [1] Personal (your own Inbox + Sent)")
    print("  [2] Sales    (the shared mailbox's Inbox + Sent)")
    print("  [3] Both")
    while True:
        choice = input("Choose 1/2/3 (or p/s/b): ").strip().lower()
        if choice in {"1", "p", "personal"}:
            return "personal"
        if choice in {"2", "s", "sales"}:
            return "sales"
        if choice in {"3", "b", "both"}:
            return "both"
        print("  Please enter 1, 2, or 3.")


def _flag_value(argv: list[str], flag: str, default=None):
    if flag in argv:
        idx = argv.index(flag)
        return argv[idx + 1] if idx + 1 < len(argv) else default
    return default


def _process_target(cfg: dict, target: dict, root_dir: Path,
                    do_sent: bool, do_replies: bool, errors: list) -> None:
    """Run the export+mine steps for one mailbox target."""
    label = target["label"]
    mailbox = target["mailbox"]
    sent_exporter = root_dir / "export_sent_outlook.py"
    recv_exporter = root_dir / "export_replies_outlook.py"

    if do_sent:
        _hdr(f"[{label}] Export Sent Items from Outlook")
        if not sent_exporter.exists():
            print(f"  WARNING: {sent_exporter} not found — skipping.")
            errors.append(f"{label}: sent exporter not found")
        else:
            a = [str(cfg["months"]), target["sent_json"]]
            if mailbox:
                a += ["--mailbox", mailbox]
            if not _run_exporter(sent_exporter, a):
                errors.append(f"{label}: sent export failed")

        _hdr(f"[{label}] Mine sent RFQs into vendor registry")
        if not Path(target["sent_json"]).exists():
            print(f"  WARNING: {target['sent_json']} not found — skipping mine.")
            errors.append(f"{label}: {target['sent_json']} missing")
        else:
            if not _run_miner("mainbox_brain.corpus",
                              [target["sent_json"], "--db", cfg["db"]]):
                errors.append(f"{label}: sent mine failed")

    if do_replies:
        _hdr(f"[{label}] Export vendor replies + attachments from Outlook")
        if not recv_exporter.exists():
            print(f"  WARNING: {recv_exporter} not found — skipping.")
            errors.append(f"{label}: reply exporter not found")
        else:
            a = [str(cfg["months"]), target["recv_json"], "--vendors-db", cfg["db"]]
            if mailbox:
                a += ["--mailbox", mailbox]
            if cfg["attachments"]:
                a += ["--attachments", cfg["attachments"]]
            if not _run_exporter(recv_exporter, a):
                errors.append(f"{label}: reply export failed")

        _hdr(f"[{label}] Mine vendor replies into price database")
        if not Path(target["recv_json"]).exists():
            print(f"  WARNING: {target['recv_json']} not found — skipping mine.")
            errors.append(f"{label}: {target['recv_json']} missing")
        else:
            a = [target["recv_json"], "--db", cfg["db"]]
            if cfg["attachments"]:
                a += ["--attachments", cfg["attachments"]]
            if not _run_miner("mainbox_brain.reply_corpus", a):
                errors.append(f"{label}: reply mine failed")


def main() -> None:
    argv = sys.argv[1:]

    # -- resolve db path first (needed to load saved config) -----------------
    db_path = CONFIG_DEFAULTS["db"]
    if "--db" in argv:
        db_path = argv[argv.index("--db") + 1]

    cfg = _load_config(db_path)
    cfg["db"] = db_path   # command-line --db always wins

    # -- apply command-line overrides ----------------------------------------
    def _arg(flag: str, default=None):
        if flag in argv:
            idx = argv.index(flag)
            return argv[idx + 1] if idx + 1 < len(argv) else default
        return default

    for key, flag in [("months","--months"), ("attachments","--attachments"),
                      ("mailbox","--mailbox"), ("sent_json","--sent-json"),
                      ("recv_json","--recv-json")]:
        v = _arg(flag)
        if v is not None:
            cfg[key] = int(v) if key == "months" else v

    # -- meta commands --------------------------------------------------------
    if "--show-config" in argv:
        _show_config(cfg)
        return

    if "--save-config" in argv:
        # capture scope too if provided on the CLI
        cli_scope = _flag_value(argv, "--scope")
        if cli_scope in {"personal", "sales", "both"}:
            cfg["scope"] = cli_scope
        _save_config(cfg["db"], {k: v for k, v in cfg.items() if k != "db"})
        print("Run 'py -m mainbox_brain.update' (no flags) to use saved config.")
        return

    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return

    do_sent    = "--no-sent"    not in argv
    do_replies = "--no-replies" not in argv

    # -- which mailbox(es)? ---------------------------------------------------
    scope = _resolve_scope(cfg, argv)
    targets = _targets_for_scope(scope, cfg)

    # if a sales target is requested but we have no shared address, ask/skip
    if any(t["label"].startswith("shared") and not t["mailbox"] for t in targets):
        if sys.stdin.isatty():
            addr = input("\nShared mailbox address (e.g. sales@americanpoweresc.com): ").strip()
            if addr:
                cfg["mailbox"] = addr
                targets = _targets_for_scope(scope, cfg)
            else:
                print("  No shared address given — skipping the shared mailbox.")
                targets = [t for t in targets if not t["label"].startswith("shared")]
        else:
            print("  No shared mailbox address configured — skipping shared mailbox.")
            targets = [t for t in targets if not t["label"].startswith("shared")]

    root_dir = Path(__file__).resolve().parent.parent   # project root

    # v0.47: loud preflight -- a missing extractor library means attachments
    # (where vendors put the actual prices) silently contribute nothing.
    dep_warnings = _attachment_dep_check() if cfg.get("attachments") else []
    for w in dep_warnings:
        print(f"  WARNING: {w}")
    errors_seed = list(dep_warnings)

    print(f"\nMaINbox Brain — update  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"  db          : {cfg['db']}")
    print(f"  months back : {cfg['months']}")
    print(f"  scope       : {scope}  ->  {', '.join(t['label'] for t in targets)}")
    if cfg["attachments"]:
        print(f"  attachments : {cfg['attachments']}")

    before = _db_stats(cfg["db"])
    errors: list[str] = list(errors_seed)   # v0.47: dep warnings ride to the summary

    if not (do_sent or do_replies):
        print("\nNothing to do (--no-sent and --no-replies both set).")
        return

    for target in targets:
        _process_target(cfg, target, root_dir, do_sent, do_replies, errors)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    after = _db_stats(cfg["db"])
    print(f"\n{'='*60}")
    print("  UPDATE COMPLETE")
    print(f"{'='*60}")
    if before and after:
        dv = after.get("vendors",0) - before.get("vendors",0)
        dr = after.get("replies",0) - before.get("replies",0)
        print(f"  vendors      : {after.get('vendors','?')}"
              + (f"  (+{dv} new)" if dv > 0 else ""))
        print(f"  reply records: {after.get('replies','?')}"
              + (f"  (+{dr} new)" if dr > 0 else ""))
        print(f"  most recent  : {after.get('latest','?')}")
    if errors:
        print(f"\n  Warnings / errors:")
        for e in errors:
            print(f"    - {e}")
        print("\n  The above are usually Outlook-not-open or missing file issues.")
    else:
        print("\n  All steps completed successfully.")
    print()


if __name__ == "__main__":
    main()
