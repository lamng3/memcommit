"""Agent sessions: one agent's view of the store, and the rule for who may write where.

A memory's scope says who may read it. `shared` is for every agent; a scope
named after an agent (`planner`) or under it (`planner/drafts`) is
that agent's own. An `AgentSession` reads `shared` plus the agent's own
scopes, and every write it makes is stamped with the agent's name.

`scope_guard` is the write rule, enforced at commit for every agent, sessions
or not: an agent may put memories only in `shared` or in its own scopes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from .records import SHARED, Commit, Memory
from .state import State
from .store import SYSTEM, MemoryStore, Transaction
from .traces import Trace, trace


def owns(agent: str, scope: str) -> bool:
    return scope == agent or scope.startswith(agent + "/")


def scope_guard(commit: Commit, state: State) -> list[str]:
    """Reject a put into another agent's scope. The system agent may write anywhere."""
    if commit.agent == SYSTEM:
        return []
    return [
        f"{commit.agent} may not write {m.kind} {m.key!r} into scope {m.scope!r}"
        for m in commit.puts()
        if m.scope != SHARED and not owns(commit.agent, m.scope)
    ]


class AgentSession:
    def __init__(self, store: MemoryStore, agent: str, reads: Iterable[str] = ()) -> None:
        if agent == SYSTEM:
            raise ValueError(f"{SYSTEM!r} is reserved for the store itself")
        self.store = store
        self.agent = agent
        self.extra_reads = frozenset(reads)

    def can_read(self, memory: Memory) -> bool:
        return memory.scope == SHARED or owns(self.agent, memory.scope) or memory.scope in self.extra_reads

    def find(self, **filters: Any) -> list[Memory]:
        return [m for m in self.store.find(**filters) if self.can_read(m)]

    def latest(self, kind: str, key: str) -> Memory | None:
        found = self.find(kind=kind, key=key)
        return max(found, key=lambda m: self.store.state.last[m.id].lsn) if found else None

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self.store.transaction(self.agent) as tx:
            yield tx

    def put(self, kind: str, key: str, body: Any = None, **kwargs: Any) -> Memory:
        return self.store.put(self.agent, kind, key, body, **kwargs)

    def trace(self, task: str, **options: Any) -> AbstractContextManager[Trace]:
        """A run of this agent, as a block. See `mneme.traces.trace`."""
        return trace(self.store, self.agent, task, **options)


def attach_scope_guard(store: MemoryStore) -> MemoryStore:
    store.guard(scope_guard)
    return store
