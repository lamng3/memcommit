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
- `Forgetting`: lowest retention first, on an Ebbinghaus forgetting curve whose
  strength grows with each use and with importance (as in MemoryBank,
  Zhong et al., 2024).
- `TTL`: memories past a time-to-live for their kind first, then a fallback policy.
- `TwoQueue`: 2Q (Johnson and Shasha, 1994). Memories used only at their
  write are evicted first, oldest first; memories used again, or paged back in
  from the archive, are protected and leave least recently used. A scan of
  one-off trace steps cannot flush what agents keep coming back to.

Promotion (short-term -> long-term):

- `AccessCount(n)`: used at least n times.
- `ImportanceAtLeast(x)`: its writer marked it important.
- `KindIn(kinds)`: always keep these kinds (facts, summaries, decisions).
- `AnyOf(...)`, `AllOf(...)`, `Never()`: combinators.

- `ARC`: adaptive replacement (Megiddo and Modha, 2003). Keeps ghost lists of
  what it evicted; a page-in of a ghost shifts its target between recency
  (memories used once) and frequency (memories used again).
- `Learned`: an online logistic model of "will this memory be used again
  soon", trained on the uses it is told about; evicts the least likely first.

A new policy is any object with the same method; nothing registers it. A
policy that keeps state between decisions also defines any of `on_use`,
`on_evict`, and `on_page_in`, and `TieredMemory` calls them.
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


@dataclass(frozen=True)
class Forgetting:
    """Evict the lowest retention `exp(-age / strength)` first; strength = base * uses * (1 + importance)."""

    base: float = 10.0

    def retention(self, c: Candidate, now: int) -> float:
        strength = self.base * max(1, c.count) * (1.0 + c.importance)
        return math.exp(-max(0, now - c.last) / strength)

    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: (self.retention(c, now), c.written))[:n]


class TTL:
    """Evict memories older than their kind's time-to-live (in ticks since written) first, then by `fallback`."""

    def __init__(self, ttl: dict[str, int], fallback: EvictionPolicy | None = None) -> None:
        self.ttl = dict(ttl)
        self.fallback = fallback or LRU()

    def expired(self, c: Candidate, now: int) -> bool:
        limit = self.ttl.get(c.memory.kind)
        return limit is not None and now - c.written > limit

    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        expired = sorted((c for c in candidates if self.expired(c, now)), key=lambda c: c.written)[:n]
        rest = [c for c in candidates if not self.expired(c, now)]
        return expired + self.fallback.victims(rest, n - len(expired), now) if len(expired) < n else expired


class TwoQueue:
    """2Q: evict memories used once (FIFO) before memories used again (LRU)."""

    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        once = sorted((c for c in candidates if c.count <= 1), key=lambda c: c.written)
        again = sorted((c for c in candidates if c.count > 1), key=lambda c: (c.last, c.written))
        return (once + again)[:n]


class ARC:
    """Adaptive replacement: balance recency and frequency by learning from regretted evictions."""

    def __init__(self, ghosts: int = 256) -> None:
        self.ghosts = ghosts
        self.target = 0.0  # how many of the victims' slots favour memories used once (T1)
        self.b1: dict[str, None] = {}  # evicted while used once
        self.b2: dict[str, None] = {}  # evicted after being used again

    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        t1 = sorted((c for c in candidates if c.count <= 1), key=lambda c: (c.last, c.written))
        t2 = sorted((c for c in candidates if c.count > 1), key=lambda c: (c.last, c.written))
        out: list[Candidate] = []
        while len(out) < n and (t1 or t2):
            take_t1 = t1 and (len(t1) > self.target or not t2)
            out.append((t1 if take_t1 else t2).pop(0))
        return out

    def on_evict(self, candidates: Sequence[Candidate]) -> None:
        for c in candidates:
            ghost = self.b1 if c.count <= 1 else self.b2
            ghost[c.id] = None
            while len(ghost) > self.ghosts:
                ghost.pop(next(iter(ghost)))

    def on_page_in(self, candidates: Sequence[Candidate]) -> None:
        for c in candidates:
            if c.id in self.b1:  # a once-used memory came back: favour recency
                self.target += max(1.0, len(self.b2) / max(1, len(self.b1)))
                del self.b1[c.id]
            elif c.id in self.b2:  # a reused memory came back: favour frequency
                self.target = max(0.0, self.target - max(1.0, len(self.b1) / max(1, len(self.b2))))
                del self.b2[c.id]


class Learned:
    """Evict the memories least likely to be used again, by an online logistic model.

    Features: recency (decayed age since last use), log uses, importance, and a bias. Every use is a
    positive example (the memory as it was just before the use); every eviction decision adds the
    candidates idle for longer than `horizon` ticks as negatives. Plain SGD; no dependencies.
    """

    def __init__(self, rate: float = 0.1, horizon: int = 50, decay: float = 0.98) -> None:
        self.rate = rate
        self.horizon = horizon
        self.decay = decay
        self.weights = [1.0, 0.5, 0.5, 0.0]
        self.now = 0
        self.examples = 0

    def features(self, c: Candidate, now: int) -> list[float]:
        return [self.decay ** max(0, now - c.last), math.log1p(c.count), c.importance, 1.0]

    def predict(self, c: Candidate, now: int) -> float:
        z = sum(w * x for w, x in zip(self.weights, self.features(c, now), strict=True))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def learn(self, c: Candidate, now: int, label: float) -> None:
        error = label - self.predict(c, now)
        self.weights = [w + self.rate * error * x for w, x in zip(self.weights, self.features(c, now), strict=True)]
        self.examples += 1

    def victims(self, candidates: Sequence[Candidate], n: int, now: int) -> list[Candidate]:
        self.now = now
        for c in candidates:
            if now - c.last > self.horizon:
                self.learn(c, now, 0.0)
        return sorted(candidates, key=lambda c: (self.predict(c, now), c.written))[:n]

    def on_use(self, candidates: Sequence[Candidate]) -> None:
        for c in candidates:
            self.now = max(self.now, c.last + 1)
            self.learn(c, self.now, 1.0)


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
