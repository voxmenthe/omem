"""Build a descriptor pool from descriptors extracted from agy, degrading unresolvable types to bytes/int."""
import sys
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf import descriptor_pool as dp
import google.protobuf.any_pb2, google.protobuf.timestamp_pb2, google.protobuf.duration_pb2, google.protobuf.struct_pb2, google.protobuf.empty_pb2, google.protobuf.wrappers_pb2, google.protobuf.field_mask_pb2, google.protobuf.descriptor_pb2
F = descriptor_pb2.FieldDescriptorProto
def build(path):
    fds = descriptor_pb2.FileDescriptorSet(); fds.ParseFromString(open(path, "rb").read())
    files = {f.name: f for f in fds.file if 'jetski' in f.name or 'gemini_coder' in f.name}
    for wk in ["google/protobuf/any.proto", "google/protobuf/timestamp.proto", "google/protobuf/duration.proto", "google/protobuf/struct.proto", "google/protobuf/empty.proto", "google/protobuf/wrappers.proto", "google/protobuf/field_mask.proto", "google/protobuf/descriptor.proto"]:
        fd = descriptor_pb2.FileDescriptorProto(); dp.Default().FindFileByName(wk).CopyToProto(fd); files[wk] = fd
    owner = {}
    def collect(prefix, msgs, enums, fname):
        for e in enums: owner.setdefault(f"{prefix}.{e.name}", (fname, "enum"))
        for m in msgs:
            full = f"{prefix}.{m.name}"; owner.setdefault(full, (fname, "msg"))
            collect(full, m.nested_type, m.enum_type, fname)
    for f in files.values(): collect("." + f.package if f.package else "", f.message_type, f.enum_type, f.name)
    for name, f in list(files.items()):
        pre = "." + f.package if f.package else ""
        keep_m = [m for m in f.message_type if owner[f"{pre}.{m.name}"][0] == name]
        keep_e = [e for e in f.enum_type if owner[f"{pre}.{e.name}"][0] == name]
        if len(keep_m) != len(f.message_type) or len(keep_e) != len(f.enum_type):
            f = descriptor_pb2.FileDescriptorProto.FromString(f.SerializeToString())
            del f.message_type[:]; f.message_type.extend(keep_m); del f.enum_type[:]; f.enum_type.extend(keep_e)
            files[name] = f
    degraded = 0
    def refs(msgs, out):
        for m in msgs:
            for fld in m.field:
                if fld.type_name: out.append(fld)
            refs(m.nested_type, out)
    def strip_opts(msgs):
        for m in msgs:
            m.ClearField("options"); del m.extension[:]
            for fld in m.field:
                fld.ClearField("options")
                if fld.label == F.LABEL_REQUIRED: fld.label = F.LABEL_OPTIONAL
            for e in m.enum_type: e.ClearField("options"); [v.ClearField("options") for v in e.value]
            strip_opts(m.nested_type)
    pool = descriptor_pool.DescriptorPool(); done = set(); failed = {}
    def add(n, stack=()):
        nonlocal degraded
        if n in done or n in failed: return
        f = descriptor_pb2.FileDescriptorProto.FromString(files[n].SerializeToString())
        fields = []; refs(f.message_type, fields)
        for o in sorted({owner[x.type_name][0] for x in fields if x.type_name in owner} - {n}):
            if o not in stack + (n,): add(o, stack + (n,))
        deps = set()
        for fld in fields:
            o = owner.get(fld.type_name)
            if o is None or (o[0] != n and o[0] not in done):
                degraded += 1
                fld.type = F.TYPE_BYTES if fld.type in (F.TYPE_MESSAGE, F.TYPE_GROUP) else F.TYPE_INT32
                fld.ClearField("type_name"); fld.ClearField("default_value")
            elif o[0] != n: deps.add(o[0])
        del f.dependency[:]; f.dependency.extend(sorted(deps)); f.ClearField("public_dependency"); f.ClearField("weak_dependency")
        f.ClearField("options"); del f.extension[:]; del f.service[:]; strip_opts(f.message_type)
        if f.syntax == "editions": f.syntax = "proto3"; f.ClearField("edition")
        for e in f.enum_type: e.ClearField("options"); [v.ClearField("options") for v in e.value]
        try: pool.Add(f); done.add(n)
        except Exception as e: failed[n] = str(e)[:200]
    for n in files: add(n)
    return pool, degraded, failed
if __name__ == "__main__":
    pool, degraded, failed = build(sys.argv[1])
    print("degraded fields", degraded, "failed files", len(failed)); [print(" ", k, v) for k, v in list(failed.items())[:8]]
    print(pool.FindMessageTypeByName("exa.cortex_pb.CortexTrajectoryStep").full_name)
