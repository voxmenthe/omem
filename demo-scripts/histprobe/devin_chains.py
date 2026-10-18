"""Classify every Devin message node by the root of its chain; report sessions per root kind."""
import collections, json, sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)

def root_kind(msg):
    m = json.loads(msg)
    t = (m.get("metadata") or {}).get("telemetry") or {}
    src, op, text = t.get("source"), t.get("operation"), (m.get("content") or "").lstrip()
    if src == "sysprompt": return f"sysprompt:{op}"
    if text.startswith("<system_info>"): return "system_info"
    if text.startswith("You are Devin"): return "devin-legacy"
    if text.startswith("You are a Summarizer"): return "summarizer"
    if "Sidekick subagent" in text[:80]: return "sidekick"
    return f"{m['role']}:{src}"

roots = {}
parents = {}
kinds_by_session = collections.defaultdict(collections.Counter)
for sid, node, parent, msg in db.execute("select session_id, node_id, parent_node_id, chat_message from message_nodes order by row_id"):
    parents[(sid, node)] = parent
    if parent is None:
        roots[(sid, node)] = root_kind(msg)
def root_of(sid, node):
    seen = 0
    while parents[(sid, node)] is not None:
        node = parents[(sid, node)]; seen += 1
    return roots[(sid, node)]
rows = collections.Counter(); users = collections.Counter()
for sid, node, msg in db.execute("select session_id, node_id, chat_message from message_nodes"):
    k = root_of(sid, node)
    rows[k] += 1
    m = json.loads(msg)
    if m["role"] == "user" and (m.get("metadata") or {}).get("is_user_input") == 1:
        users[k] += 1
    kinds_by_session[sid][k] += 1
print("rows by root kind", dict(rows))
print("user-input rows by root kind", dict(users))
combo = collections.Counter(tuple(sorted(c)) for c in kinds_by_session.values())
for k, v in combo.most_common(15): print(v, k)
