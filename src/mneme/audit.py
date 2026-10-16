"""Audit: integrity checks, lineage, and containment for a memory that may have been tampered with.

Shared memory is an attack surface. One agent's poisoned observation becomes
another agent's premise, and a cached answer outlives the fact it came from.
The store's design already resists much of this: memories are
content-addressed, so a body cannot change without its id changing; status
comes from an append-only log; provenance says what every memory was built on.
This module gives a defender the tools that design makes possible.

- `verify(store)`: replay the log and report anything a well-behaved store
  could not have written: ids that do not match bodies, records out of order,
  memories derived from memories that did not exist yet, writes into another
  agent's scope.
- `lineage(store, id)`: what a memory was built on, transitively.
- `blast_radius(store, ref)`: everything built on a reference, in any status.
- `quarantine(store, ids)`: retract suspect memories and mark everything built
  on them stale, in one transaction.
- `diff(store, a, b)`: which memories changed status or tier between two LSNs.
- `provenance_guard`: reject, at write time, a memory derived from a memory
  that does not exist or no longer holds: forged or stale provenance.

None of these decide what is malicious. That is the defender's job; these
make its decisions cheap and its repairs exact.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .records import LIVE, SHARED, Commit, Memory, Record
from .state import State
from .store import SYSTEM, MemoryStore


def _owns(agent: str, scope: str) -> bool:
    return scope == agent or scope.startswith(agent + "/")


def verify(store: MemoryStore, scopes: bool = True) -> list[str]:
    """Problems in the log, in log order. An empty list means the log is consistent."""
    problems: list[str] = []
    seen: set[str] = set()
    last_lsn, last_txn = 0, 0
    for record in store.engine.records():
        m = record.memory
        where = f"lsn {record.lsn}"
        if record.lsn <= last_lsn:
            problems.append(f"{where}: out of order after lsn {last_lsn}")
        if record.txn < last_txn:
            problems.append(f"{where}: transaction {record.txn} after transaction {last_txn}")
        last_lsn, last_txn = max(last_lsn, record.lsn), max(last_txn, record.txn)
        rehashed = Memory.from_json({**m.to_json(), "id": ""})
        if rehashed.id != m.id:
            problems.append(f"{where}: memory {m.id} does not hash to its id")
        if record.op != "put" and m.id not in seen:
            problems.append(f"{where}: {record.op} of {m.id}, which was never put")
        if record.op == "put":
            missing = [r for r in m.memory_refs() if r not in seen]
            if missing:
                problems.append(f"{where}: {m.kind} {m.key!r} is derived from unknown memories {missing}")
            if scopes and record.agent not in (SYSTEM, m.source):
                problems.append(f"{where}: {record.agent} wrote a memory whose source is {m.source!r}")
            if scopes and m.scope != SHARED and record.agent != SYSTEM and not _owns(record.agent, m.scope):
                problems.append(f"{where}: {record.agent} wrote into scope {m.scope!r}")
        seen.add(m.id)
    return problems


def lineage(store: MemoryStore, memory_id: str) -> list[str]:
    """Every memory `memory_id` was built on, transitively, oldest first."""
    state = store.state
    found: set[str] = set()
    frontier = [memory_id]
    while frontier:
        memory = state.get(frontier.pop())
        for ref in memory.memory_refs() if memory else ():
            if ref not in found and ref in state.memories:
                found.add(ref)
                frontier.append(ref)
    return sorted(found, key=lambda m: state.first[m].lsn)


def blast_radius(store: MemoryStore, refs: Iterable[str]) -> list[str]:
    """Every memory built on `refs`, directly or not, in any status, oldest first."""
    state = store.state
    return sorted(state.descendants(refs), key=lambda m: state.first[m].lsn)


def quarantine(store: MemoryStore, memory_ids: Iterable[str], reason: str, agent: str = SYSTEM) -> list[Record]:
    """Retract `memory_ids` and mark stale every live memory built on them, in one transaction."""
    memory_ids = [m for m in memory_ids if m in store.state.memories]
    downstream = [m for m in store.state.dependents(memory_ids) if m not in memory_ids]
    with store.transaction(agent) as tx:
        for memory_id in memory_ids:
            tx.retract(memory_id, reason)
        for memory_id in downstream:
            tx.mark_stale(memory_id, f"built on quarantined memory: {reason}")
    return list(tx.committed.records) if tx.committed else []


@dataclass(frozen=True)
class Change:
    memory: Memory
    before: tuple[str | None, str | None]  # (status, tier) at the first LSN
    after: tuple[str | None, str | None]


def diff(store: MemoryStore, a: int, b: int | None = None) -> list[Change]:
    """Memories whose status or tier differs between LSN `a` and LSN `b` (default: now)."""
    old = store.as_of(a)
    new = store.as_of(b) if b is not None else store.state
    out = []
    for memory_id, memory in new.memories.items():
        before = (old.status.get(memory_id), old.tier.get(memory_id))
        after = (new.status.get(memory_id), new.tier.get(memory_id))
        if before != after:
            out.append(Change(memory, before, after))
    return out


def provenance_guard(allow_stale: bool = False):
    """A guard: reject a memory derived from a memory that does not exist, or no longer holds."""

    def guard(commit: Commit, state: State) -> list[str]:
        written = {m.id for m in commit.puts()}
        problems = []
        for memory in commit.puts():
            for ref in memory.memory_refs():
                if ref in written:
                    continue
                if ref not in state.memories:
                    problems.append(f"{memory.kind} {memory.key!r} is derived from {ref}, which does not exist")
                elif not allow_stale and state.status[ref] != LIVE:
                    problems.append(f"{memory.kind} {memory.key!r} is derived from {ref}, which is {state.status[ref]}")
        return problems

    return guard
