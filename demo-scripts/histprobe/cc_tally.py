import json, sys, collections, pathlib
root = pathlib.Path(sys.argv[1])
types = collections.Counter(); blocks = collections.Counter(); roles = collections.Counter()
versions = collections.Counter(); sources = collections.Counter(); keys = collections.Counter()
bad = 0; files = 0; nojsonl_meta = 0; entry = collections.Counter(); first = collections.Counter()
for f in root.glob("*/*.jsonl"):
    if f.name.endswith(".checkpoints.jsonl"): continue
    files += 1
    for i, line in enumerate(f.open("rb")):
        try: r = json.loads(line)
        except Exception: bad += 1; continue
        types[r.get("type")] += 1
        if i == 0: first[r.get("type")] += 1
        if r.get("type") == "session": versions[r.get("version")] += 1
        for k in r: keys[(r.get("type"), k)] += 1
        m = r.get("message") or {}
        if m:
            roles[m.get("role")] += 1
            sources[(m.get("role"), (m.get("meta") or {}).get("source"))] += 1
            c = m.get("content")
            if isinstance(c, list):
                for b in c: blocks[(m.get("role"), b.get("type"))] += 1
            else: blocks[(m.get("role"), type(c).__name__)] += 1
for f in root.glob("*/*.meta.json"):
    try: entry[json.load(f.open()).get("entrypoint")] += 1
    except Exception: pass
print("files", files, "bad", bad); print("first", first); print("types", types); print("versions", versions)
print("roles", roles); print("sources", sources); print("blocks", blocks); print("entry", entry)
print("keys", sorted(keys.items()))
