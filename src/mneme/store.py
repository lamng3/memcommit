"""The memory store: transactions over a log, with guards before a commit and observers after it.

    store = MemoryStore(JsonlEngine("memory.jsonl"))
    with store.transaction("planner") as tx:
        plan = tx.put("plan", "trip", {"steps": ["book", "pack"]}, derived_from=["doc:itinerary@3"])
    store.invalidate(["doc:itinerary@3"], reason="the itinerary changed")   # and everything derived from it
    store.rollback_to(12)                                                   # compensation, history kept

Every write goes through a transaction. On commit, the store numbers the
records, asks every guard whether the commit may happen, appends the records
to the engine in one call, applies them to the state, and then tells every
observer. A transaction that raises writes nothing.

A commit that brings a memory live also marks stale, in the same
transaction, every memory built on a read (`query_ref`) whose result that
memory changes. Memories written by the commit itself are left alone.

Guards are the extension point for rules that must hold (scopes, provenance,
a defender's checks); observers are the extension point for reactions
(caches, schedulers, tier policies). Neither may commit while the store is
committing. An observer that wants to write calls `defer(work)`, and the store
runs the work once the commit is done, in order, so every commit's effects
stay in one place and policies can still write.

Rollback is compensation, as in ARIES: the store computes the state at the
target LSN and writes the records that move every memory back to its status
and tier there. Nothing is erased, so the log still shows what happened and
what undid it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .engines import Engine, MemoryEngine
from .records import LIVE, OPS, RETRACTED, SHARED, SHORT, STALE, TIERS, Commit, Memory, Record
from .state import State

# guard(commit, state) -> problems; an empty result lets the commit through.
Guard = Callable[[Commit, State], Iterable[str]]
# observer(commit, state) -> None; called after the commit is durable.
Observer = Callable[[Commit, State], None]

SYSTEM = "mneme"


class Rejected(RuntimeError):
    """A guard refused a commit. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class Transaction:
    """Writes collected for one commit. Use it through `MemoryStore.transaction`."""

    def __init__(self, store: MemoryStore, agent: str) -> None:
        self.store = store
        self.agent = agent
        # (op, memory, reason, tier)
        self.ops: list[tuple[str, Memory, str, str]] = []
        # Set when the block ends: what was written, or None if every op was a no-op.
        self.committed: Commit | None = None

    def put(
        self,
        kind: str,
        key: str,
        body: Any = None,
        *,
        scope: str = SHARED,
        derived_from: Iterable[str] = (),
        importance: float = 0.0,
        tier: str = SHORT,
    ) -> Memory:
        """Write a memory, stamped with this transaction's agent. Rewriting a live memory is a no-op."""
        memory = Memory(
            kind, key, body, source=self.agent, scope=scope, derived_from=tuple(derived_from), importance=importance
        )
        self.ops.append(("put", memory, "", tier))
        return memory

    def retract(self, memory_id: str, reason: str = "") -> None:
        """Withdraw a memory: it was wrong, not merely outdated."""
        self.ops.append(("retract", self._existing(memory_id), reason, ""))

    def mark_stale(self, memory_id: str, reason: str = "") -> None:
        """Mark a memory outdated: something it was derived from no longer holds."""
        self.ops.append(("stale", self._existing(memory_id), reason, ""))

    def restore(self, memory_id: str, reason: str = "") -> None:
        self.ops.append(("restore", self._existing(memory_id), reason, ""))

    def move(self, memory_id: str, tier: str, reason: str = "") -> None:
        """Place a memory in another tier. Its status does not change."""
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")
        self.ops.append(("move", self._existing(memory_id), reason, tier))

    def _existing(self, memory_id: str) -> Memory:
        memory = self.store.state.get(memory_id) or next(
            (m for op, m, _, _ in self.ops if op == "put" and m.id == memory_id), None
        )
        if memory is None:
            raise KeyError(f"no memory {memory_id}")
        return memory


class MemoryStore:
    def __init__(
        self,
        engine: Engine | None = None,
        guards: Sequence[Guard] = (),
        observers: Sequence[Observer] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.engine = engine or MemoryEngine()
        records = self.engine.records()
        self.state = State.replay(records)
        self._txn = max((r.txn for r in records), default=0)
        self.guards: list[Guard] = list(guards)
        self.observers: list[Observer] = list(observers)
        self.clock = clock
        self._committing = False
        self._deferred: deque[Callable[[], Any]] = deque()
        self._draining = False

    # Reads ---------------------------------------------------------------

    @property
    def lsn(self) -> int:
        return self.state.lsn

    def get(self, memory_id: str) -> Memory | None:
        return self.state.get(memory_id)

    def status(self, memory_id: str) -> str | None:
        return self.state.status.get(memory_id)

    def tier(self, memory_id: str) -> str | None:
        return self.state.tier.get(memory_id)

    def find(self, **filters: Any) -> list[Memory]:
        """See `State.find` for the filters."""
        return list(self.state.find(**filters))

    def latest(self, kind: str, key: str) -> Memory | None:
        """The live memory of this kind and key written last."""
        found = list(self.state.find(kind=kind, key=key))
        return max(found, key=lambda m: self.state.last[m.id].lsn) if found else None

    def history(self, memory_id: str | None = None) -> list[Record]:
        """Every record, or every record of one memory, in log order."""
        return [r for r in self.engine.records() if memory_id is None or r.memory.id == memory_id]

    def as_of(self, lsn: int) -> State:
        """The state right after `lsn`, rebuilt from the log."""
        return State.replay(self.engine.records(), until=lsn)

    # Extension points ----------------------------------------------------

    def guard(self, guard: Guard) -> None:
        self.guards.append(guard)

    def subscribe(self, observer: Observer) -> None:
        self.observers.append(observer)

    def defer(self, work: Callable[[], Any]) -> None:
        """Run `work` once the current commit is done (now, if none is running). Work may commit."""
        self._deferred.append(work)
        if not self._committing:
            self._drain()

    # Writes --------------------------------------------------------------

    @contextmanager
    def transaction(self, agent: str) -> Iterator[Transaction]:
        """Collect writes; commit them when the block ends, or write nothing if it raises."""
        tx = Transaction(self, agent)
        yield tx
        tx.committed = self._commit(tx)

    def put(self, agent: str, kind: str, key: str, body: Any = None, **kwargs: Any) -> Memory:
        """One memory in its own transaction."""
        with self.transaction(agent) as tx:
            memory = tx.put(kind, key, body, **kwargs)
        return memory

    def move(self, memory_ids: Iterable[str], tier: str, reason: str = "", agent: str = SYSTEM) -> list[str]:
        """Move memories to `tier` in one transaction. Returns the ids that moved."""
        moved = [m for m in memory_ids if self.state.tier.get(m) not in (None, tier)]
        if moved:
            with self.transaction(agent) as tx:
                for memory_id in moved:
                    tx.move(memory_id, tier, reason)
        return moved

    def invalidate(self, refs: Iterable[str], reason: str, agent: str = SYSTEM) -> list[str]:
        """Mark stale every live memory derived from `refs`, transitively, in one transaction.

        Memory ids in `refs` are marked stale themselves too. Returns the ids marked.
        """
        refs = list(refs)
        targets = [r for r in refs if self.state.is_live(r)]
        targets += [m for m in self.state.dependents(refs) if m not in targets]
        if targets:
            with self.transaction(agent) as tx:
                for memory_id in targets:
                    tx.mark_stale(memory_id, reason)
        return targets

    def rollback_to(self, lsn: int, reason: str = "", agent: str = SYSTEM) -> list[Record]:
        """Write the records that return every memory to its status and tier as of `lsn`. Returns them."""
        if lsn >= self.lsn:
            return []
        target = self.as_of(lsn)
        reason = reason or f"rollback to lsn {lsn}"
        with self.transaction(agent) as tx:
            for memory_id, status in self.state.status.items():
                wanted = target.status.get(memory_id, RETRACTED)
                if wanted != status:
                    if wanted == LIVE:
                        tx.restore(memory_id, reason)
                    elif wanted == STALE:
                        tx.mark_stale(memory_id, reason)
                    else:
                        tx.retract(memory_id, reason)
                tier = target.tier.get(memory_id)
                if tier is not None and tier != self.state.tier[memory_id]:
                    tx.move(memory_id, tier, reason)
        return list(tx.committed.records) if tx.committed else []

    def _commit(self, tx: Transaction) -> Commit | None:
        if self._committing:
            raise RuntimeError("a guard or observer tried to commit during a commit; call store.defer(work) instead")
        records = []
        lsn = self.lsn
        now = self.clock()
        statuses: dict[str, str | None] = {}
        tiers: dict[str, str | None] = {}
        for op, memory, reason, tier in tx.ops:
            current = statuses.get(memory.id, self.state.status.get(memory.id))
            if op == "move":
                if tiers.get(memory.id, self.state.tier.get(memory.id)) == tier:
                    continue
                tiers[memory.id] = tier
            else:
                if op == "put" and current is not None:
                    op, tier = "restore", ""
                if OPS[op] == current:
                    continue  # already in that status: nothing to write
                statuses[memory.id] = OPS[op]
                if op == "put":
                    tiers[memory.id] = tier
            lsn += 1
            records.append(Record(lsn, self._txn + 1, op, memory, tx.agent, reason, tier, now))
        records += self._changed_reads(records, statuses, tx.agent, lsn, now)
        if not records:
            return None
        commit = Commit(self._txn + 1, tx.agent, tuple(records))
        self._committing = True
        try:
            problems = [p for guard in self.guards for p in guard(commit, self.state)]
            if problems:
                raise Rejected(problems)
            self.engine.append(records)
            self._txn += 1
            for record in records:
                self.state.apply(record)
            for observer in self.observers:
                observer(commit, self.state)
        except BaseException:
            self._deferred.clear()  # work queued for a commit that did not happen
            raise
        finally:
            self._committing = False
        self._drain()
        return commit

    def _drain(self) -> None:
        if self._draining:
            return  # the outer drain runs it
        self._draining = True
        try:
            while self._deferred:
                self._deferred.popleft()()
        finally:
            self._draining = False

    def _changed_reads(
        self, records: list[Record], statuses: dict[str, str | None], agent: str, lsn: int, now: float
    ) -> list[Record]:
        """Stale records for memories derived from reads that `records` change by bringing a memory live."""
        written = {r.memory.id for r in records}
        out: list[Record] = []
        for record in list(records):
            if record.status != LIVE or self.state.is_live(record.memory.id):
                continue
            refs = self.state.reads_of(record.memory)
            reason = f"{record.memory.kind} {record.memory.key!r} changes a read this was built on"
            for memory_id in self.state.dependents(refs):
                if memory_id in written or statuses.get(memory_id, LIVE) != LIVE:
                    continue
                lsn += 1
                out.append(Record(lsn, self._txn + 1, "stale", self.state.memories[memory_id], agent, reason, "", now))
                statuses[memory_id] = STALE
        return out
