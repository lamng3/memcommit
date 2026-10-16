"""Policies: which memories leave working memory, and which are worth keeping long-term.

A policy sees `Candidate`s, never the store: a memory with the numbers a
decision needs (when it was written, how often and how lately it was used,
how important its writer said it was). That keeps policies small, testable on
their own, and swappable without touching the store. `TieredMemory` builds
the candidates and carries out the decisions as `move` records, so every
decision a policy makes is in the log.

Eviction (short-term over capacity -> archive):

- `FIFO`: oldest written first.
- `LRU`: least recently used first.
- `LFU`: least often used first, ties by recency.
- `Scored`: lowest weighted sum of recency, importance, and frequency first,
  the retrieval score of Generative Agents (Park et al., 2023) used in reverse.

Promotion (short-term -> long-term):

- `AccessCount(n)`: used at least n times.
- `ImportanceAtLeast(x)`: its writer marked it important.
- `KindIn(kinds)`: always keep these kinds (facts, summaries, decisions).
- `AnyOf(...)`, `AllOf(...)`, `Never()`: combinators.

A new policy is any object with the same method; nothing registers it.
Learned policies (a model scoring candidates) fit the same shape.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .records import Memory


@dataclass(frozen=True)
class Candidate:
    """A memory as a policy sees it. Times are ticks of the access clock, not seconds."""

    memory: Memory
    tier: str
    status: str
    written: int  # tick it was first written
    last: int  # tick of its last use (its write counts as one)
    count: int  # uses, its write included

    @property
    def id(self) -> str:
        return self.memory.id

    @property
    def importance(self) -> float:
        return self.memory.importance


class EvictionPolicy(Protocol):
    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        """The `n` candidates to evict first. `now` is the current tick."""


class PromotionPolicy(Protocol):
    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        """The candidates to promote to long-term memory."""


# Eviction ------------------------------------------------------------------


class FIFO:
    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: c.written)[:n]


class LRU:
    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: (c.last, c.written))[:n]


class LFU:
    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: (c.count, c.last, c.written))[:n]


@dataclass(frozen=True)
class Scored:
    """Evict the lowest `recency * decay^age + importance * w + log(1 + uses) * w` first."""

    recency: float = 1.0
    importance: float = 1.0
    frequency: float = 0.5
    decay: float = 0.99

    def score(self, c: Candidate, now: int) -> float:
        return (
            self.recency * self.decay ** max(0, now - c.last)
            + self.importance * c.importance
            + self.frequency * math.log1p(c.count)
        )

    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: (self.score(c, now), c.written))[:n]


# Promotion -----------------------------------------------------------------


@dataclass(frozen=True)
class AccessCount:
    minimum: int = 3

    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        return [c for c in candidates if c.count >= self.minimum]


@dataclass(frozen=True)
class ImportanceAtLeast:
    threshold: float = 0.7

    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        return [c for c in candidates if c.importance >= self.threshold]


class KindIn:
    def __init__(self, kinds: Iterable[str]) -> None:
        self.kinds = frozenset(kinds)

    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        return [c for c in candidates if c.memory.kind in self.kinds]


class AnyOf:
    def __init__(self, *policies: PromotionPolicy) -> None:
        self.policies = policies

    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        chosen = {c.id for p in self.policies for c in p.select(candidates, now)}
        return [c for c in candidates if c.id in chosen]


class AllOf:
    def __init__(self, *policies: PromotionPolicy) -> None:
        self.policies = policies

    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        chosen = [{c.id for c in p.select(candidates, now)} for p in self.policies]
        return [c for c in candidates if all(c.id in ids for ids in chosen)]


class Never:
    def select(self, candidates: Sequence[Candidate], now: int) -> list[Candidate]:
        return []
