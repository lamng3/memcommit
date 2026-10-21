"""Tiered memory: a short-term cache in front of the store, an optional mid-term, long-term, and an archive.

    tiers = TieredMemory(store, capacity=64, eviction=LRU(), promotion=AccessCount(3),
                         compactor=Compactor(store))
    tiers.touch([m.id for m in hits])            # an agent used these: a cache hit or a miss
    tiers.working(scopes=["shared", "planner"])  # what is in its working memory now
    tiers.stats                                  # hits, misses, page-ins, evictions

Short-term memory is a cache. The store holds every memory; short-term holds
the few an agent works with now, bounded per partition, and `touch` is a
cache access: a hit when the memory is there, a miss when it is not, and a
missed memory that is still live is paged back in (`page_in=True`), the way
MemGPT pages archived context back into the prompt. Eviction makes room.

The tiers follow the memory hierarchies agent systems have converged on:
MemGPT's main context, recall, and archival storage (Packer et al., 2023),
MemoryOS's short-, mid-, and long-term stores with heat-based promotion
(Kang et al., 2025), and Generative Agents' recency, importance, and relevance
(Park et al., 2023). Every move between tiers is a record in the log, so "why
did the agent forget that" has an answer, and rollback undoes a policy's
decisions with everything else.

`maintain()` does, in order:

1. **Compact** finished traces into long-term summaries (when a `Compactor` is given).
2. **Promote** short-term memories the promotion policy selects to long-term.
3. **Evict** from each short-term partition over capacity: memories that no
   longer hold go first, then the eviction policy's victims. They go to the
   archive, or to mid-term when `mid_capacity` is set.
4. **Mid-term** (when set): memories are grouped into segments (`segment`, by
   trace by default). A segment whose heat (uses, decayed by age) reaches
   `heat_threshold` is promoted to long-term whole; while mid-term is over
   capacity, the coldest segment is consolidated: summarized into one
   long-term memory by `segment_summarizer` if one is given, and archived.
5. **Trim** long-term memory to `long_capacity`, if one is set.

Capacity is per partition, a memory's scope by default, so each agent's
working memory is bounded on its own. With `auto=True` the tiers run
`maintain()` after any commit that leaves a partition over capacity, through
`store.defer`.

Policies may keep state between decisions (ARC's ghost lists, a learned
model). A policy that defines `on_use`, `on_evict`, or `on_page_in` is told
about each use, each eviction, and each page-in, with the candidates involved.

Use is counted by an `AccessLog`: a logical clock that ticks on every write
and every `touch`. It is not in the store's log, because reads are many and
cheap; `save` and `load` keep it across restarts. With `record=True` it also
keeps every write and use in order, which `bench.from_access_log` replays to
compare policies on what agents actually did.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compaction import SUMMARY, Compactor
from .policies import LRU, Candidate, EvictionPolicy, Never, PromotionPolicy
from .records import ARCHIVE, LIVE, LONG, MID, SHORT, Commit, Memory
from .state import State
from .store import SYSTEM, MemoryStore

PAGED_IN = "paged in on use"


def by_scope(memory: Memory) -> str:
    return memory.scope


def by_trace(memory: Memory) -> str:
    """A memory's segment: its trace, if it is a trace root or step, else its kind."""
    if memory.kind == "trace":
        return memory.id
    if isinstance(memory.body, dict) and memory.body.get("trace"):
        return str(memory.body["trace"])
    return memory.kind


@dataclass
class Use:
    first: int
    last: int
    count: int = 1


@dataclass
class AccessLog:
    """How often and how lately each memory was used, on a logical clock. Optionally, every event in order."""

    now: int = 0
    uses: dict[str, Use] = field(default_factory=dict)
    record: bool = False
    events: list[tuple[str, str]] = field(default_factory=list)  # ("write" | "use", memory id)

    def touch(self, memory_ids: Iterable[str], write: bool = False) -> None:
        for memory_id in memory_ids:
            self.now += 1
            use = self.uses.get(memory_id)
            if use is None:
                self.uses[memory_id] = Use(self.now, self.now)
            else:
                use.last = self.now
                use.count += 1
            if self.record:
                self.events.append(("write" if write else "use", memory_id))

    def get(self, memory_id: str) -> Use | None:
        return self.uses.get(memory_id)

    def save(self, path: str | Path) -> None:
        data = {
            "now": self.now,
            "uses": {k: [u.first, u.last, u.count] for k, u in self.uses.items()},
            "record": self.record,
            "events": self.events,
        }
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> AccessLog:
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        events = [tuple(e) for e in data.get("events", [])]
        return cls(data["now"], {k: Use(*v) for k, v in data["uses"].items()}, data.get("record", False), events)


@dataclass
class CacheStats:
    """Short-term memory as a cache: what `touch` found."""

    hits: int = 0  # in short-term
    warm: int = 0  # not in short-term, but in mid- or long-term working memory
    misses: int = 0  # archived, or no longer live
    page_ins: int = 0
    evictions: int = 0

    @property
    def accesses(self) -> int:
        return self.hits + self.warm + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.accesses if self.accesses else 0.0


@dataclass
class Maintenance:
    """What one `maintain()` did."""

    compacted: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)
    consolidated: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.compacted or self.promoted or self.evicted or self.consolidated)


SegmentSummarizer = Callable[[str, list[Memory]], Any]


class TieredMemory:
    def __init__(
        self,
        store: MemoryStore,
        capacity: int = 128,
        eviction: EvictionPolicy | None = None,
        promotion: PromotionPolicy | None = None,
        compactor: Compactor | None = None,
        long_capacity: int | None = None,
        long_eviction: EvictionPolicy | None = None,
        mid_capacity: int | None = None,
        segment: Callable[[Memory], str] = by_trace,
        heat_threshold: float | None = None,
        heat_decay: float = 0.99,
        segment_summarizer: SegmentSummarizer | None = None,
        partition: Callable[[Memory], str] = by_scope,
        access: AccessLog | None = None,
        auto: bool = True,
        page_in: bool = True,
        agent: str = SYSTEM,
    ) -> None:
        self.store = store
        self.capacity = capacity
        self.eviction = eviction or LRU()
        self.promotion = promotion or Never()
        self.compactor = compactor
        self.long_capacity = long_capacity
        self.long_eviction = long_eviction or self.eviction
        self.mid_capacity = mid_capacity
        self.segment = segment
        self.heat_threshold = heat_threshold
        self.heat_decay = heat_decay
        self.segment_summarizer = segment_summarizer
        self.partition = partition
        self.access = access or AccessLog()
        self.auto = auto
        self.page_in = page_in
        self.agent = agent
        self.stats = CacheStats()
        self._scheduled = False
        known = set(self.access.uses)
        self.access.touch((m for m in store.state.memories if m not in known), write=True)
        store.subscribe(self._observe)

    # Use -----------------------------------------------------------------

    def touch(self, memory_ids: Iterable[str]) -> list[str]:
        """Count a use of each memory: a cache access. Returns the ids paged back into short-term memory."""
        uses = list(memory_ids)
        memory_ids = list(dict.fromkeys(uses))
        state = self.store.state
        for memory_id in uses:
            tier = state.tier.get(memory_id)
            if tier == SHORT and state.is_live(memory_id):
                self.stats.hits += 1
            elif tier in (MID, LONG) and state.is_live(memory_id):
                self.stats.warm += 1
            else:
                self.stats.misses += 1
        before = {m: self.candidate(m) for m in memory_ids if m in state.memories}
        self.access.touch(uses)
        _hook(self.eviction, "on_use", list(before.values()))
        if not self.page_in:
            return []
        faults = [m for m in memory_ids if state.tier.get(m) in (ARCHIVE, MID) and state.is_live(m)]
        if not faults:
            return []
        _hook(self.eviction, "on_page_in", [before[m] for m in faults])
        moved = self.store.move(faults, SHORT, PAGED_IN, self.agent)
        self.stats.page_ins += len(moved)
        return moved

    def candidate(self, memory_id: str) -> Candidate:
        state = self.store.state
        use = self.access.get(memory_id) or Use(0, 0, 0)
        return Candidate(
            state.memories[memory_id], state.tier[memory_id], state.status[memory_id], use.first, use.last, use.count
        )

    def candidates(self, tier: str = SHORT) -> list[Candidate]:
        return [self.candidate(m) for m, t in self.store.state.tier.items() if t == tier]

    # Reads ---------------------------------------------------------------

    def working(self, scopes: Iterable[str] | None = None, kinds: Iterable[str] | None = None) -> list[Memory]:
        """Live short-term memories, in these scopes and kinds, most recently used last."""
        found = self.store.find(tiers=[SHORT], scopes=scopes)
        if kinds is not None:
            kinds = set(kinds)
            found = [m for m in found if m.kind in kinds]
        return sorted(found, key=lambda m: (self.candidate(m.id).last, self.store.state.first[m.id].lsn))

    def sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for t in self.store.state.tier.values():
            sizes[t] = sizes.get(t, 0) + 1
        return sizes

    def over_capacity(self) -> dict[str, int]:
        """Short-term partitions over capacity, and by how much."""
        counts: dict[str, int] = {}
        state = self.store.state
        for memory_id, t in state.tier.items():
            if t == SHORT:
                p = self.partition(state.memories[memory_id])
                counts[p] = counts.get(p, 0) + 1
        return {p: n - self.capacity for p, n in counts.items() if n > self.capacity}

    def segments(self) -> dict[str, list[Candidate]]:
        """Mid-term memories grouped by segment."""
        out: dict[str, list[Candidate]] = {}
        for c in self.candidates(MID):
            out.setdefault(self.segment(c.memory), []).append(c)
        return out

    def heat(self, segment: list[Candidate]) -> float:
        """A segment's heat: each member's uses, decayed by how long ago it was last used."""
        now = self.access.now
        return sum(c.count * self.heat_decay ** max(0, now - c.last) for c in segment)

    # Maintenance ---------------------------------------------------------

    def maintain(self) -> Maintenance:
        done = Maintenance()
        if self.compactor is not None:
            done.compacted = [s.id for s in self.compactor.run()]
        now = self.access.now
        short = [c for c in self.candidates(SHORT) if c.status == LIVE]
        promoted = [c.id for c in self.promotion.select(short, now)]
        done.promoted = self.store.move(promoted, LONG, "promoted", self.agent)
        destination = MID if self.mid_capacity is not None else ARCHIVE
        done.evicted = self._evict(SHORT, self.capacity, self.eviction, now, destination)
        if self.mid_capacity is not None:
            self._maintain_mid(done)
        if self.long_capacity is not None:
            done.evicted += self._evict(LONG, self.long_capacity, self.long_eviction, now, ARCHIVE)
        return done

    def _evict(self, tier: str, capacity: int, policy: EvictionPolicy, now: int, destination: str) -> list[str]:
        groups: dict[str, list[Candidate]] = {}
        for c in self.candidates(tier):
            groups.setdefault(self.partition(c.memory), []).append(c)
        victims: list[Candidate] = []
        for group in groups.values():
            over = len(group) - capacity
            if over <= 0:
                continue
            gone = [c for c in group if c.status != LIVE][:over]
            live = [c for c in group if c.status == LIVE]
            gone += policy.victims(live, over - len(gone), now) if over > len(gone) else []
            victims += gone
        moved = self.store.move([c.id for c in victims], destination, f"evicted from {tier}-term memory", self.agent)
        _hook(policy, "on_evict", [c for c in victims if c.id in set(moved)])
        self.stats.evictions += len(moved)
        return moved

    def _maintain_mid(self, done: Maintenance) -> None:
        segments = self.segments()
        if self.heat_threshold is not None:
            for name, members in list(segments.items()):
                if self.heat(members) >= self.heat_threshold:
                    done.promoted += self.store.move([c.id for c in members], LONG, f"hot segment {name}", self.agent)
                    del segments[name]
        size = sum(len(m) for m in segments.values())
        for name, members in sorted(segments.items(), key=lambda kv: self.heat(kv[1])):
            if size <= self.mid_capacity:
                break
            done.consolidated += self._consolidate(name, [c.memory for c in members])
            size -= len(members)

    def _consolidate(self, name: str, members: list[Memory]) -> list[str]:
        """Summarize a cold segment into long-term memory (if a summarizer is given) and archive it."""
        live = [m for m in members if self.store.state.is_live(m.id)]
        with self.store.transaction(self.agent) as tx:
            if self.segment_summarizer is not None and live:
                body = self.segment_summarizer(name, live)
                body = {**body, "segment": name} if isinstance(body, dict) else {"content": body, "segment": name}
                tx.put(
                    SUMMARY,
                    f"segment:{name}",
                    body,
                    scope=live[0].scope,
                    derived_from=[m.id for m in live],
                    importance=max(m.importance for m in live),
                    tier=LONG,
                )
            for m in members:
                tx.move(m.id, ARCHIVE, f"consolidated segment {name}")
        return [m.id for m in members]

    def _observe(self, commit: Commit, state: State) -> None:
        self.access.touch((r.memory.id for r in commit.records if r.op == "put"), write=True)
        if self.auto and not self._scheduled and self._needs_work(commit):
            self._scheduled = True
            self.store.defer(self._run)

    def _needs_work(self, commit: Commit) -> bool:
        if commit.agent == self.agent and not any(r.reason == PAGED_IN for r in commit.records):
            return False  # our own moves and summaries, except page-ins, which may need room
        if any(r.op == "put" and r.memory.kind == "trace_end" for r in commit.records) and self.compactor:
            return True
        return bool(self.over_capacity())

    def _run(self) -> None:
        try:
            self.maintain()
        finally:
            self._scheduled = False


def _hook(policy: object, name: str, candidates: list[Candidate]) -> None:
    method = getattr(policy, name, None)
    if method is not None and candidates:
        method(candidates)
