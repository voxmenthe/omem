"""Time per-session change-token queries on the real Devin and OpenCode stores."""
import pathlib, sqlite3, time
HOME = pathlib.Path.home()
def run(path, sql):
    t = time.perf_counter()
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute(sql).fetchall()
    db.close()
    return len(rows), round((time.perf_counter() - t) * 1000, 1)
DEVIN = HOME / ".local/share/devin/cli/sessions.db"
OPENCODE = HOME / ".local/share/opencode/opencode.db"
for _ in range(3):
    print("devin group", run(DEVIN, "select session_id, max(row_id), count(*) from message_nodes group by session_id"))
    print("devin sessions", run(DEVIN, "select id, working_directory, created_at, last_activity_at from sessions"))
    print("opencode group", run(OPENCODE, "select session_id, max(seq), count(*), max(time_updated) from session_message group by session_id"))
    print("opencode sessions", run(OPENCODE, "select id, directory, time_created, time_updated from session_v2"))
