"""Corpus checks behind the Antigravity mapping: sources, parents, response fields, errors."""
import collections, pathlib, sqlite3, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import agy_pool
from google.protobuf.message_factory import GetMessageClass
pool, _, _ = agy_pool.build(str(pathlib.Path(__file__).parent / "agy/all.fds"))
Step = GetMessageClass(pool.FindMessageTypeByName("gemini_coder.Step"))
Meta = GetMessageClass(pool.FindMessageTypeByName("exa.cortex_pb.CortexTrajectoryMetadata"))
c = collections.Counter(); ex = {}
print("CortexErrorDetails", [(x.number, x.name) for x in pool.FindMessageTypeByName("exa.cortex_pb.CortexErrorDetails").fields])
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.db")):
    db = sqlite3.connect(f"file:{f}?mode=ro&immutable=1", uri=True)
    c[("traj source", db.execute("select source from trajectory_meta").fetchone()[0])] += 1
    m = Meta(); m.ParseFromString(db.execute("select data from trajectory_metadata_blob").fetchone()[0])
    c[("has parent", bool(m.parent_conversation_id))] += 1
    c[("root is self", m.root_conversation_id == f.stem)] += 1
    c[("workspaces", len(m.workspaces))] += 1
    c[("has origin", any(w.repository.git_origin_url for w in m.workspaces))] += 1
    for (p,) in db.execute("select step_payload from steps"):
        s = Step(); s.ParseFromString(p)
        if s.HasField("planner_response"):
            r = s.planner_response
            c[("resp", bool(r.response), bool(r.modified_response), r.response == r.modified_response)] += 1
            if r.response != r.modified_response and r.modified_response and "diff" not in ex:
                ex["diff"] = (r.response[:200], r.modified_response[:200])
        if s.HasField("error_message") and "err" not in ex:
            ex["err"] = str(s.error_message)[:400]
        if s.HasField("ephemeral_message"):
            ex.setdefault("eph", []).append(s.ephemeral_message.content[:120])
        if s.HasField("conversation_history") and "hist" not in ex:
            ex["hist"] = s.conversation_history.content[:200]
        if s.HasField("user_input"):
            u = s.user_input
            c[("ui", bool(u.user_response), bool(u.query), len(u.media) > 0, len(u.images) > 0)] += 1
        if s.HasField("generic"):
            c[("generic tool named", bool(s.metadata.tool_call.name))] += 1
            c[("generic result", bool(s.generic.result.result))] += 1
for k in sorted(c, key=str): print(k, c[k])
for k, v in ex.items(): print(k, v if k != "eph" else v[:5])
