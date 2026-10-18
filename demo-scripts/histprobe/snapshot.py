"""Snapshot per-row content hashes of the Devin, OpenCode and Antigravity stores.

`snapshot.py take NAME` writes snapshots/NAME.json; `snapshot.py diff A B` reports
rows that changed, appeared, or vanished between two snapshots. Read-only.
"""
import hashlib, json, pathlib, sqlite3, sys

HOME = pathlib.Path.home()
DEVIN = HOME / ".local/share/devin/cli/sessions.db"
OPENCODE = HOME / ".local/share/opencode/opencode.db"
AGY = HOME / ".gemini/antigravity-cli/conversations"
OUT = pathlib.Path(__file__).parent / "snapshots"

def h(*parts):
    return hashlib.sha256(json.dumps(parts, default=lambda b: b.hex()).encode()).hexdigest()[:16]

def ro(path):
    wal = path.with_name(path.name + "-wal")
    mode = "ro" if wal.exists() else "ro&immutable=1"
    return sqlite3.connect(f"file:{path}?mode={mode}", uri=True)

def take():
    snap = {"devin": {}, "opencode": {}, "agy": {}}
    db = ro(DEVIN)
    for row_id, sid, node, parent, msg, meta in db.execute(
            "select row_id, session_id, node_id, parent_node_id, chat_message, metadata from message_nodes"):
        snap["devin"][str(row_id)] = [sid, node, h(parent, msg, meta)]
    snap["devin_sessions"] = {sid: [last, main] for sid, last, main in db.execute(
        "select id, last_activity_at, main_chain_id from sessions")}
    db = ro(OPENCODE)
    for mid, sid, seq, typ, upd, data in db.execute(
            "select id, session_id, seq, type, time_updated, data from session_message"):
        snap["opencode"][mid] = [sid, seq, typ, upd, h(data)]
    for f in sorted(AGY.glob("*.db")):
        db = ro(f)
        for idx, st, status, payload in db.execute("select idx, step_type, status, step_payload from steps"):
            snap["agy"][f"{f.stem}:{idx}"] = [st, status, h(payload)]
        db.close()
    OUT.mkdir(exist_ok=True)
    (OUT / f"{sys.argv[2]}.json").write_text(json.dumps(snap))
    print({k: len(v) for k, v in snap.items()})

def diff():
    a = json.loads((OUT / f"{sys.argv[2]}.json").read_text())
    b = json.loads((OUT / f"{sys.argv[3]}.json").read_text())
    for host in ("devin", "opencode", "agy"):
        x, y = a[host], b[host]
        changed = [k for k in x.keys() & y.keys() if x[k] != y[k]]
        gone = sorted(x.keys() - y.keys())
        new = sorted(y.keys() - x.keys())
        print(f"{host}: {len(new)} new, {len(changed)} changed, {len(gone)} gone")
        for k in changed[:10]: print("  changed", k, x[k], "->", y[k])
        for k in gone[:10]: print("  gone", k, x[k])

{"take": take, "diff": diff}[sys.argv[1]]()
