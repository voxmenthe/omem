import sys, sqlite3, collections, pathlib
sys.path.insert(0, "/tmp/histprobe")
import agy_pool
from google.protobuf.message_factory import GetMessageClass
pool, _, _ = agy_pool.build(sys.argv[1])
Step = GetMessageClass(pool.FindMessageTypeByName("gemini_coder.Step"))
enum = pool.FindEnumTypeByName("exa.cortex_pb.CortexStepType")
oneof = collections.Counter(); pr = collections.Counter(); ui = collections.Counter(); fails = 0; steps = 0; sizediff = 0
gen_inner = collections.Counter(); versions = collections.Counter()
for f in sorted(pathlib.Path(sys.argv[2]).glob("*.db")):
    db = sqlite3.connect(f"file:{f}?immutable=1", uri=True)
    for st, payload in db.execute("select step_type, step_payload from steps"):
        steps += 1
        s = Step()
        try: s.ParseFromString(payload)
        except Exception: fails += 1; continue
        sizediff += len(s.SerializeToString()) != len(payload)
        names = tuple(sorted(fd.name for fd, _ in s.ListFields() if fd.name not in ("type", "status", "metadata", "error", "task_details", "permissions", "render_info")))
        oneof[(enum.values_by_number[st].name.replace("CORTEX_STEP_TYPE_", "") if st in enum.values_by_number else st, names)] += 1
        if s.HasField("planner_response"):
            for fd, _ in s.planner_response.ListFields(): pr[fd.name] += 1
        if s.HasField("user_input"):
            for fd, _ in s.user_input.ListFields(): ui[fd.name] += 1
        if s.HasField("generic") and s.generic.result.HasField("payload"):
            gen_inner[s.generic.result.payload.type_url.rsplit("/", 1)[-1]] += 1
    db.close()
print("steps", steps, "parse failures", fails, "reserialize size differs", sizediff)
for k, v in sorted(oneof.items(), key=lambda x: -x[1]): print(" ", v, k)
print("planner_response fields", dict(pr)); print("user_input fields", dict(ui)); print("generic payload types", dict(gen_inner))
