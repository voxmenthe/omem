"""Find metadata fields that separate multi-turn (surely interactive) Antigravity conversations from single-prompt ones."""
import collections, json, pathlib, sqlite3, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import agy_pool
from google.protobuf import json_format
from google.protobuf.message_factory import GetMessageClass
pool, _, _ = agy_pool.build(str(pathlib.Path(__file__).parent / "agy/all.fds"))
Step = GetMessageClass(pool.FindMessageTypeByName("gemini_coder.Step"))
Meta = GetMessageClass(pool.FindMessageTypeByName("exa.cortex_pb.CortexTrajectoryMetadata"))
def flat(d, prefix=""):
    out = set()
    if isinstance(d, dict):
        for k, v in d.items(): out |= flat(v, f"{prefix}.{k}")
    elif isinstance(d, list):
        for v in d[:3]: out |= flat(v, prefix + "[]")
    else:
        out.add(f"{prefix}={d}" if isinstance(d, (bool, int)) or (isinstance(d, str) and len(d) < 40 and not d.startswith("file:")) else prefix)
    return out
groups = {"multi": collections.Counter(), "single": collections.Counter()}; n = collections.Counter()
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.db")):
    db = sqlite3.connect(f"file:{f}?mode=ro&immutable=1", uri=True)
    users = db.execute("select step_payload from steps where step_type=14 order by idx").fetchall()
    if not users: continue
    g = "multi" if len(users) > 1 else "single"; n[g] += 1
    keys = set()
    tm = db.execute("select * from trajectory_meta").fetchone(); keys.add(f"tmeta.source={tm[3]}")
    m = Meta(); m.ParseFromString(db.execute("select data from trajectory_metadata_blob").fetchone()[0])
    d = json_format.MessageToDict(m, preserving_proto_field_name=True, descriptor_pool=pool); d.pop("mendel_experiment_ids", None)
    keys |= {"meta" + k for k in flat(d)}
    s = Step(); s.ParseFromString(users[0][0])
    d = json_format.MessageToDict(s, preserving_proto_field_name=True, descriptor_pool=pool)
    ui = d["user_input"]; ui.pop("items", None); ui.pop("user_response", None)
    keys |= {"ui" + k for k in flat(ui)} | {"stepmeta" + k for k in flat(d.get("metadata", {}))}
    groups[g].update(keys)
print(n)
rows = []
for k in groups["multi"].keys() | groups["single"].keys():
    a, b = groups["multi"][k] / n["multi"], groups["single"][k] / n["single"]
    if abs(a - b) > 0.5: rows.append((round(a - b, 2), round(a, 2), round(b, 2), k))
for r in sorted(rows)[:15] + sorted(rows)[-15:]: print(r)
