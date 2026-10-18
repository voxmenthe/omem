"""Print field numbers of the Antigravity messages the hist adapter decodes."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import agy_pool
pool, _, _ = agy_pool.build(str(pathlib.Path(__file__).parent / "agy/all.fds"))
def show(name, only=None):
    m = pool.FindMessageTypeByName(name)
    fs = [(x.number, x.name, x.message_type.full_name if x.message_type else (x.enum_type.full_name if x.enum_type else x.type), "rep" if x.is_repeated else "") for x in m.fields]
    if only: fs = [f for f in fs if f[1] in only]
    print(name, fs)
    return m
show("exa.cortex_pb.CortexStepMetadata", {"created_at", "source", "tool_call", "completed_at", "finished_generating_at", "viewable_at"})
show("exa.cortex_pb.CortexStepUserInput")
pr = show("exa.cortex_pb.CortexStepPlannerResponse")
g = show("exa.cortex_pb.CortexStepGeneric")
for x in g.fields:
    if x.message_type: show(x.message_type.full_name)
for n in ["CortexStepCheckpoint", "CortexStepErrorMessage", "CortexStepConversationHistory", "CortexStepEphemeralMessage", "CortexStepSystemMessage"]:
    show("exa.cortex_pb." + n)
show("exa.cortex_pb.CortexTrajectoryMetadata", {"workspaces", "created_at", "root_conversation_id", "workspace_uris", "project_id", "parent_conversation_id"})
for x in pr.fields:
    if x.name == "tool_calls": show(x.message_type.full_name)
w = pool.FindMessageTypeByName("exa.cortex_pb.CortexTrajectoryMetadata").fields_by_name["workspaces"].message_type
show(w.full_name)
for x in w.fields:
    if x.message_type and x.name == "repository": show(x.message_type.full_name)
