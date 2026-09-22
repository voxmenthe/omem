"""Declarative host table: identity slugs, hook wire formats, and delivery.

A host is any coding-agent CLI that runs `memory`. Every lowercase slug is a
valid identity, so any agent can record candidates and be named in the trust
list. Only the hosts listed here have a prompt hook whose wire format OMem can
speak; every other bound host is told at wake to orient manually.
"""

import re
from dataclasses import dataclass
from typing import Final, Literal, Mapping

HOST_SLUG: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")

HookCodec = Literal["json", "text"]


@dataclass(frozen=True)
class HookHost:
    """One host with a supported prompt-hook wire format.

    `json` hosts read a `hookSpecificOutput` envelope; `text` hosts append
    plain stdout to the model context. `compact_event` names the host event
    that carries the post-compaction recovery checkpoint, gated on
    `compact_source` when the event also fires for ordinary starts.
    """

    label: str
    command: str
    codec: HookCodec
    compact_event: str | None = None
    compact_source: str | None = None


HOOK_HOSTS: Final[Mapping[str, HookHost]] = {
    "codex": HookHost("Codex", "codex-hook", "json", "SessionStart", "compact"),
    "claude": HookHost(
        "Claude Code", "hook claude", "json", "SessionStart", "compact"
    ),
    "devin": HookHost("Devin", "hook devin", "json", "SessionStart", "compact"),
    "kimi": HookHost("Kimi Code", "hook kimi", "text"),
}
