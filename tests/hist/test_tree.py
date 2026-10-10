"""View properties: push's shape at unit sizes, hard budgets, determinism."""

import random
import unittest

from hist.tree import Node, ViewBudgetError, compute_view, node_at, roots


def taelin_push(total: int) -> list[tuple[int, int]]:
    """Taelin's rollback push over ticks 0..total-1, as (start, size) lines."""

    def push(new_state, states):
        if states is None:
            return (0, new_state, None)
        keep, state, older = states
        if keep == 0:
            return (1, state, older)
        return (0, new_state, push(state, older))

    states = None
    for tick in range(total):
        states = push(tick, states)
    starts = []
    while states is not None:
        starts.append(states[1])
        states = states[2]
    starts.sort()
    ends = [*starts[1:], total]
    return [(start, end - start) for start, end in zip(starts, ends)]


def greedy_merge(total: int, limit: int) -> list[Node] | None:
    """UniiChat's bottom-up order: merge the most due pair, oldest on ties."""

    lines = [Node(0, i) for i in range(total)]
    while len(lines) > limit:
        best = None
        for k in range(len(lines) - 1):
            a, b = lines[k], lines[k + 1]
            if a.level == b.level and a.index % 2 == 0 and b.index == a.index + 1:
                due = (total - b.last) / (1 << a.level)
                if best is None or due > best[0]:
                    best = (due, k)
        if best is None:
            return None
        k = best[1]
        lines[k : k + 2] = [Node(lines[k].level + 1, lines[k].index // 2)]
    return lines


def unit_view(total: int, limit: int) -> list[Node] | None:
    try:
        view = compute_view(total, lambda node: "x", lambda node: "-", limit)
    except ViewBudgetError:
        return None
    if any(line.omitted for line in view):
        return None
    return [line.node for line in view]


class ViewShapeTest(unittest.TestCase):
    def test_unit_sizes_match_push_and_greedy_merging(self) -> None:
        for total in range(1, 130):
            push = taelin_push(total)
            view = unit_view(total, len(push))
            self.assertEqual([(n.start, n.size) for n in view], push, total)
            for limit in range(1, total + 1):
                greedy, top = greedy_merge(total, limit), unit_view(total, limit)
                if greedy is not None and top is not None:
                    self.assertEqual(top, greedy, (total, limit))

    def test_twelve_messages_in_three_lines_is_push_shape(self) -> None:
        view = unit_view(12, 3)
        self.assertEqual([(n.start, n.size) for n in view], [(0, 8), (8, 2), (10, 2)])

    def test_variable_utf8_lines_always_fit_and_cover_the_timeline(self) -> None:
        rng = random.Random(7)
        alphabet = ["a", "é", "語", "🙂", " "]
        totals = [2**k - 1 for k in range(1, 12)] + [rng.randrange(1, 5000) for _ in range(40)]
        for total in totals:
            lengths: dict[Node, str] = {}

            def render(node: Node) -> str:
                if node not in lengths:
                    size = rng.randrange(1, 120)
                    lengths[node] = "".join(rng.choice(alphabet) for _ in range(size)) + "\n"
                return lengths[node]

            omitted = lambda node: f"{node.start}+{node.size}|-\n"
            root_bytes = sum(len(render(n).encode()) for n in roots(total))
            for budget in (root_bytes, root_bytes + 1, root_bytes * 3, 8000):
                view = compute_view(total, render, omitted, budget)
                used = sum(len(line.text.encode()) for line in view)
                self.assertLessEqual(used, budget)
                position = 0
                for line in view:
                    self.assertEqual(line.node.start, position)
                    position += line.node.size
                self.assertEqual(position, total)
                again = compute_view(total, render, omitted, budget)
                self.assertEqual(view, again)

    def test_oldest_roots_become_references_when_roots_do_not_fit(self) -> None:
        total = 2**16 - 1
        render = lambda node: "y" * 200 + "\n"
        omitted = lambda node: f"{node.start}+{node.size}|(omitted)\n"
        view = compute_view(total, render, omitted, 1500)
        self.assertEqual(len(view), 16)
        flags = [line.omitted for line in view]
        self.assertEqual(flags, sorted(flags, reverse=True))
        self.assertTrue(flags[0] and not flags[-1])
        self.assertLessEqual(sum(len(line.text.encode()) for line in view), 1500)
        with self.assertRaises(ViewBudgetError):
            compute_view(total, render, omitted, 100)

    def test_node_at_validates_alignment(self) -> None:
        self.assertEqual(node_at(9216, 256), Node(8, 36))
        for start, size in ((3, 2), (0, 3), (-1, 1), (0, 0)):
            with self.assertRaises(ValueError):
                node_at(start, size)


if __name__ == "__main__":
    unittest.main()
