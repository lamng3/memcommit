"""Traces: what an agent thought, did, and saw during one run, as memories.

    with trace(store, "planner", "book a trip", inputs={"city": "Lyon"}) as t:
        t.thought("I need dates before I can search flights")
        call = t.action("search_flights", {"to": "LYO"})
        t.observation({"flights": 3}, parent=call)
        helper = t.handoff("booker", "book the cheapest flight")   # another agent's sub-trace
        helper.result({"booked": "AF123"})
        helper.end()
        t.result({"status": "booked"})

A run is a tree, the way agent frameworks log them: a root memory of kind
`trace` (who, what task, which run), then one memory per step. Every step is
derived from the root and from its parent step, which is the previous step
unless one is given, so the tree is provenance: invalidating the trace's
inputs, or one step, marks everything after it stale. A step's body is the
caller's JSON plus `trace`, `step` (its number), and `parent`.

Each step commits on its own, so a crash keeps every step before it, and
`end` writes a `trace_end` step with the outcome. A run that raises inside the
`with` block ends with status "error". `Trace.open` reads a trace back.

Step kinds are open: `thought`, `action`, `observation`, `message`, `result`,
and `error` have helpers, and `step(kind, body)` writes any other.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .records import SHARED, SHORT, Memory
from .store import MemoryStore

TRACE, TRACE_END = "trace", "trace_end"
THOUGHT, ACTION, OBSERVATION, MESSAGE, RESULT, ERROR = (
    "thought",
    "action",
    "observation",
    "message",
    "result",
    "error",
)
STEP_KINDS = (THOUGHT, ACTION, OBSERVATION, MESSAGE, RESULT, ERROR, TRACE_END)


@dataclass
class Trace:
    """A writer for one run. Create it with `trace(...)` or `Trace.start(...)`."""

    store: MemoryStore
    root: Memory
    steps: list[Memory] = field(default_factory=list)
    ended: bool = False

    @classmethod
    def start(
        cls,
        store: MemoryStore,
        agent: str,
        task: str,
        *,
        inputs: Any = None,
        derived_from: Iterable[str] = (),
        scope: str = SHARED,
        run: str | None = None,
        importance: float = 0.0,
    ) -> Trace:
        """Write the root of a new run. `run` names it; a fresh id is made when none is given."""
        run = run or uuid.uuid4().hex
        body = {"agent": agent, "task": task, "run": run, "inputs": inputs}
        root = store.put(
            agent, TRACE, f"{agent}:{run}", body, scope=scope, derived_from=derived_from, importance=importance
        )
        return cls(store, root)

    @classmethod
    def open(cls, store: MemoryStore, root_id: str) -> Trace:
        """Read a trace back: its root and every step, in step order, whatever their status."""
        root = store.get(root_id)
        if root is None or root.kind != TRACE:
            raise KeyError(f"no trace {root_id}")
        found = store.find(derived_from=root_id, status=None, where={"trace": root_id})
        steps = [m for m in found if "step" in m.body]  # not summaries or other memories about the trace
        steps.sort(key=lambda m: m.body["step"])
        return cls(store, root, steps, ended=any(m.kind == TRACE_END for m in steps))

    @property
    def agent(self) -> str:
        return self.root.body["agent"]

    @property
    def id(self) -> str:
        return self.root.id

    # Writes --------------------------------------------------------------

    def step(
        self,
        kind: str,
        body: Any = None,
        *,
        parent: Memory | str | None = None,
        derived_from: Iterable[str] = (),
        importance: float = 0.0,
        tier: str = SHORT,
    ) -> Memory:
        """Write one step. `body` is a dict, or any JSON value, which is stored under "content"."""
        if self.ended:
            raise RuntimeError(f"trace {self.id} has ended")
        parent_id = parent.id if isinstance(parent, Memory) else parent
        if parent_id is None:
            parent_id = self.steps[-1].id if self.steps else self.root.id
        content = dict(body) if isinstance(body, dict) else {"content": body}
        content.update(trace=self.root.id, step=len(self.steps), parent=parent_id)
        memory = self.store.put(
            self.agent,
            kind,
            f"{self.root.key}#{len(self.steps)}",
            content,
            scope=self.root.scope,
            derived_from=[self.root.id, parent_id, *derived_from],
            importance=importance,
            tier=tier,
        )
        self.steps.append(memory)
        return memory

    def thought(self, text: str, **extra: Any) -> Memory:
        options = _step_options(extra)
        return self.step(THOUGHT, {"text": text, **extra}, **options)

    def action(self, tool: str, arguments: Any = None, **extra: Any) -> Memory:
        options = _step_options(extra)
        return self.step(ACTION, {"tool": tool, "arguments": arguments, **extra}, **options)

    def observation(self, content: Any, **extra: Any) -> Memory:
        options = _step_options(extra)
        return self.step(OBSERVATION, {"content": content, **extra}, **options)

    def message(self, to: str, content: Any, **extra: Any) -> Memory:
        options = _step_options(extra)
        return self.step(MESSAGE, {"to": to, "content": content, **extra}, **options)

    def result(self, value: Any, **extra: Any) -> Memory:
        options = _step_options(extra)
        return self.step(RESULT, {"value": value, **extra}, **options)

    def error(self, error: str, **extra: Any) -> Memory:
        options = _step_options(extra)
        return self.step(ERROR, {"error": error, **extra}, **options)

    def handoff(self, agent: str, task: str, inputs: Any = None) -> Trace:
        """Start another agent's sub-trace, derived from a `message` step in this one."""
        message = self.message(agent, task)
        return Trace.start(
            self.store,
            agent,
            task,
            inputs=inputs,
            derived_from=[message.id],
            scope=self.root.scope,
            run=f"{self.root.body['run']}/{len(self.steps) - 1}",
        )

    def end(self, status: str = "ok", **extra: Any) -> Memory:
        """Close the run with its outcome. Nothing can be written to it after this."""
        memory = self.step(TRACE_END, {"status": status, **extra})
        self.ended = True
        return memory

    # Reads ---------------------------------------------------------------

    def status(self) -> str | None:
        """The run's outcome, or None while it runs."""
        ends = [m for m in self.steps if m.kind == TRACE_END]
        return ends[-1].body["status"] if ends else None

    def children(self, step: Memory | str) -> list[Memory]:
        """Steps whose parent is `step`."""
        step_id = step.id if isinstance(step, Memory) else step
        return [m for m in self.steps if m.body["parent"] == step_id]

    def subtraces(self) -> list[Memory]:
        """Roots of the traces handed off from this one."""
        return [
            t
            for m in self.steps
            if m.kind == MESSAGE
            for t in self.store.find(kind=TRACE, derived_from=m.id, status=None)
        ]

    def to_json(self) -> dict[str, Any]:
        """The run as one JSON document: root, steps in order, and the status of each step now."""
        state = self.store.state
        return {
            "id": self.root.id,
            "agent": self.agent,
            "task": self.root.body["task"],
            "run": self.root.body["run"],
            "inputs": self.root.body["inputs"],
            "status": self.status(),
            "steps": [{"id": m.id, "kind": m.kind, "memory": state.status.get(m.id), **m.body} for m in self.steps],
        }


def _step_options(extra: dict[str, Any]) -> dict[str, Any]:
    """Pull the keyword options `step` takes out of a helper's extra fields."""
    return {k: extra.pop(k) for k in ("parent", "derived_from", "importance", "tier") if k in extra}


@contextmanager
def trace(store: MemoryStore, agent: str, task: str, **options: Any) -> Iterator[Trace]:
    """A run as a block: it ends "ok" when the block finishes and "error" when it raises."""
    t = Trace.start(store, agent, task, **options)
    try:
        yield t
    except Exception as err:
        if not t.ended:
            t.end("error", error=repr(err))
        raise
    if not t.ended:
        t.end()
