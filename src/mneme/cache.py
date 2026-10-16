"""Caches over the store: a fast read path that never outlives the memory behind it.

The store is the source of truth. A cache (an agent's hot answers, a prefix
trie, an embedding index) keeps a copy of some memories in whatever shape
makes its lookups fast. `CacheSync` keeps any cache in step with the store:
it admits a memory of the watched kinds when it goes live, and evicts it the
moment it is retracted or goes stale. A cache only has to implement `Cache`.

`DictCache` is the simplest one, keyed by (kind, key).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from .records import LIVE, Commit, Memory
from .state import State
from .store import MemoryStore


class Cache(Protocol):
    def admit(self, memory: Memory) -> None: ...

    def evict(self, memory: Memory) -> None: ...


class CacheSync:
    """Observer: mirror live memories of `kinds` into `cache`, and evict the rest."""

    def __init__(self, store: MemoryStore, cache: Cache, kinds: Iterable[str]) -> None:
        self.cache = cache
        self.kinds = frozenset(kinds)
        self.admitted = 0
        self.evicted = 0
        for kind in sorted(self.kinds):
            for memory in store.find(kind=kind):
                self._admit(memory)
        store.subscribe(self)

    def _admit(self, memory: Memory) -> None:
        self.cache.admit(memory)
        self.admitted += 1

    def __call__(self, commit: Commit, state: State) -> None:
        for record in commit.records:
            if record.memory.kind not in self.kinds:
                continue
            if record.status == LIVE:
                self._admit(record.memory)
            else:
                self.cache.evict(record.memory)
                self.evicted += 1


@dataclass
class DictCache:
    entries: dict[tuple[str, str], Memory] = field(default_factory=dict)

    def admit(self, memory: Memory) -> None:
        self.entries[(memory.kind, memory.key)] = memory

    def evict(self, memory: Memory) -> None:
        if self.entries.get((memory.kind, memory.key)) == memory:
            del self.entries[(memory.kind, memory.key)]

    def get(self, kind: str, key: str) -> Memory | None:
        return self.entries.get((kind, key))
