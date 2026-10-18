"""Decode every Antigravity step with a stdlib wire-format reader; check the adapter's field map holds."""
import collections, json, pathlib, sqlite3

def fields(data):
    out, i, n = {}, 0, len(data)
    while i < n:
        key, i = varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0: value, i = varint(data, i)
        elif wire == 2:
            size, i = varint(data, i); value = data[i:i + size]; i += size
            if i > n: raise ValueError("truncated")
        elif wire == 1: value = data[i:i + 8]; i += 8
        elif wire == 5: value = data[i:i + 4]; i += 4
        else: raise ValueError(f"wire type {wire}")
        out.setdefault(number, []).append(value)
    return out

def varint(data, i):
    result = shift = 0
    while True:
        byte = data[i]; i += 1
        result |= (byte & 0x7F) << shift; shift += 7
        if byte < 0x80: return result, i

home = pathlib.Path.home() / ".gemini/antigravity-cli"
hist = {}
for line in (home / "history.jsonl").open():
    r = json.loads(line)
    if r.get("conversationId"): hist.setdefault(r["conversationId"], []).append(r["timestamp"])
kinds = collections.Counter(); generic_named = collections.Counter(); fails = 0; status = collections.Counter()
timing = []; nonfinal_closed = 0; multi = 0; in_hist = 0; total = 0
for f in sorted((home / "conversations").glob("*.db")):
    wal = f.with_name(f.name + "-wal").exists()
    db = sqlite3.connect(f"file:{f}?mode={'ro' if wal else 'ro&immutable=1'}", uri=True)
    steps = db.execute("select idx, step_type, status, step_payload from steps order by idx").fetchall()
    total += 1; users = 0; first_user_time = None
    for idx, st, stat, payload in steps:
        try: s = fields(payload)
        except (ValueError, IndexError): fails += 1; continue
        kinds[st] += 1; status[stat] += 1
        if not wal and stat not in (3, 4, 5, 6, 7, 12): nonfinal_closed += 1
        meta = fields(s[5][0]) if 5 in s else {}
        if st == 132: generic_named[(4 in meta, 140 in s)] += 1
        if st == 14:
            users += 1
            if first_user_time is None and 1 in meta: first_user_time = fields(meta[1][0]).get(1, [0])[0] * 1000
    multi += users >= 2
    if f.stem in hist:
        in_hist += 1
        if first_user_time: timing.append(min(hist[f.stem]) - first_user_time)
print("conversations", total, "decode failures", fails, "in history", in_hist, "multi-prompt", multi)
print("step types", sorted(kinds.items()))
print("statuses", sorted(status.items()), "non-final steps in closed conversations", nonfinal_closed)
print("generic (has metadata.tool_call, has generic field)", generic_named)
timing.sort(); print("history ts - first user step created (ms): min", timing[0], "median", timing[len(timing)//2], "max", timing[-1])
