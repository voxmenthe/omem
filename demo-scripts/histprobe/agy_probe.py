import sqlite3, sys, math, collections, re
db = sqlite3.connect(sys.argv[1])
def ent(b):
    if not b: return 0
    c = collections.Counter(b); n = len(b)
    return -sum(v/n*math.log2(v/n) for v in c.values())
print("step_format", db.execute("select step_format, count(*) from steps group by 1").fetchall())
print("step_type", db.execute("select step_type, count(*) from steps group by 1").fetchall())
print("status", db.execute("select status, count(*) from steps group by 1").fetchall())
for idx, st, fmt, p, m in db.execute("select idx, step_type, step_format, step_payload, metadata from steps order by idx limit 6"):
    p = p or b""
    print(idx, st, fmt, len(p), round(ent(p), 2), p[:12].hex(), re.findall(rb"[ -~\n]{24,}", p)[:2])
blob = db.execute("select data from trajectory_metadata_blob").fetchone()[0]
print("meta", len(blob), round(ent(blob),2), re.findall(rb"[ -~]{12,}", blob)[:6])
