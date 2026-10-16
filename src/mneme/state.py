"""The store's state: a fold over the log, with the indexes reads and invalidation need.

`State.apply(record)` is the only way state changes, so replaying a log
rebuilds exactly the state that wrote it, and `as_of(lsn)` is a replay that
stops early. Three indexes are kept as records are applied:

- by (kind, key), for the reads agents make most;
- by reference, from each reference to the memories derived from it, so the
  memories an invalidation reaches are found without a scan;
- by kind, the query references (`find:...`) memories were derived from, so a
  commit finds the reads a new memory changes without a scan.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .records import LIVE, SHORT, Memory, Record, body_has, matches, parse_query


@dataclass
class State:
    lsn: int = 0
    memories: dict[str, Memory] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    tier: dict[str, str] = field(default_factory=dict)
    # The record that first wrote each memory, and the newest record of each.
    first: dict[str, Record] = field(default_factory=dict)
    last: dict[str, Record] = field(default_factory=dict)
    _by_key: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    _children: dict[str, set[str]] = field(default_factory=dict)
    _queries: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def replay(cls, records: Iterable[Record], until: int | None = None) -> State:
        state = cls()
        for record in records:
            if until is not None and record.lsn > until:
                break
            state.apply(record)
        return state

    def apply(self, record: Record) -> None:
        if record.lsn <= self.lsn:
            raise ValueError(f"record lsn {record.lsn} is not after {self.lsn}: the log is out of order")
        memory = record.memory
        if memory.id not in self.memories:
            if record.op != "put":
                raise ValueError(f"record lsn {record.lsn} {record.op}s {memory.id}, which was never put")
            self.memories[memory.id] = memory
            self.first[memory.id] = record
            self.tier[memory.id] = record.tier or SHORT
            self._by_key.setdefault((memory.kind, memory.key), []).append(memory.id)
            for ref in memory.derived_from:
                self._children.setdefault(ref, set()).add(memory.id)
                query = parse_query(ref)
                if query is not None:
                    self._queries.setdefault(query["kind"], {})[ref] = query
        if record.op == "move":
            self.tier[memory.id] = record.tier
        else:
            self.status[memory.id] = record.status
        self.last[memory.id] = record
        self.lsn = record.lsn

    def get(self, memory_id: str) -> Memory | None:
        return self.memories.get(memory_id)

    def is_live(self, memory_id: str) -> bool:
        return self.status.get(memory_id) == LIVE

    def find(
        self,
        kind: str | None = None,
        key: str | None = None,
        source: str | None = None,
        scopes: Iterable[str] | None = None,
        derived_from: str | None = None,
        status: str | None = LIVE,
        where: dict[str, Any] | None = None,
        tiers: Iterable[str] | None = None,
    ) -> Iterator[Memory]:
        """Memories matching every filter given, oldest first. status=None matches any status.

        `where` matches body fields: `where={"subject": "Metal"}` keeps memories whose body has subject "Metal".
        `tiers` keeps memories in those tiers; None keeps every tier.
        """
        if kind is not None and key is not None:
            ids: Iterable[str] = self._by_key.get((kind, key), [])
        elif derived_from is not None:
            ids = sorted(self._children.get(derived_from, ()), key=lambda i: self.first[i].lsn)
        else:
            ids = self.memories
        scopes = set(scopes) if scopes is not None else None
        tiers = set(tiers) if tiers is not None else None
        for memory_id in ids:
            m = self.memories[memory_id]
            if (
                (kind is None or m.kind == kind)
                and (key is None or m.key == key)
                and (source is None or m.source == source)
                and (scopes is None or m.scope in scopes)
                and (derived_from is None or derived_from in m.derived_from)
                and (status is None or self.status[memory_id] == status)
                and (tiers is None or self.tier[memory_id] in tiers)
                and body_has(m.body, where)
            ):
                yield m

    def children(self, ref: str) -> set[str]:
        """Memories derived directly from `ref`, in any status."""
        return set(self._children.get(ref, ()))

    def descendants(self, refs: Iterable[str]) -> set[str]:
        """Memories derived from any of `refs`, directly or through other memories, in any status."""
        found: set[str] = set()
        frontier = list(refs)
        while frontier:
            for child in self._children.get(frontier.pop(), ()):
                if child not in found:
                    found.add(child)
                    frontier.append(child)
        return found

    def dependents(self, refs: Iterable[str]) -> list[str]:
        """Live memories derived from any of `refs`, directly or through other memories, in log order."""
        return sorted((m for m in self.descendants(refs) if self.is_live(m)), key=lambda m: self.last[m].lsn)

    def reads_of(self, memory: Memory) -> list[str]:
        """The query references whose results include `memory`: the reads it changes by going live."""
        return [ref for ref, query in self._queries.get(memory.kind, {}).items() if matches(query, memory)]
