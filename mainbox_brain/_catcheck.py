from mainbox_brain import catalog_lookup as c
conn = c._connect()
if conn:
    print("total products:", conn.execute("SELECT COUNT(*) FROM products").fetchone()[0])
    print("brands:", [r[0] for r in conn.execute("SELECT DISTINCT manufacturer_name FROM products").fetchall()])
else:
    print("no catalog")
