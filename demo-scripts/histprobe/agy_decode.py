import sys, sqlite3, collections
from google.protobuf import descriptor_pb2, descriptor_pool, json_format
from google.protobuf.message_factory import GetMessageClass
fds = descriptor_pb2.FileDescriptorSet(); fds.ParseFromString(open(sys.argv[1], "rb").read())
pool = descriptor_pool.DescriptorPool()
from google.protobuf import descriptor_pool as dp
byname = {f.name: f for f in fds.file}
added = set(); missing = set()
def add(name):
    if name in added: return True
    f = byname.get(name)
    if f is None:
        try:
            pool.AddSerializedFile(dp.Default().FindFileByName(name).serialized_pb) if False else None
            fd = descriptor_pb2.FileDescriptorProto(); dp.Default().FindFileByName(name).CopyToProto(fd); pool.Add(fd); added.add(name); return True
        except Exception:
            missing.add(name); return False
    added.add(name)
    ok = [d for d in f.dependency if add(d)]
    if len(ok) != len(f.dependency):
        f = descriptor_pb2.FileDescriptorProto.FromString(f.SerializeToString())
        del f.dependency[:]; f.dependency.extend(ok); f.ClearField("public_dependency"); f.ClearField("weak_dependency")
    try: pool.Add(f); added.add(name); return True
    except Exception as e: missing.add(f"{name}: {e}"); return False
import google.protobuf.any_pb2, google.protobuf.timestamp_pb2, google.protobuf.duration_pb2, google.protobuf.struct_pb2, google.protobuf.empty_pb2, google.protobuf.wrappers_pb2, google.protobuf.field_mask_pb2
add("third_party/jetski/cortex_pb/cortex.proto")
print("missing", [m[:160] for m in sorted(missing) if "cortex" in m or "jetski" in m][:8], len(missing))
Step = GetMessageClass(pool.FindMessageTypeByName("exa.cortex_pb.CortexTrajectoryStep"))
enum = pool.FindEnumTypeByName("exa.cortex_pb.CortexStepType")
db = sqlite3.connect(f"file:{sys.argv[2]}?immutable=1", uri=True)
oneofs = collections.Counter(); unknown = 0; total = 0
for idx, st, payload in db.execute("select idx, step_type, step_payload from steps order by idx"):
    s = Step(); s.ParseFromString(payload); total += 1
    which = [f.name for f, _ in s.ListFields()]
    oneofs[(enum.values_by_number[st].name if st in enum.values_by_number else st, tuple(w for w in which if w not in ("type", "status", "metadata")))] += 1
    if s.UnknownFields() if hasattr(s, "UnknownFields") else False: unknown += 1
for k, v in sorted(oneofs.items(), key=lambda x: -x[1]): print(v, k)
if len(sys.argv) > 3:
    for idx in map(int, sys.argv[3].split(",")):
        (p,) = db.execute("select step_payload from steps where idx=?", (idx,)).fetchone()
        s = Step(); s.ParseFromString(p)
        print(f"=== {idx}"); print(json_format.MessageToJson(s, preserving_proto_field_name=True)[:2500])
