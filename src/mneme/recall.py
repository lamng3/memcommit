"""Recall: find the memories that matter for a query, scored by relevance, recency, and importance.

    recall = Recall(store, tiers=tiers)
    for hit in recall.search("cheapest flight to Lyon", k=5, scopes=["shared", "planner"]):
        print(hit.score, hit.memory.kind, hit.memory.body)

The score is Generative Agents' (Park et al., 2023): a weighted sum of
relevance to the query, recency, and importance, each in [0, 1]. Relevance
comes from an index, which is a `Cache` kept in step with the store by
`CacheSync`, so a stale or retracted memory is never recalled:

- `LexicalIndex`: BM25 over the words of a memory's key and body. No dependencies.
- `VectorIndex`: cosine similarity over embeddings from any `Embedder`, a
  function from texts to vectors (a local model, an API, a test stub).

Recency decays with the number of log records since the memory was last
touched, so it is deterministic and does not depend on wall time. Hits are
touched in the `TieredMemory` given, which is how recall feeds the eviction
and promotion policies. By default the archive is not searched; pass
`tiers=None` in a query to search it too.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .cache import CacheSync
from .records import LONG, SHORT, Memory
from .store import MemoryStore
from .tiers import TieredMemory

WORD = re.compile(r"[a-z0-9]+")

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def text_of(memory: Memory) -> str:
    """The words a memory is searched by: its kind, key, and every string and number in its body."""
    parts = [memory.kind, memory.key]

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, int | float):
            parts.append(str(value))
        elif isinstance(value, dict):
            for k, v in value.items():
                if k not in ("trace", "parent"):  # ids, not words
                    walk(v)
        elif isinstance(value, list | tuple):
            for v in value:
                walk(v)

    walk(memory.body)
    return " ".join(parts)


def words(text: str) -> list[str]:
    return WORD.findall(text.lower())


class LexicalIndex:
    """BM25 over memory text. A `Cache`: admit and evict keep it in step with the store."""

    def __init__(self, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.terms: dict[str, dict[str, int]] = {}  # word -> memory id -> count
        self.lengths: dict[str, int] = {}

    def admit(self, memory: Memory) -> None:
        if memory.id in self.lengths:
            return
        tokens = words(text_of(memory))
        self.lengths[memory.id] = len(tokens)
        for w in tokens:
            postings = self.terms.setdefault(w, {})
            postings[memory.id] = postings.get(memory.id, 0) + 1

    def evict(self, memory: Memory) -> None:
        if self.lengths.pop(memory.id, None) is None:
            return
        for w in set(words(text_of(memory))):
            postings = self.terms.get(w, {})
            postings.pop(memory.id, None)
            if not postings:
                self.terms.pop(w, None)

    def scores(self, query: str) -> dict[str, float]:
        n = len(self.lengths)
        if not n:
            return {}
        average = sum(self.lengths.values()) / n
        out: dict[str, float] = {}
        for w in set(words(query)):
            postings = self.terms.get(w, {})
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for memory_id, tf in postings.items():
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[memory_id] / average)
                out[memory_id] = out.get(memory_id, 0.0) + idf * tf * (self.k1 + 1) / norm
        return out


class VectorIndex:
    """Cosine similarity over embeddings. A `Cache`, like `LexicalIndex`."""

    def __init__(self, embed: Embedder) -> None:
        self.embed = embed
        self.vectors: dict[str, list[float]] = {}

    def admit(self, memory: Memory) -> None:
        if memory.id not in self.vectors:
            self.vectors[memory.id] = _unit(self.embed([text_of(memory)])[0])

    def evict(self, memory: Memory) -> None:
        self.vectors.pop(memory.id, None)

    def scores(self, query: str) -> dict[str, float]:
        if not self.vectors:
            return {}
        q = _unit(self.embed([query])[0])
        return {m: max(0.0, sum(a * b for a, b in zip(q, v, strict=True))) for m, v in self.vectors.items()}


def _unit(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


@dataclass(frozen=True)
class Hit:
    memory: Memory
    score: float
    relevance: float
    recency: float
    importance: float


class Recall:
    def __init__(
        self,
        store: MemoryStore,
        index: LexicalIndex | VectorIndex | None = None,
        tiers: TieredMemory | None = None,
        relevance: float = 1.0,
        recency: float = 0.5,
        importance: float = 0.5,
        decay: float = 0.995,
    ) -> None:
        self.store = store
        self.index = index or LexicalIndex()
        self.sync = CacheSync(store, self.index)
        self.tiers = tiers
        self.weights = (relevance, recency, importance)
        self.decay = decay

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        kinds: Iterable[str] | None = None,
        scopes: Iterable[str] | None = None,
        tiers: Iterable[str] | None = (SHORT, LONG),
        where: dict[str, Any] | None = None,
        touch: bool = True,
    ) -> list[Hit]:
        """The `k` best live memories for `query` among those that pass every filter."""
        raw = self.index.scores(query)
        top = max(raw.values(), default=0.0) or 1.0
        state = self.store.state
        kinds = set(kinds) if kinds is not None else None
        allowed = {m.id for m in state.find(scopes=scopes, tiers=tiers, where=where)}
        w_rel, w_rec, w_imp = self.weights
        hits = []
        for memory_id in allowed:
            memory = state.memories[memory_id]
            if kinds is not None and memory.kind not in kinds:
                continue
            relevance = raw.get(memory_id, 0.0) / top
            if relevance == 0.0 and query.strip():
                continue
            recency = self.decay ** (state.lsn - state.last[memory_id].lsn)
            importance = min(1.0, max(0.0, memory.importance))
            score = w_rel * relevance + w_rec * recency + w_imp * importance
            hits.append(Hit(memory, score, relevance, recency, importance))
        hits.sort(key=lambda h: (-h.score, -state.first[h.memory.id].lsn))
        hits = hits[:k]
        if touch and self.tiers is not None:
            self.tiers.touch(h.memory.id for h in hits)
        return hits
