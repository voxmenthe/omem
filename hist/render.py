"""Lines for tree nodes: summaries, exact joins, and outlines.

A message's line is `[s:<alias> <label>] <kind>: <text>` on one line. A node
is exact when all its messages' lines, joined by newlines, fit in 512 bytes;
newlines show as spaces. A node with a model-written summary shows it. Any
other node shows an outline, marked `~`: a preview from its first user and
last agent message, frequent tools, and its span. Outlines are lossy.
"""

import sqlite3
from dataclasses import dataclass

from .tree import LINE_BYTES, OMITTED_TEXT, Node, clip_utf8

EXCERPT_BYTES = 150
# The shortest possible message line; above this level no node can be exact.
_MIN_LINE = len("[s:00000000 x] user: ")
EXACT_MAX_LEVEL = max(level for level in range(12) if (1 << level) * _MIN_LINE <= LINE_BYTES)
_KIND_LABEL = {
    "user": "user",
    "agent": "agent",
    "tool_call": "tool",
    "tool_result": "result",
    "work": "work",
    "event": "event",
}
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass(frozen=True)
class Timeline:
    id: int
    name: str
    kind: str
    display: str
    length: int
    repo_identity: str | None
    generation: int

    @property
    def ref_name(self) -> str:
        return self.name if self.generation == 1 else f"{self.name}~{self.generation}"

    def ref(self, node: Node) -> str:
        return f"{self.ref_name}:{node.start}+{node.size}"


def flat(text: str) -> str:
    return " ".join(text.split())


def kind_label(kind: str, tool: str | None) -> str:
    label = _KIND_LABEL[kind]
    return f"{label} {tool}" if tool and kind != "event" else label


def message_line(alias: str, label: str, kind: str, tool: str | None, text: str) -> str:
    return f"[s:{alias} {label}] {kind_label(kind, tool)}: {flat(text)}"


def clip_line(line: str, limit: int = LINE_BYTES) -> str:
    if len(line.encode("utf-8")) <= limit:
        return line
    return clip_utf8(line, limit - len("…".encode("utf-8"))) + "…"


def short_date(stamp: str) -> str:
    month = _MONTHS[int(stamp[5:7]) - 1]
    return f"{month} {int(stamp[8:10])}"


def span(first: str, last: str) -> str:
    if first[:10] == last[:10]:
        return f"{short_date(first)} {first[11:16]}–{last[11:16]}Z"
    return f"{short_date(first)} – {short_date(last)}"


class Renderer:
    """Renders node lines for one timeline from one read snapshot."""

    def __init__(self, db: sqlite3.Connection, timeline: Timeline) -> None:
        self.db = db
        self.timeline = timeline

    def _rows(self, start: int, end: int) -> list[tuple[str, str, str, str | None, str]]:
        return self.db.execute(
            "SELECT x.alias, x.label, m.kind, m.tool, m.text FROM messages m "
            "JOIN sessions x ON x.id = m.session_id "
            "WHERE m.timeline_id = ? AND m.position BETWEEN ? AND ? ORDER BY m.position",
            (self.timeline.id, start, end),
        ).fetchall()

    def exact(self, node: Node) -> str | None:
        if node.level > EXACT_MAX_LEVEL:
            return None
        lines = [message_line(*row) for row in self._rows(node.start, node.last)]
        joined = "\n".join(lines)
        return joined if len(joined.encode("utf-8")) <= LINE_BYTES else None

    def summary(self, node: Node) -> str | None:
        row = self.db.execute(
            "SELECT text FROM nodes WHERE timeline_id = ? AND level = ? AND idx = ? "
            "AND kind = 'summary'",
            (self.timeline.id, node.level, node.index),
        ).fetchone()
        return None if row is None else row[0]

    def _excerpt(self, kind: str, start: int, end: int, newest: bool) -> str | None:
        order = "DESC" if newest else "ASC"
        row = self.db.execute(
            f"SELECT text FROM messages WHERE timeline_id = ? AND kind = ? AND position "
            f"BETWEEN ? AND ? ORDER BY position {order} LIMIT 1",
            (self.timeline.id, kind, start, end),
        ).fetchone()
        return None if row is None else clip_line(flat(row[0]), EXCERPT_BYTES)

    def outline(self, node: Node) -> str:
        start, end = node.start, node.last
        if node.level == 0:
            row = self._rows(start, end)[0]
            return "~ " + clip_line(message_line(*row), LINE_BYTES - 2)
        pieces = []
        first_user = self._excerpt("user", start, end, newest=False)
        last_agent = self._excerpt("agent", start, end, newest=True)
        if first_user:
            pieces.append(f'user: "{first_user}"')
        if last_agent:
            pieces.append(f'agent: "{last_agent}"')
        if not pieces:
            first = self._rows(start, start)[0]
            pieces.append(clip_line(message_line(*first), EXCERPT_BYTES * 2))
        tools = self.db.execute(
            "SELECT tool, count(*) AS n FROM messages WHERE timeline_id = ? AND kind = 'tool_call' "
            "AND position BETWEEN ? AND ? GROUP BY tool ORDER BY n DESC, tool LIMIT 3",
            (self.timeline.id, start, end),
        ).fetchall()
        sessions = self.db.execute(
            "SELECT count(*) FROM sessions WHERE timeline_id = ? AND first_position <= ? "
            "AND last_position >= ?",
            (self.timeline.id, end, start),
        ).fetchone()[0]
        times = self.db.execute(
            "SELECT min(time), max(time) FROM messages WHERE timeline_id = ? AND position IN (?, ?)",
            (self.timeline.id, start, end),
        ).fetchone()
        facts = [f"{node.size} msgs", f"{sessions} session{'s' if sessions != 1 else ''}"]
        if times[0]:
            facts.append(span(times[0], times[1]))
        tool_text = ", ".join(f"{tool}×{n}" for tool, n in tools)
        tail = f" ({'; '.join(([f'tools: {tool_text}'] if tool_text else []) + [', '.join(facts)])})"
        return "~ " + clip_line(" … ".join(pieces), LINE_BYTES - len(tail.encode()) - 2) + tail

    def text(self, node: Node) -> str:
        """The node's line text: summary, exact join, or outline."""

        found = self.summary(node) or self.exact(node)
        return flat(found) if found is not None else self.outline(node)

    def line(self, node: Node) -> str:
        return f"{self.timeline.ref(node)}|{self.text(node)}\n"

    def omitted(self, node: Node) -> str:
        return f"{self.timeline.ref(node)}|{OMITTED_TEXT}\n"
