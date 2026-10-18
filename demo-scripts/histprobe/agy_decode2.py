import sys, sqlite3, collections, json
sys.path.insert(0, "/tmp/histprobe")
import agy_pool
from google.protobuf import json_format
from google.protobuf.message_factory import GetMessageClass
pool, _, _ = agy_pool.build(sys.argv[1])
Step = GetMessageClass(pool.FindMessageTypeByName("gemini_coder.Step"))
db = sqlite3.connect(f"file:{sys.argv[2]}?immutable=1", uri=True)
which = collections.Counter(); unk = 0
for idx, st, payload in db.execute("select idx, step_type, step_payload from steps order by idx"):
    s = Step(); s.ParseFromString(payload)
    names = tuple(f.name for f, _ in s.ListFields() if f.name not in ("type", "status", "metadata"))
    which[(st, names)] += 1
    unk += len(s.SerializeToString()) != len(payload)
print("steps whose reserialization differs in size", unk)
for k, v in sorted(which.items(), key=lambda x: -x[1]): print(v, k)
for idx in map(int, sys.argv[3].split(",")) if len(sys.argv) > 3 else []:
    (p,) = db.execute("select step_payload from steps where idx=?", (idx,)).fetchone()
    s = Step(); s.ParseFromString(p)
    d = json_format.MessageToDict(s, preserving_proto_field_name=True, descriptor_pool=pool)
    d.pop("metadata", None)
    txt = json.dumps(d, indent=1)
    print(f"=== {idx}"); print(txt[:1800])
