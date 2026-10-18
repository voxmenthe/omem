import sqlite3, sys
def varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7f) << s; s += 7
        if x < 0x80: return r, i
def parse(b):
    i, out = 0, []
    while i < len(b):
        k, i = varint(b, i); f, w = k >> 3, k & 7
        if f == 0: raise ValueError
        if w == 0: v, i = varint(b, i)
        elif w == 1: v = b[i:i+8]; i += 8
        elif w == 5: v = b[i:i+4]; i += 4
        elif w == 2:
            n, i = varint(b, i); v = b[i:i+n]; i += n
            if i > len(b): raise ValueError
        else: raise ValueError
        out.append((f, w, v))
    return out
def show(b, path, depth, lim):
    try: fields = parse(b)
    except Exception: fields = None
    if fields is None or depth > 8:
        try: s = b.decode()
        except UnicodeDecodeError: print(f"{path} <{len(b)} bytes binary>"); return
        print(f"{path} = {s[:lim]!r}"); return
    for f, w, v in fields:
        p = f"{path}.{f}"
        if w == 2:
            try:
                s = v.decode()
                if s.isprintable() or "\n" in s:
                    print(f"{p} = {s[:lim]!r}"); continue
            except UnicodeDecodeError: pass
            show(v, p, depth+1, lim)
        elif w == 0: print(f"{p} = {v}")
db = sqlite3.connect(sys.argv[1]); lim = int(sys.argv[3]) if len(sys.argv) > 3 else 160
for idx, st, p in db.execute(f"select idx, step_type, step_payload from steps where idx in ({sys.argv[2]}) order by idx"):
    print(f"=== step {idx} type {st}"); show(p, "", 0, lim)
