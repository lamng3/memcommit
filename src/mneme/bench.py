"""Benchmark tier policies: replay a workload of writes and uses through a real store, and count hits.

A workload is a list of events:

- `Write(key, kind, importance)`: an agent writes a memory.
- `Use(key)`: an agent needs that memory in its context.

`replay` runs the events through a `MemoryStore` with a `TieredMemory` built
from the policies under test. A use is a **hit** when the memory is in working
memory (short- or long-term) at that moment, and a **miss** when it has been
evicted to the archive and would have to be searched for. The report also
gives promotions, evictions, page-ins, and the largest working memory seen.
A missed memory is paged back in on use, as a cache would refill a line.

`synthetic` makes a workload with the shape agent runs have: tasks that write
bursts of trace steps, a few of which matter, then later tasks that reuse
earlier memories with a popularity skew (Zipf) and a pull towards the recent
and the important. It is seeded, so a benchmark is reproducible.

`from_access_log` builds the workload from what agents actually did: run them
on a store with `TieredMemory(..., access=AccessLog(record=True))`, and every
write and every use is kept in order, ready to replay under other policies.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from .policies import EvictionPolicy, PromotionPolicy
from .records import ARCHIVE
from .store import MemoryStore
from .tiers import AccessLog, TieredMemory


@dataclass(frozen=True)
class Write:
    key: str
    kind: str = "note"
    importance: float = 0.0


@dataclass(frozen=True)
class Use:
    key: str


Event = Write | Use


@dataclass
class Report:
    policy: str
    capacity: int
    uses: int
    hits: int
    promoted: int
    evicted: int
    paged_in: int
    peak_working: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.uses if self.uses else 0.0

    def to_json(self) -> dict:
        return {**asdict(self), "hit_rate": round(self.hit_rate, 4)}


def replay(
    events: Iterable[Event],
    eviction: EvictionPolicy,
    capacity: int,
    promotion: PromotionPolicy | None = None,
    name: str | None = None,
) -> Report:
    """Run `events` through a fresh store with these policies, and count hits."""
    store = MemoryStore(clock=lambda: 0.0)
    tiers = TieredMemory(store, capacity=capacity, eviction=eviction, promotion=promotion)
    ids: dict[str, str] = {}
    uses = hits = peak = 0
    for event in events:
        if isinstance(event, Write):
            ids[event.key] = store.put("agent", event.kind, event.key, importance=event.importance).id
        else:
            memory_id = ids.get(event.key)
            if memory_id is None:
                continue
            uses += 1
            if store.tier(memory_id) != ARCHIVE:
                hits += 1
            tiers.touch([memory_id])
            if promotion is not None:
                tiers.maintain()
        sizes = tiers.sizes()
        peak = max(peak, sizes.get("short", 0) + sizes.get("long", 0))
    moves = [r for r in store.history() if r.op == "move"]
    return Report(
        name or type(eviction).__name__,
        capacity,
        uses,
        hits,
        promoted=sum(r.tier == "long" for r in moves),
        evicted=sum(r.tier == ARCHIVE for r in moves),
        paged_in=sum(r.reason == "paged in on use" for r in moves),
        peak_working=peak,
    )


def synthetic(
    tasks: int = 60,
    steps: int = 6,
    uses_per_task: int = 8,
    important: float = 0.2,
    skew: float = 1.1,
    recent_bias: float = 0.5,
    seed: int = 0,
) -> list[Event]:
    """An agent-shaped workload: bursts of trace writes, then skewed reuse of what came before."""
    rng = random.Random(seed)
    events: list[Event] = []
    written: list[Write] = []
    for task in range(tasks):
        for step in range(steps):
            kind = ("thought", "action", "observation")[step % 3]
            w = Write(f"t{task}.s{step}", kind, 0.9 if rng.random() < important else 0.0)
            events.append(w)
            written.append(w)
        for _ in range(uses_per_task if task else 0):
            events.append(Use(_pick(rng, written, skew, recent_bias).key))
    return events


def _pick(rng: random.Random, written: list[Write], skew: float, recent_bias: float) -> Write:
    """Recent memories with probability `recent_bias`, else a Zipf draw that favours important ones."""
    if rng.random() < recent_bias:
        return written[-1 - min(len(written) - 1, int(rng.expovariate(0.3)))]
    weights = [(1.0 + 4.0 * w.importance) / (rank + 1) ** skew for rank, w in enumerate(written)]
    return rng.choices(written, weights=weights)[0]


def compare(
    events: list[Event],
    policies: dict[str, Callable[[], tuple[EvictionPolicy, PromotionPolicy | None]]],
    capacities: Iterable[int],
) -> list[Report]:
    """Replay `events` under every named policy pair and capacity."""
    out = []
    for capacity in capacities:
        for name, make in policies.items():
            eviction, promotion = make()
            out.append(replay(events, eviction, capacity, promotion, name=name))
    return out


def from_access_log(store: MemoryStore, access: AccessLog) -> list[Event]:
    """The writes and uses an `AccessLog(record=True)` kept, as a workload over the store's memories."""
    if not access.record:
        raise ValueError("this access log did not record events; create it with AccessLog(record=True)")
    events: list[Event] = []
    for kind, memory_id in access.events:
        memory = store.get(memory_id)
        if memory is None:
            continue
        events.append(Write(memory_id, memory.kind, memory.importance) if kind == "write" else Use(memory_id))
    return events
