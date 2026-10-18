"""Decode an Antigravity conversation's metadata tables and its first user input."""
import json, pathlib, sqlite3, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import agy_pool
from google.protobuf import json_format
from google.protobuf.message_factory import GetMessageClass
pool, _, _ = agy_pool.build(str(pathlib.Path(__file__).parent / "agy/all.fds"))
def cls(name): return GetMessageClass(pool.FindMessageTypeByName(name))
path = pathlib.Path(sys.argv[1])
wal = pathlib.Path(str(path) + "-wal")
db = sqlite3.connect(f"file:{path}?mode={'ro' if wal.exists() else 'ro&immutable=1'}", uri=True)
print("trajectory_meta", db.execute("select * from trajectory_meta").fetchall())
def dump(name, blob, keep=None):
    m = cls(name)(); m.ParseFromString(blob)
    d = json_format.MessageToDict(m, preserving_proto_field_name=True, descriptor_pool=pool)
    if keep: d = {k: v for k, v in d.items() if k in keep}
    print(name, json.dumps(d, indent=1)[:int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
dump("exa.cortex_pb.CortexTrajectoryMetadata", db.execute("select data from trajectory_metadata_blob").fetchone()[0])
for (blob,) in db.execute("select data from executor_metadata"):
    dump("exa.cortex_pb.ExecutorMetadata", blob)
(p,) = db.execute("select step_payload from steps where step_type=14 order by idx limit 1").fetchone()
s = cls("gemini_coder.Step")(); s.ParseFromString(p)
d = json_format.MessageToDict(s, preserving_proto_field_name=True, descriptor_pool=pool)
ui = d["user_input"]; ui.pop("items", None); ui["user_response"] = ui["user_response"][:80]
print("metadata", json.dumps(d["metadata"], indent=1)[:1500])
print("user_input", json.dumps(ui, indent=1)[:2500])
