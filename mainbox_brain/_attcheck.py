# _attcheck.py -- one-shot attachment pipeline diagnostic (v0.47)
# Run from the mainbox_brain folder:   py _attcheck.py
# Answers, in order: are the extractor libs installed? is attachment saving
# configured? did the Versabar order-ack PDF get saved? what did the miner
# extract from it? and what does the reply record in the db actually hold?
import os, json, sqlite3, glob

print("=" * 62)
print("1) Extractor libraries")
for mod, pkg in (("pypdf", "pypdf"), ("openpyxl", "openpyxl"),
                 ("docx", "python-docx")):
    try:
        __import__(mod)
        print(f"   OK       {pkg}")
    except Exception:
        print(f"   MISSING  {pkg}   -> py -m pip install {pkg}")

print("=" * 62)
print("2) Update config (attachments dir)")
cfg = {}
try:
    db = sqlite3.connect("mainbox.db")
    row = db.execute("SELECT value FROM settings WHERE key='update_config'").fetchone()
    cfg = json.loads(row[0]) if row else {}
    db.close()
except Exception as e:
    print("   (couldn't read settings:", e, ")")
att = cfg.get("attachments", "quote_files (default)")
print(f"   attachments = {att!r}")

print("=" * 62)
print("3) Saved attachment files (newest 8)")
adir = cfg.get("attachments") or "quote_files"
files = sorted(glob.glob(os.path.join(adir, "*")), key=os.path.getmtime,
               reverse=True) if os.path.isdir(adir) else []
if not files:
    print(f"   NONE — folder {adir!r} missing or empty. The exporter isn't "
          "saving attachments; that's the break.")
for f in files[:8]:
    print(f"   {os.path.getsize(f)//1024:>5} KB  {os.path.basename(f)}")
ack = [f for f in files if "1449694" in f or "P000020541" in f]
print(f"   Versabar order-ack present: {'YES -> ' + os.path.basename(ack[0]) if ack else 'NO'}")

print("=" * 62)
print("4) Miner dry-run on the order-ack (if present)")
if ack:
    try:
        from mainbox_brain.attachment_miner import mine_attachment
        facts, note = mine_attachment(ack[0], subject="RE: Purchase Order P000020541")
        print(f"   note: {note or '(clean extraction)'}")
        print(f"   facts extracted: {len(facts)}")
        for ft in facts[:6]:
            print("   -", str(ft)[:100])
        w = [ft for ft in facts if "W6138" in str(ft).upper()]
        print(f"   contains W6138AS4-US: {'YES' if w else 'NO'}")
    except Exception as e:
        print("   miner error:", e)

print("=" * 62)
print("5) The email's reply record in the db")
try:
    db = sqlite3.connect("mainbox.db"); db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT received_at, subject, items, facts FROM reply_records "
        "WHERE from_email LIKE '%wesanco-zsi%' ORDER BY received_at DESC LIMIT 3"
    ).fetchall()
    for r in rows:
        n_items = len(json.loads(r["items"] or "[]"))
        n_facts = len(json.loads(r["facts"] or "[]"))
        print(f"   [{r['received_at']}] {r['subject'][:48]!r}  items={n_items} facts={n_facts}")
    if not rows:
        print("   no wesanco-zsi reply records at all")
    db.close()
except Exception as e:
    print("   db error:", e)
print("=" * 62)
print("Send this whole output back and the break point is identified.")
