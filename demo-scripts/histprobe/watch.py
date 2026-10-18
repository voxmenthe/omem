"""Poll a host store while a session runs; log every row that appears or changes.

watch.py devin|opencode|agy SECONDS   (agy watches the newest conversation file)
"""
import hashlib, json, pathlib, sqlite3, sys, time

HOME = pathlib.Path.home()
host, seconds = sys.argv[1], float(sys.argv[2])

def ro(path):
    wal = pathlib.Path(str(path) + "-wal")
    return sqlite3.connect(f"file:{path}?mode={'ro' if wal.exists() else 'ro&immutable=1'}", uri=True)

def rows():
    if host == "devin":
        db = ro(HOME / ".local/share/devin/cli/sessions.db")
        sid = db.execute("select id from sessions order by created_at desc, last_activity_at desc limit 1").fetchone()[0]
        out = {f"{sid}:{r}": (n, p, json.loads(m)["role"], hashlib.sha1((m + str(md)).encode()).hexdigest()[:8], len(m))
               for r, n, p, m, md in db.execute("select row_id, node_id, parent_node_id, chat_message, metadata from message_nodes where session_id=?", (sid,))}
        out[f"{sid}:session"] = db.execute("select last_activity_at, main_chain_id, title from sessions where id=?", (sid,)).fetchone()
    elif host == "opencode":
        db = ro(HOME / ".local/share/opencode/opencode.db")
        sid = db.execute("select id from session_v2 order by time_created desc limit 1").fetchone()[0]
        out = {f"{sid}:{seq}": (t, json.loads(d).get("time"), hashlib.sha1(d.encode()).hexdigest()[:8], len(d))
               for seq, t, d in db.execute("select seq, type, data from session_message where session_id=?", (sid,))}
    else:
        conv = max((HOME / ".gemini/antigravity-cli/conversations").glob("*.db"), key=lambda p: p.stat().st_mtime)
        db = ro(conv)
        out = {f"{conv.stem[:8]}:{idx}": (st, status, hashlib.sha1(p or b"").hexdigest()[:8], len(p or b""))
               for idx, st, status, p in db.execute("select idx, step_type, status, step_payload from steps")}
        out[f"{conv.stem[:8]}:wal"] = pathlib.Path(str(conv) + "-wal").exists()
    db.close()
    return out

start = time.monotonic(); last = {}
while time.monotonic() - start < seconds:
    try: now = rows()
    except sqlite3.Error as e: now = last; print(f"{time.monotonic()-start:6.1f} error {e}")
    for k in sorted(now.keys() | last.keys()):
        if now.get(k) != last.get(k):
            print(f"{time.monotonic()-start:6.1f} {k} {last.get(k)} -> {now.get(k)}", flush=True)
    last = now
    time.sleep(0.25)
