"""The memory store: transactions over a log, with guards before a commit and observers after it.

    store = MemoryStore(JsonlEngine("memory.jsonl"))
    with store.transaction("kroma.matcher") as tx:
        match = tx.put("match", "Metal=MetallicMaterial", {"confidence": 0.93}, derived_from=[request.id])
    store.invalidate(["ontology:materials@3"], reason="rolled back")   # and everything derived from it
    store.rollback_to(12)                                               # compensation, history kept

Every write goes through a transaction. On commit, the store numbers the
records, asks every guard whether the commit may happen, appends the records
to the engine in one call, applies them to the state, and then tells every
observer. A transaction that raises writes nothing.

Guards are the extension point for policy that must hold (scopes, provenance
rules, a defense layer's checks); observers are the extension point for
reactions (caches, schedulers, metrics). Neither is allowed to commit while
the store is committing: an observer that wants to write queues the work and
does it after, which keeps every commit's effects in one place.

Rollback is compensation, as in ARIES: the store computes the state at the
target LSN and writes the records that move every memory back to its status
there. Nothing is erased, so the log still shows what happened and what undid it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .engines import Engine, MemoryEngine
from .records import LIVE, OPS, RETRACTED, SHARED, STALE, Commit, Memory, Record
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
        self.ops: list[tuple[str, Memory, str]] = []
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
    ) -> Memory:
        """Write a memory, stamped with this transaction's agent. Rewriting a live memory is a no-op."""
        memory = Memory(kind, key, body, source=self.agent, scope=scope, derived_from=tuple(derived_from))
        self.ops.append(("put", memory, ""))
        return memory

    def retract(self, memory_id: str, reason: str = "") -> None:
        """Withdraw a memory: it was wrong, not merely outdated."""
        self.ops.append(("retract", self._existing(memory_id), reason))

    def mark_stale(self, memory_id: str, reason: str = "") -> None:
        """Mark a memory outdated: something it was derived from no longer holds."""
        self.ops.append(("stale", self._existing(memory_id), reason))

    def restore(self, memory_id: str, reason: str = "") -> None:
        self.ops.append(("restore", self._existing(memory_id), reason))

    def _existing(self, memory_id: str) -> Memory:
        memory = self.store.state.get(memory_id) or next(
            (m for op, m, _ in self.ops if op == "put" and m.id == memory_id), None
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
    ) -> None:
        self.engine = engine or MemoryEngine()
        records = self.engine.records()
        self.state = State.replay(records)
        self._txn = max((r.txn for r in records), default=0)
        self.guards: list[Guard] = list(guards)
        self.observers: list[Observer] = list(observers)
        self._committing = False

    # Reads ---------------------------------------------------------------

    @property
    def lsn(self) -> int:
        return self.state.lsn

    def get(self, memory_id: str) -> Memory | None:
        return self.state.get(memory_id)

    def status(self, memory_id: str) -> str | None:
        return self.state.status.get(memory_id)

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
        """Write the records that return every memory to its status as of `lsn`. Returns them."""
        if lsn >= self.lsn:
            return []
        target = self.as_of(lsn)
        reason = reason or f"rollback to lsn {lsn}"
        with self.transaction(agent) as tx:
            for memory_id, status in self.state.status.items():
                wanted = target.status.get(memory_id, RETRACTED)
                if wanted == status:
                    continue
                if wanted == LIVE:
                    tx.restore(memory_id, reason)
                elif wanted == STALE:
                    tx.mark_stale(memory_id, reason)
                else:
                    tx.retract(memory_id, reason)
        return list(tx.committed.records) if tx.committed else []

    def _commit(self, tx: Transaction) -> Commit | None:
        if self._committing:
            raise RuntimeError("a guard or observer tried to commit during a commit; queue the work instead")
        records = []
        lsn = self.lsn
        statuses: dict[str, str | None] = {}
        for op, memory, reason in tx.ops:
            current = statuses.get(memory.id, self.state.status.get(memory.id))
            if op == "put" and current is not None:
                op = "restore"
            if OPS[op] == current:
                continue  # already in that status: nothing to write
            lsn += 1
            record = Record(lsn, self._txn + 1, op, memory, tx.agent, reason)
            statuses[memory.id] = record.status
            records.append(record)
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
        finally:
            self._committing = False
        return commit
