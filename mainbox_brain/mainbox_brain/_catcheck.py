from mainbox_brain import catalog_lookup as c
print("path:", repr(c.catalog_path()))
print("connected:", c.available())
conn = c._connect()
rows = conn.execute("SELECT DISTINCT manufacturer_name FROM products").fetchall() if conn else []
print("brands:", len(rows))
print("topaz-ish:", [r[0] for r in rows if r[0] and "opaz" in r[0].lower()])
