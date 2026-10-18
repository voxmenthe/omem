import sqlite3, sys, collections, pathlib
types = collections.Counter(); status = collections.Counter(); fmt = collections.Counter(); tt = collections.Counter()
n = sub = parents = errs = wal = 0
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.db")):
    if f.with_name(f.name + "-wal").exists(): wal += 1
    try:
        db = sqlite3.connect(f"file:{f}?immutable=1", uri=True)
        for st, s, fm, c in db.execute("select step_type, status, step_format, count(*) from steps group by 1,2,3"):
            types[st] += c; status[s] += c; fmt[fm] += c
        for t, c in db.execute("select trajectory_type, count(*) from trajectory_meta group by 1"): tt[t] += c
        sub += db.execute("select count(*) from steps where has_subtrajectory").fetchone()[0]
        parents += db.execute("select count(*) from parent_references").fetchone()[0] > 0
        n += 1; db.close()
    except Exception as e:
        errs += 1; print(f.name, e)
print("dbs", n, "errors", errs, "with -wal", wal, "with parent refs", parents, "steps with subtrajectory", sub)
print("trajectory_type", tt); print("step_format", fmt); print("status", status)
print("step_type", sorted(types.items()))
