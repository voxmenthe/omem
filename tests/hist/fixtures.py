"""Transcript fixtures in the record shapes observed on this machine in M0.

Codex records model CLI 0.162 (paginated session_meta, content_item_kinds)
and the pre-0.2 legacy layout; Claude Code records model 2.1.295; Command
Code records model CLI 1.83 (format version 3); OpenCode tables model 2.0.31;
Devin tables model CLI 3000.10; Antigravity steps model CLI 1.4. Text is
synthetic; structure, field names, and injected-context markers are real.
"""

import itertools
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def stamp(minute: int, second: int = 0) -> str:
    return f"2026-10-01T10:{minute:02d}:{second:02d}.000Z"


def epoch_ms(minute: int, second: int = 0) -> int:
    """`stamp(minute, second)` in milliseconds since the epoch."""

    return 1790848800000 + (minute * 60 + second) * 1000


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


class Codex:
    """Codex rollout records."""

    @staticmethod
    def path(home: Path, thread: str, archived: bool = False) -> Path:
        name = f"rollout-2026-10-01T10-00-00-{thread}.jsonl"
        if archived:
            return home / "archived_sessions" / name
        return home / "sessions" / "2026" / "10" / "01" / name

    @staticmethod
    def meta(thread: str, cwd: str, source: Any = "cli", origin: str | None = None,
             minute: int = 0, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": thread, "session_id": thread, "timestamp": stamp(minute), "cwd": cwd,
            "originator": "codex_cli_rs", "cli_version": "0.162.0", "source": source,
            "model_provider": "openai", "base_instructions": {"text": "base"},
            "history_mode": "paginated",
        }
        if origin:
            payload["git"] = {"repository_url": origin, "branch": "main", "commit_hash": "abc"}
        payload.update(extra)
        return {"timestamp": stamp(minute), "type": "session_meta", "payload": payload}

    @staticmethod
    def item(payload: dict[str, Any], minute: int, second: int = 0) -> dict[str, Any]:
        return {"timestamp": stamp(minute, second), "type": "response_item", "payload": payload}

    @classmethod
    def user(cls, text: str, minute: int, second: int = 0) -> dict[str, Any]:
        return cls.item({
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]},
        }, minute, second)

    @classmethod
    def injected_user(cls, minute: int) -> dict[str, Any]:
        return cls.item({
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>\nINJECTED-AGENTS\n</INSTRUCTIONS>"},
                {"type": "input_text", "text": "<environment_context>INJECTED-ENV</environment_context>"},
            ],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["agents_md.instructions", "environments.environment_context"]
            },
        }, minute)

    @classmethod
    def legacy_injected_user(cls, minute: int) -> dict[str, Any]:
        """An older rollout's injected context: tags only, no item kinds."""

        return cls.item({
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<user_instructions>INJECTED-OLD</user_instructions>"}],
        }, minute)

    @classmethod
    def developer(cls, text: str, minute: int) -> dict[str, Any]:
        return cls.item({
            "type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["hooks.additional_context"]},
        }, minute)

    @classmethod
    def assistant(cls, text: str, minute: int, second: int = 0) -> dict[str, Any]:
        return cls.item({
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}],
        }, minute, second)

    @classmethod
    def call(cls, name: str, arguments: str, call_id: str, minute: int) -> dict[str, Any]:
        return cls.item({"type": "function_call", "name": name, "arguments": arguments,
                         "call_id": call_id}, minute)

    @classmethod
    def output(cls, call_id: str, text: str, minute: int) -> dict[str, Any]:
        return cls.item({"type": "function_call_output", "call_id": call_id, "output": text}, minute)

    @classmethod
    def reasoning(cls, minute: int) -> dict[str, Any]:
        return cls.item({"type": "reasoning", "summary": [{"type": "summary_text", "text": "REASONING-TEXT"}],
                         "encrypted_content": "zzz"}, minute)

    @classmethod
    def agent_message(cls, author: str, text: str, minute: int) -> dict[str, Any]:
        return cls.item({"type": "agent_message", "author": author, "recipient": "/root",
                         "content": [{"type": "input_text", "text": text}]}, minute)

    @staticmethod
    def compacted(minute: int) -> dict[str, Any]:
        return {"timestamp": stamp(minute), "type": "compacted",
                "payload": {"message": "SUMMARY-TEXT", "replacement_history": []}}

    @staticmethod
    def event(kind: str, minute: int, **fields: Any) -> dict[str, Any]:
        return {"timestamp": stamp(minute), "type": "event_msg", "payload": {"type": kind, **fields}}


class Claude:
    """Claude Code transcript records."""

    @staticmethod
    def project(root: Path, cwd: str) -> Path:
        return root / "".join(c if c.isalnum() else "-" for c in cwd)

    @staticmethod
    def base(kind: str, uuid: str, session: str, cwd: str, minute: int, entrypoint: str = "cli",
             parent: str | None = None, **extra: Any) -> dict[str, Any]:
        record = {
            "parentUuid": parent, "isSidechain": False, "type": kind, "uuid": uuid,
            "timestamp": stamp(minute), "userType": "external", "entrypoint": entrypoint,
            "cwd": cwd, "sessionId": session, "version": "2.1.295", "gitBranch": "main",
        }
        record.update(extra)
        return record

    @classmethod
    def user(cls, uuid: str, session: str, cwd: str, text: Any, minute: int, **extra: Any) -> dict[str, Any]:
        return cls.base("user", uuid, session, cwd, minute,
                        message={"role": "user", "content": text}, **extra)

    @classmethod
    def assistant(cls, uuid: str, session: str, cwd: str, blocks: list[dict[str, Any]], minute: int,
                  **extra: Any) -> dict[str, Any]:
        return cls.base("assistant", uuid, session, cwd, minute, message={
            "model": "claude-opus-5-5", "id": f"msg_{uuid}", "type": "message", "role": "assistant",
            "content": blocks, "stop_reason": "end_turn", "usage": {},
        }, **extra)

    @staticmethod
    def text(text: str) -> dict[str, Any]:
        return {"type": "text", "text": text}

    @staticmethod
    def tool_use(tool_id: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "tool_use", "id": tool_id, "name": name, "input": payload}

    @staticmethod
    def tool_result(tool_id: str, text: str) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": tool_id, "content": text}

    @classmethod
    def hook_context(cls, uuid: str, session: str, cwd: str, minute: int, text: str) -> dict[str, Any]:
        return cls.base("attachment", uuid, session, cwd, minute, attachment={
            "type": "hook_additional_context", "content": [text], "hookEvent": "SessionStart",
            "hookName": "SessionStart", "toolUseID": "x",
        }, rendered=[{"content": text}], renderedRole="system")

    @classmethod
    def queued(cls, uuid: str, session: str, cwd: str, minute: int, mode: str, prompt: str,
               origin: str | None = None) -> dict[str, Any]:
        """What arrived while the agent worked: a typed prompt, or a task notification."""

        attachment: dict[str, Any] = {"type": "queued_command", "commandMode": mode, "prompt": prompt,
                                      "source_uuid": "s", "delivery_id": "d", "timestamp": stamp(minute)}
        if origin is not None:
            attachment["origin"] = {"kind": origin}
        return cls.base("attachment", uuid, session, cwd, minute, attachment=attachment,
                        rendered=[{"content": prompt}], renderedRole="user")

    @staticmethod
    def saved_output(path: Path, preview: str) -> str:
        """A tool result's text when the host saved the whole output apart."""

        return (f"<persisted-output>\nOutput too large (88.1KB). Full output saved to: {path}\n\n"
                f"Preview (first 2KB):\n{preview}\n...\n</persisted-output>")

    @staticmethod
    def metadata(kind: str, session: str, **fields: Any) -> dict[str, Any]:
        return {"type": kind, "sessionId": session, **fields}


class CommandCode:
    """Command Code transcript records and the session's metadata file."""

    _ids = itertools.count(1)

    @staticmethod
    def path(root: Path, cwd: str, session: str) -> Path:
        return root / "".join(c if c.isalnum() else "-" for c in cwd.strip("/").lower()) / f"{session}.jsonl"

    @staticmethod
    def write_meta(path: Path, **fields: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name(f"{path.stem}.meta.json").write_text(json.dumps({"traceIds": [], **fields}))

    @staticmethod
    def session(session: str, cwd: str, minute: int = 0) -> dict[str, Any]:
        return {"type": "session", "version": 3, "id": session, "timestamp": stamp(minute), "cwd": cwd}

    @classmethod
    def message(cls, role: str, source: str | None, blocks: list[dict[str, Any]], minute: int,
                **meta: Any) -> dict[str, Any]:
        number = next(cls._ids)
        fields: dict[str, Any] = {"messageId": f"00000000-0000-4000-8000-{number:012d}", **meta}
        if source is not None:
            fields["source"] = source
        return {"type": "message", "id": f"{number:08x}", "parentId": None, "timestamp": stamp(minute),
                "message": {"role": role, "content": blocks, "meta": fields}}

    @classmethod
    def user(cls, text: str, minute: int, source: str | None = "user", **meta: Any) -> dict[str, Any]:
        return cls.message("user", source, [{"type": "text", "text": text}], minute, **meta)

    @classmethod
    def assistant(cls, blocks: list[dict[str, Any]], minute: int) -> dict[str, Any]:
        return cls.message("assistant", "model", blocks, minute)

    @classmethod
    def tool_result(cls, tool_id: str, text: str, minute: int) -> dict[str, Any]:
        return cls.message("user", "tool", [{"type": "tool_result", "tool_use_id": tool_id,
                                             "content": [{"type": "text", "text": text}]}], minute)


class OpenCode:
    """An OpenCode database: the `session_v2` and `session_message` columns
    hist reads, in WAL mode with the connection held open as a running host's is."""

    SCHEMA = """
        CREATE TABLE session_v2 (
          id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, version TEXT NOT NULL,
          time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, time_viewed INTEGER);
        CREATE TABLE session_message (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
          time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    """

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "opencode.db"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(self.SCHEMA)
        self._ids = itertools.count(1)

    def close(self) -> None:
        self.db.close()

    def source(self, session: str) -> Path:
        return Path(f"{self.path}#{session}")

    def session(self, session: str, cwd: str, minute: int = 0, version: str = "2.0.31",
                viewed: bool = True, parent: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session, parent, cwd, version, epoch_ms(minute), epoch_ms(minute),
             epoch_ms(minute) if viewed else None),
        )
        self.db.commit()

    def add(self, session: str, rows: list[dict[str, Any]]) -> None:
        """Append rows, each `{"type", "minute", "data"}`, after the session's
        last; they are updated now, as a running host's are."""

        (last,) = self.db.execute(
            "SELECT coalesce(max(seq), 3) FROM session_message WHERE session_id = ?", (session,)
        ).fetchone()
        written = int(time.time() * 1000)
        for seq, row in enumerate(rows, last + 1):
            created = epoch_ms(row["minute"])
            data = {**row["data"], "time": {"created": created}}
            self.db.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"msg_{next(self._ids):08d}", session, row["type"], seq, created, written, json.dumps(data)),
            )
        self.db.commit()

    @staticmethod
    def row(kind: str, minute: int, **data: Any) -> dict[str, Any]:
        return {"type": kind, "minute": minute, "data": data}

    @classmethod
    def user(cls, text: str, minute: int) -> dict[str, Any]:
        return cls.row("user", minute, text=text)

    @classmethod
    def assistant(cls, blocks: list[dict[str, Any]], minute: int, **data: Any) -> dict[str, Any]:
        return cls.row("assistant", minute, agent="build", model={"id": "m"}, content=blocks,
                       finish="stop", **data)

    @classmethod
    def idle(cls, minute: int) -> dict[str, Any]:
        return cls.row("idle", minute, outcome="succeeded")


class Devin:
    """A Devin CLI database: the `sessions` and `message_nodes` columns hist
    reads, in WAL mode with the connection held open."""

    SCHEMA = """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, created_at INTEGER NOT NULL,
          last_activity_at INTEGER NOT NULL);
        CREATE TABLE message_nodes (
          row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, node_id INTEGER NOT NULL,
          parent_node_id INTEGER, chat_message TEXT NOT NULL, created_at INTEGER NOT NULL, metadata TEXT,
          UNIQUE (session_id, node_id));
    """

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "sessions.db"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(self.SCHEMA)
        self._ids = itertools.count(1)

    def close(self) -> None:
        self.db.close()

    def source(self, session: str) -> Path:
        return Path(f"{self.path}#{session}")

    def session(self, session: str, cwd: str) -> None:
        now = int(time.time())
        self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (session, cwd, now, now))
        self.db.commit()

    def add(self, session: str, nodes: list[tuple[int, int | None, dict[str, Any]]]) -> None:
        """Insert (node id, parent node id, chat message) rows in order."""

        now = int(time.time())
        for node, parent, message in nodes:
            self.db.execute(
                "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session, node, parent, json.dumps(message), now),
            )
        self.db.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?", (now, session))
        self.db.commit()

    def message(self, role: str, content: str, minute: int, message_id: str | None = None,
                source: str = "system", operation: str = "unknown", **fields: Any) -> dict[str, Any]:
        metadata = {"is_user_input": fields.pop("is_user_input", None), "created_at": stamp(minute),
                    "telemetry": {"source": source, "operation": operation}}
        return {"message_id": message_id or f"m-{next(self._ids):08d}", "role": role,
                "content": content, "metadata": metadata, **fields}

    def prompt(self, minute: int, operation: str = "normal") -> dict[str, Any]:
        return self.message("system", "You are Devin, an interactive command line agent.", minute,
                            source="sysprompt", operation=operation)

    def user(self, text: str, minute: int, message_id: str | None = None) -> dict[str, Any]:
        return self.message("user", text, minute, message_id, source="user", is_user_input=True)

    def reply(self, text: str, minute: int, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.message("assistant", text, minute, source="assistant", tool_calls=calls or [],
                            thinking={"thinking": "THINKING-TEXT", "signature": "s"})


def protobuf(*fields: tuple[int, int | str | bytes]) -> bytes:
    """Protobuf wire format: varints for ints, length-delimited otherwise."""

    def varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte, value = value & 0x7F, value >> 7
            out.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(out)

    out = bytearray()
    for number, value in fields:
        if isinstance(value, int):
            out += varint(number << 3) + varint(value)
        else:
            data = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else value
            out += varint(number << 3 | 2) + varint(len(data)) + data
    return bytes(out)


class Antigravity:
    """Antigravity CLI conversation databases and prompt history. Steps are
    encoded by the field numbers hist reads; an open conversation keeps its
    connection, and so its `-wal` file, open."""

    DONE, RUNNING = 3, 2

    def __init__(self, root: Path) -> None:
        self.conversations = root / "conversations"
        self.conversations.mkdir(parents=True, exist_ok=True)
        self.history = root / "history.jsonl"
        self.open: dict[str, sqlite3.Connection] = {}

    def close(self) -> None:
        for db in self.open.values():
            db.close()
        self.open.clear()

    def path(self, conversation: str) -> Path:
        return self.conversations / f"{conversation}.db"

    def conversation(self, conversation: str, cwd: str | None, origin: str | None = None,
                     parent: str | None = None, minute: int = 0) -> None:
        db = sqlite3.connect(self.path(conversation))
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER NOT NULL, "
            "status INTEGER NOT NULL, step_payload BLOB NOT NULL);"
            "CREATE TABLE trajectory_metadata_blob (data BLOB NOT NULL);"
        )
        workspace = protobuf((1, Path(cwd).as_uri()), (3, protobuf((2, origin)))) if cwd and origin else (
            protobuf((1, Path(cwd).as_uri())) if cwd else b"")
        fields: list[tuple[int, int | str | bytes]] = [(2, self.timestamp(minute))]
        if workspace:
            fields.append((1, workspace))
        if parent:
            fields.append((5, parent))
        db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (protobuf(*fields),))
        db.commit()
        self.open[conversation] = db

    def typed(self, conversation: str, text: str) -> None:
        with self.history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"display": text, "timestamp": 0, "conversationId": conversation}) + "\n")

    def add(self, conversation: str, steps: list[tuple[int, int, bytes]]) -> None:
        """Append (step type, status, payload) steps."""

        db = self.open[conversation]
        (last,) = db.execute("SELECT coalesce(max(idx), -1) FROM steps").fetchone()
        for idx, (kind, status, payload) in enumerate(steps, last + 1):
            db.execute("INSERT INTO steps VALUES (?, ?, ?, ?)", (idx, kind, status, payload))
        db.commit()

    def finish(self, conversation: str) -> None:
        """Close the conversation as the CLI does on exit, removing its `-wal`."""

        self.open.pop(conversation).close()

    def set_status(self, conversation: str, idx: int, status: int) -> None:
        db = self.open[conversation]
        (payload,) = db.execute("SELECT step_payload FROM steps WHERE idx = ?", (idx,)).fetchone()
        db.execute("UPDATE steps SET status = ?, step_payload = ? WHERE idx = ?",
                   (status, payload + protobuf((4, status)), idx))
        db.commit()

    @staticmethod
    def timestamp(minute: int) -> bytes:
        return protobuf((1, epoch_ms(minute) // 1000))

    @classmethod
    def step(cls, kind: int, minute: int, body: tuple[int, bytes] | None = None,
             status: int = DONE, tool: str | None = None) -> tuple[int, int, bytes]:
        metadata = [(1, cls.timestamp(minute))]
        if tool:
            metadata.append((4, protobuf((1, "call-1"), (2, tool))))
        fields: list[tuple[int, int | str | bytes]] = [(1, kind), (4, status), (5, protobuf(*metadata))]
        if body:
            fields.append(body)
        return kind, status, protobuf(*fields)

    @classmethod
    def user(cls, text: str, minute: int, printed: bool = False) -> tuple[int, int, bytes]:
        body: list[tuple[int, int | str | bytes]] = [(2, text)]
        if printed:
            # user_config.planner_config.tool_config.mcp.non_blocking_turn_load = false
            tool_config = protobuf((16, protobuf((4, 0))))
            body.append((12, protobuf((1, protobuf((13, tool_config))))))
        return cls.step(14, minute, (19, protobuf(*body)))

    @classmethod
    def reply(cls, text: str, minute: int, calls: tuple[tuple[str, str], ...] = (), status: int = DONE,
              thinking: str = "") -> tuple[int, int, bytes]:
        body: list[tuple[int, int | str | bytes]] = [(1, text), (8, text), (3, thinking)]
        body += [(7, protobuf((1, f"call-{name}"), (2, name), (3, arguments))) for name, arguments in calls]
        return cls.step(15, minute, (20, protobuf(*body)), status)

    @classmethod
    def result(cls, name: str, text: str, minute: int) -> tuple[int, int, bytes]:
        return cls.step(132, minute, (140, protobuf((2, protobuf((1, text))))), tool=name)


def git_repo(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


class HistCase(unittest.TestCase):
    """A scratch machine: two Codex accounts, one root each for Claude Code,
    Command Code, OpenCode, Devin, and Antigravity, and a hist home."""

    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.codex = self.base / ".codex"
        self.codex_state = self.base / ".codex-state"
        self.codex2 = self.base / ".codex2"
        self.claude = self.base / ".claude" / "projects"
        self.commandcode = self.base / ".commandcode" / "projects"
        self.opencode = self.base / ".local" / "share" / "opencode"
        self.devin = self.base / ".local" / "share" / "devin" / "cli"
        self.antigravity = self.base / ".gemini" / "antigravity-cli"
        for path in (self.codex, self.codex_state, self.codex2, self.claude, self.commandcode):
            path.mkdir(parents=True)
        self.home = self.base / "hist-home"
        self.repo = git_repo(self.base / "work" / "proj", "git@github.com:me/proj.git")
        self.origin = "https://github.com/me/proj.git"
        self.write_config()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_config(self, extra: str = "", inject: str = '["proj"]') -> None:
        self.home.mkdir(exist_ok=True)
        (self.home / "config.toml").write_text(
            f"""
[[sources]]
adapter = "codex"
label = "codex"
roots = ["{self.codex}", "{self.codex_state}"]

[[sources]]
adapter = "codex"
label = "codex2"
roots = ["{self.codex2}"]

[[sources]]
adapter = "claude"
label = "claude"
roots = ["{self.claude}"]

[[sources]]
adapter = "commandcode"
label = "commandcode"
roots = ["{self.commandcode}"]

[[sources]]
adapter = "opencode"
label = "opencode"
roots = ["{self.opencode}"]

[[sources]]
adapter = "devin"
label = "devin"
roots = ["{self.devin}"]

[[sources]]
adapter = "antigravity"
label = "antigravity"
roots = ["{self.antigravity}"]

[view]
inject = {inject}
{extra}
"""
        )

    def hist(self, *args: str, cwd: Path | None = None, stdin: str | None = None,
             env: dict[str, str] | None = None, check: bool = True,
             terminal: bool = False) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CODEX_", "CLAUDE_CODE_", "HIST_"))
        }
        environment.update({"HIST_HOME": str(self.home), "PYTHONPATH": str(REPO_ROOT)})
        environment.update(env or {})
        # Never the runner's own terminal: `find` treats a terminal as a person.
        source: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        if terminal:
            controller, device = os.openpty()
            self.addCleanup(os.close, controller)
            self.addCleanup(os.close, device)
            source = {"stdin": device}
        result = subprocess.run(
            [sys.executable, "-m", "hist", *args], cwd=cwd or self.repo, **source,
            capture_output=True, text=True, env=environment, timeout=120,
        )
        if check and result.returncode != 0:
            self.fail(f"hist {' '.join(args)} exited {result.returncode}: {result.stderr}")
        return result

    def db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.home / "hist.db")
        self.addCleanup(connection.close)
        return connection

    def assertNotStored(self, marker: bytes) -> None:
        stored = b"".join(path.read_bytes() for path in self.home.glob("hist.db*") if path.is_file())
        self.assertFalse(marker in stored, f"{marker!r} is in the store")
