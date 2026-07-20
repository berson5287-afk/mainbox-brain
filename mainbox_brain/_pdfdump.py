# _pdfdump.py -- show the raw text extracted from the Versabar order-ack, so the
# parser can be written to the ACTUAL layout instead of guessed.
# Run from the folder where you run ask (the one whose _attcheck showed the file):
#     py _pdfdump.py
import glob, os, sqlite3, json

cfg = {}
try:
    db = sqlite3.connect("mainbox.db")
    row = db.execute("SELECT value FROM settings WHERE key='update_config'").fetchone()
    cfg = json.loads(row[0]) if row else {}
    db.close()
except Exception:
    pass
adir = cfg.get("attachments") or "quote_files"

cands = glob.glob(os.path.join(adir, "*1449694*")) or \
        glob.glob(os.path.join(adir, "*P000020541*"))
if not cands:
    print(f"Order-ack PDF not found in {adir!r}. Newest PDFs there:")
    for f in sorted(glob.glob(os.path.join(adir, "*.pdf")),
                    key=os.path.getmtime, reverse=True)[:8]:
        print("  ", os.path.basename(f))
    raise SystemExit

path = cands[0]
print("FILE:", os.path.basename(path))
print("SIZE:", os.path.getsize(path), "bytes")
print("=" * 70)

try:
    from pypdf import PdfReader
    r = PdfReader(path)
    print(f"pages: {len(r.pages)}")
    for i, pg in enumerate(r.pages):
        txt = pg.extract_text() or ""
        print(f"\n----- PAGE {i+1}: {len(txt)} chars extracted -----")
        # numbered lines so we can point at exactly where part# and price sit
        for ln, line in enumerate(txt.splitlines()):
            if line.strip():
                print(f"{ln:>3}| {line}")
        if len(txt.strip()) < 40:
            print("   (little/no text -> this page is likely a SCANNED IMAGE; "
                  "needs OCR, not pypdf)")
except Exception as e:
    print("pypdf error:", e)

print("=" * 70)
print("Copy everything from FILE: down and paste it back.")
print("If pages show ~0 chars, the ack is a scanned image and needs the OCR path.")
