"""Compaction: a finished trace becomes one long-term summary, and its steps leave working memory.

A trace is many small memories: every thought, call, and observation. Once
the run is over, an agent rarely needs the steps; it needs what happened.
`Compactor` writes a `summary` memory in the long-term tier, derived from the
trace root and every step, and moves the steps to the archive, in one
transaction. Nothing is lost: the steps are still in the log and the archive,
and because the summary is derived from them, a step that is later found to
be wrong (retracted, or stale after its inputs were invalidated) takes the
summary with it.

What a summary says is the `Summarizer`'s choice. `outline` is structural and
needs no model: the task, the outcome, the tools called, the results, the
errors. A summarizer backed by a language model (a reflection, in the sense of
Generative Agents and Reflexion) is a function with the same signature.

Which traces to compact is the `CompactionPolicy`'s choice. `FinishedTraces`
compacts every ended trace without a live summary, optionally keeping the
newest few whole.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .records import ARCHIVE, LONG, Memory
from .store import SYSTEM, MemoryStore
from .traces import ACTION, ERROR, RESULT, TRACE, TRACE_END, Trace

SUMMARY = "summary"

Summarizer = Callable[[Trace], Any]


class CompactionPolicy(Protocol):
    def select(self, traces: Sequence[Trace]) -> list[Trace]:
        """The traces to compact now. Every trace given has ended and has no live summary."""


class FinishedTraces:
    """Compact every finished trace, except the `keep_recent` newest, which stay whole."""

    def __init__(self, keep_recent: int = 0) -> None:
        self.keep_recent = keep_recent

    def select(self, traces: Sequence[Trace]) -> list[Trace]:
        return list(traces[: max(0, len(traces) - self.keep_recent)])


def outline(t: Trace) -> dict[str, Any]:
    """A structural summary: task, outcome, tools, results, errors, and step counts."""
    counts: dict[str, int] = {}
    for m in t.steps:
        counts[m.kind] = counts.get(m.kind, 0) + 1
    return {
        "agent": t.agent,
        "task": t.root.body["task"],
        "run": t.root.body["run"],
        "status": t.status(),
        "tools": [m.body.get("tool") for m in t.steps if m.kind == ACTION],
        "results": [m.body.get("value") for m in t.steps if m.kind == RESULT],
        "errors": [m.body.get("error") for m in t.steps if m.kind == ERROR or m.body.get("error")],
        "steps": counts,
    }


class Compactor:
    def __init__(
        self,
        store: MemoryStore,
        summarizer: Summarizer = outline,
        policy: CompactionPolicy | None = None,
        archive_steps: bool = True,
        agent: str = SYSTEM,
    ) -> None:
        self.store = store
        self.summarizer = summarizer
        self.policy = policy or FinishedTraces()
        self.archive_steps = archive_steps
        self.agent = agent

    def summary_of(self, root_id: str) -> Memory | None:
        """The live summary of a trace, if it has one."""
        found = self.store.find(kind=SUMMARY, derived_from=root_id, where={"trace": root_id})
        return found[-1] if found else None

    def pending(self) -> list[Trace]:
        """Ended traces with no live summary, oldest first."""
        out = []
        for root in self.store.find(kind=TRACE):
            ended = self.store.find(kind=TRACE_END, derived_from=root.id, where={"trace": root.id})
            if ended and self.summary_of(root.id) is None:
                out.append(Trace.open(self.store, root.id))
        return out

    def compact(self, t: Trace) -> Memory:
        """Write the summary of `t` to long-term memory and archive its steps, in one transaction."""
        body = self.summarizer(t)
        body = {**body, "trace": t.id} if isinstance(body, dict) else {"content": body, "trace": t.id}
        live_steps = [m for m in t.steps if self.store.status(m.id) == "live"]
        importance = max([t.root.importance, *(m.importance for m in t.steps)])
        with self.store.transaction(self.agent) as tx:
            summary = tx.put(
                SUMMARY,
                t.root.key,
                body,
                scope=t.root.scope,
                derived_from=[t.id, *(m.id for m in live_steps)],
                importance=importance,
                tier=LONG,
            )
            if self.archive_steps:
                for m in t.steps:
                    tx.move(m.id, ARCHIVE, f"compacted into {summary.id}")
        return summary

    def run(self) -> list[Memory]:
        """Compact what the policy selects. Returns the summaries written."""
        return [self.compact(t) for t in self.policy.select(self.pending())]
