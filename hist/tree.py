"""Timeline tree positions and the top-down orientation view.

Node `(level, index)` covers the `2**level` positions from `index * 2**level`.
Every node shown or opened is complete: it ends before the timeline's length.
This module is pure; callers supply rendered line text.
"""

import heapq
from dataclasses import dataclass
from typing import Callable

LINE_BYTES = 512
OMITTED_TEXT = "(omitted for space: zoom it)"


class ViewBudgetError(ValueError):
    """The budget cannot hold even all-reference roots."""


@dataclass(frozen=True, order=True)
class Node:
    level: int
    index: int

    @property
    def start(self) -> int:
        return self.index << self.level

    @property
    def size(self) -> int:
        return 1 << self.level

    @property
    def last(self) -> int:
        return ((self.index + 1) << self.level) - 1

    def children(self) -> tuple["Node", "Node"]:
        if self.level == 0:
            raise ValueError("a single message has no children")
        return Node(self.level - 1, 2 * self.index), Node(
            self.level - 1, 2 * self.index + 1
        )

    def descendants(self, depth: int) -> list["Node"]:
        """The 2**depth nodes `depth` levels below, oldest first."""

        depth = min(depth, self.level)
        level = self.level - depth
        first = self.index << depth
        return [Node(level, first + k) for k in range(1 << depth)]


def node_at(start: int, size: int) -> Node:
    """Validate an aligned power-of-two range and return its node."""

    if size < 1 or size & (size - 1):
        raise ValueError(f"size {size} is not a power of two")
    if start < 0 or start % size:
        raise ValueError(f"start {start} is not a multiple of {size}")
    level = size.bit_length() - 1
    return Node(level, start >> level)


def roots(total: int) -> list[Node]:
    """The aligned roots of a timeline: one node per set bit, oldest first."""

    nodes: list[Node] = []
    start = 0
    for level in range(total.bit_length() - 1, -1, -1):
        if total >> level & 1:
            nodes.append(Node(level, start >> level))
            start += 1 << level
    return nodes


def _split_key(total: int, node: Node) -> tuple[float, int]:
    # Children's due: (T - last) / 2**(l-1). Floats are exact here because
    # the numerator is a small integer and the divisor a power of two.
    due = (total - node.last) / (1 << (node.level - 1))
    return due, -node.start


@dataclass(frozen=True)
class ViewLine:
    node: Node
    text: str
    omitted: bool


def compute_view(
    total: int,
    render: Callable[[Node], str],
    render_omitted: Callable[[Node], str],
    budget: int,
) -> list[ViewLine]:
    """Split least-due lines while the rendered view fits `budget` bytes.

    `render` returns one complete output line, newline included. When even
    the roots do not fit, the oldest roots become bare references until
    they do; splitting stops at the first split that would not fit.
    """

    if total <= 0:
        return []
    lines: dict[int, ViewLine] = {}
    used = 0
    for node in roots(total):
        text = render(node)
        lines[node.start] = ViewLine(node, text, False)
        used += len(text.encode("utf-8"))
    if used > budget:
        for start in sorted(lines):
            line = lines[start]
            text = render_omitted(line.node)
            used += len(text.encode("utf-8")) - len(line.text.encode("utf-8"))
            lines[start] = ViewLine(line.node, text, True)
            if used <= budget:
                break
        if used > budget:
            raise ViewBudgetError(
                f"{budget} bytes cannot hold the {len(lines)} roots of {total}"
            )
        return [lines[start] for start in sorted(lines)]

    heap = [
        (*_split_key(total, line.node), line.node)
        for line in lines.values()
        if line.node.level > 0
    ]
    heapq.heapify(heap)
    while heap:
        _, _, node = heapq.heappop(heap)
        left, right = node.children()
        left_text, right_text = render(left), render(right)
        grown = (
            used
            - len(lines[node.start].text.encode("utf-8"))
            + len(left_text.encode("utf-8"))
            + len(right_text.encode("utf-8"))
        )
        if grown > budget:
            break
        used = grown
        lines[left.start] = ViewLine(left, left_text, False)
        lines[right.start] = ViewLine(right, right_text, False)
        for child in (left, right):
            if child.level > 0:
                heapq.heappush(heap, (*_split_key(total, child), child))
    return [lines[start] for start in sorted(lines)]


def clip_utf8(text: str, limit: int) -> str:
    """Cut text to at most `limit` UTF-8 bytes without splitting a character."""

    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")
