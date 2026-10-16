"""Tiered memory: bounded short-term memory, long-term memory, and an archive, run by policies.

    tiers = TieredMemory(store, capacity=64, eviction=LRU(), promotion=AccessCount(3),
                         compactor=Compactor(store))
    tiers.touch([m.id for m in hits])      # an agent used these
    tiers.working(scopes=["shared", "planner"])   # what is in its working memory now

The design follows the memory hierarchies agent systems have converged on:
MemGPT's main context, recall, and archival storage (Packer et al., 2023),
MemoryOS's short-, mid-, and long-term stores with heat-based promotion
(Kang et al., 2025), and Generative Agents' recency, importance, and relevance
(Park et al., 2023). Here each move between tiers is a record in the log, so
"why did the agent forget that" has an answer, and rollback undoes a policy's
decisions with everything else.

`maintain()` does, in order:

1. **Compact** finished traces into long-term summaries (when a `Compactor` is given).
2. **Promote** short-term memories the promotion policy selects to long-term.
3. **Evict** from each short-term partition over capacity: memories that no
   longer hold go first, then the eviction policy's victims, to the archive.
4. **Trim** long-term memory to `long_capacity`, if one is set, the same way.

Capacity is per partition, and a partition is the memory's scope by default,
so each agent's working memory is bounded on its own and shared memory has a
bound of its own. With `auto=True` the tiers run `maintain()` after any
commit that leaves a partition over capacity, through `store.defer`.

A used memory that was archived is paged back into short-term memory
(`page_in=True`, the default), the way MemGPT pages archived context back in.

Use is counted by an `AccessLog`: a logical clock that ticks on every write
and every `touch`. It is not in the log, because reads are many and cheap;
`save` and `load` keep it across restarts, and without it a restarted store
counts each memory as used once, when it was written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .compaction import Compactor
from .policies import LRU, Candidate, EvictionPolicy, Never, PromotionPolicy
from .records import ARCHIVE, LIVE, LONG, SHORT, Commit, Memory
from .state import State
from .store import SYSTEM, MemoryStore


def by_scope(memory: Memory) -> str:
    return memory.scope


@dataclass
class Use:
    first: int
    last: int
    count: int = 1


@dataclass
class AccessLog:
    """How often and how lately each memory was used, on a logical clock."""

    now: int = 0
    uses: dict[str, Use] = field(default_factory=dict)

    def touch(self, memory_ids: Iterable[str]) -> None:
        for memory_id in memory_ids:
            self.now += 1
            use = self.uses.get(memory_id)
            if use is None:
                self.uses[memory_id] = Use(self.now, self.now)
            else:
                use.last = self.now
                use.count += 1

    def get(self, memory_id: str) -> Use | None:
        return self.uses.get(memory_id)

    def save(self, path: str | Path) -> None:
        data = {"now": self.now, "uses": {k: [u.first, u.last, u.count] for k, u in self.uses.items()}}
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> AccessLog:
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["now"], {k: Use(*v) for k, v in data["uses"].items()})


@dataclass
class Maintenance:
    """What one `maintain()` did."""

    compacted: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.compacted or self.promoted or self.evicted)


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
        self.partition = partition
        self.access = access or AccessLog()
        self.auto = auto
        self.page_in = page_in
        self.agent = agent
        self._scheduled = False
        known = set(self.access.uses)
        self.access.touch(m for m in store.state.memories if m not in known)
        store.subscribe(self._observe)

    # Use -----------------------------------------------------------------

    def touch(self, memory_ids: Iterable[str]) -> list[str]:
        """Count a use of each memory: an agent read it into its context.

        With `page_in`, a live archived memory that is used comes back to short-term memory, as a
        page fault brings a page back; eviction then makes room. Returns the ids paged in.
        """
        memory_ids = list(memory_ids)
        self.access.touch(memory_ids)
        if not self.page_in:
            return []
        state = self.store.state
        faults = [m for m in memory_ids if state.tier.get(m) == ARCHIVE and state.is_live(m)]
        return self.store.move(dict.fromkeys(faults), SHORT, "paged in on use", self.agent) if faults else []

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

    # Maintenance ---------------------------------------------------------

    def maintain(self) -> Maintenance:
        done = Maintenance()
        if self.compactor is not None:
            done.compacted = [s.id for s in self.compactor.run()]
        now = self.access.now
        short = [c for c in self.candidates(SHORT) if c.status == LIVE]
        promoted = [c.id for c in self.promotion.select(short, now)]
        done.promoted = self.store.move(promoted, LONG, "promoted", self.agent)
        done.evicted = self._evict(SHORT, self.capacity, self.eviction, now)
        if self.long_capacity is not None:
            done.evicted += self._evict(LONG, self.long_capacity, self.long_eviction, now)
        return done

    def _evict(self, tier: str, capacity: int, policy: EvictionPolicy, now: int) -> list[str]:
        groups: dict[str, list[Candidate]] = {}
        for c in self.candidates(tier):
            groups.setdefault(self.partition(c.memory), []).append(c)
        victims: list[str] = []
        for group in groups.values():
            over = len(group) - capacity
            if over <= 0:
                continue
            gone = [c for c in group if c.status != LIVE][:over]
            live = [c for c in group if c.status == LIVE]
            gone += policy.victims(live, over - len(gone), now) if over > len(gone) else []
            victims += [c.id for c in gone]
        return self.store.move(victims, ARCHIVE, f"evicted from {tier}-term memory", self.agent)

    def _observe(self, commit: Commit, state: State) -> None:
        self.access.touch(r.memory.id for r in commit.records if r.op == "put")
        if self.auto and not self._scheduled and self._needs_work(commit):
            self._scheduled = True
            self.store.defer(self._run)

    def _needs_work(self, commit: Commit) -> bool:
        if commit.agent == self.agent and not any(r.reason == "paged in on use" for r in commit.records):
            return False  # our own moves and summaries, except page-ins, which may need room
        if any(r.op == "put" and r.memory.kind == "trace_end" for r in commit.records) and self.compactor:
            return True
        return bool(self.over_capacity())

    def _run(self) -> None:
        try:
            self.maintain()
        finally:
            self._scheduled = False
