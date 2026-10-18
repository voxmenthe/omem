"""Per month: Antigravity conversations by user-input count and the mcp.non_blocking_turn_load=false flag."""
import collections, pathlib, sqlite3, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import agy_pool
from google.protobuf.message_factory import GetMessageClass
pool, _, _ = agy_pool.build(str(pathlib.Path(__file__).parent / "agy/all.fds"))
Step = GetMessageClass(pool.FindMessageTypeByName("gemini_coder.Step"))
rows = collections.Counter(); latest_multi_flagged = []
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.db")):
    db = sqlite3.connect(f"file:{f}?mode=ro&immutable=1", uri=True)
    users = db.execute("select step_payload from steps where step_type=14 order by idx").fetchall()
    if not users: continue
    s = Step(); s.ParseFromString(users[0][0])
    mcp = s.user_input.user_config.planner_config.tool_config.mcp
    flagged = (mcp.HasField("non_blocking_turn_load"), mcp.non_blocking_turn_load, s.user_input.user_config.planner_config.tool_config.HasField("mcp"))
    month = s.metadata.created_at.ToDatetime().strftime("%Y-%m")
    rows[(month, "multi" if len(users) > 1 else "single", flagged)] += 1
for k in sorted(rows): print(k, rows[k])
