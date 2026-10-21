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

Which traces to compact is the `CompactionPolicy`'s choice:

- `FinishedTraces(keep_recent)`: every ended trace, except the newest few.
- `OlderThan(records)`: traces that ended at least `records` log records ago.
- `KeepFailed(inner, records)`: as `inner`, but a failed trace stays whole
  (its steps are what a debugger or a defender reads) until it is older.
- `UnderPressure(tiers, fraction, inner)`: as `inner`, but only while some
  short-term partition is past `fraction` of its capacity.

`Compactor.merge(by)` compacts summaries in turn: summaries of the same task
(or any key) fold into one, derived from all of them, so a task an agent ran
fifty times leaves one memory of how it tends to go.
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


def _end_lsn(t: Trace) -> int:
    ends = [m for m in t.steps if m.kind == TRACE_END]
    return t.store.state.first[ends[-1].id].lsn if ends else t.store.lsn


class OlderThan:
    """Compact traces that ended at least `records` log records ago."""

    def __init__(self, records: int) -> None:
        self.records = records

    def select(self, traces: Sequence[Trace]) -> list[Trace]:
        return [t for t in traces if t.store.lsn - _end_lsn(t) >= self.records]


class KeepFailed:
    """As `inner`, but keep failed traces whole until they ended `records` log records ago."""

    def __init__(self, inner: CompactionPolicy | None = None, records: int = 1000) -> None:
        self.inner = inner or FinishedTraces()
        self.records = records

    def select(self, traces: Sequence[Trace]) -> list[Trace]:
        return [t for t in self.inner.select(traces) if t.status() == "ok" or t.store.lsn - _end_lsn(t) >= self.records]


class UnderPressure:
    """As `inner`, but only while a short-term partition is past `fraction` of its capacity."""

    def __init__(self, tiers: Any, fraction: float = 0.8, inner: CompactionPolicy | None = None) -> None:
        self.tiers = tiers
        self.fraction = fraction
        self.inner = inner or FinishedTraces()

    def select(self, traces: Sequence[Trace]) -> list[Trace]:
        counts: dict[str, int] = {}
        state = self.tiers.store.state
        for memory_id, tier in state.tier.items():
            if tier == "short":
                p = self.tiers.partition(state.memories[memory_id])
                counts[p] = counts.get(p, 0) + 1
        if max(counts.values(), default=0) < self.fraction * self.tiers.capacity:
            return []
        return self.inner.select(traces)


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

    def merge(self, by: Callable[[Memory], str] | None = None, minimum: int = 2) -> list[Memory]:
        """Fold live trace summaries that share a key (the task, by default) into one merged summary each."""
        by = by or (lambda m: m.body.get("task", m.key))
        groups: dict[str, list[Memory]] = {}
        for summary in self.store.find(kind=SUMMARY, tiers=["short", "mid", "long"]):  # archived: merged already
            if summary.body.get("trace"):
                groups.setdefault(by(summary), []).append(summary)
        merged = []
        for key, summaries in groups.items():
            if len(summaries) < minimum:
                continue
            statuses: dict[str, int] = {}
            for m in summaries:
                statuses[str(m.body.get("status"))] = statuses.get(str(m.body.get("status")), 0) + 1
            body = {
                "merged": key,
                "runs": len(summaries),
                "statuses": statuses,
                "tools": sorted({t for m in summaries for t in m.body.get("tools", []) if t}),
                "results": [r for m in summaries for r in m.body.get("results", [])][-10:],
                "errors": [e for m in summaries for e in m.body.get("errors", [])][-10:],
            }
            with self.store.transaction(self.agent) as tx:
                merged_memory = tx.put(
                    SUMMARY,
                    f"merged:{key}",
                    body,
                    scope=summaries[0].scope,
                    derived_from=[m.id for m in summaries],
                    importance=max(m.importance for m in summaries),
                    tier=LONG,
                )
                for m in summaries:
                    tx.move(m.id, ARCHIVE, f"merged into {merged_memory.id}")
            merged.append(merged_memory)
        return merged
