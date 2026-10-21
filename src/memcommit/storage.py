"""The store as a database: log compaction, retention, snapshots, and seals that keep it honest.

MemCommit compacts at two levels, the way a log-structured database does:

- **Memory compaction** (`compaction.Compactor`, `tiers.TieredMemory`): what
  agents see. Finished traces fold into summaries, cold memories leave the
  short-term cache for the archive.
- **Log compaction** (`compact`, here): what the disk holds. Every memory has
  a history (put, stale, restore, move, ...), but replaying the state needs
  only three records per memory: its put, its last status change, and its last
  move. `compact` folds every record up to a horizon into those, keeping the
  original records (LSNs, transactions, agents, reasons), so the folded log
  replays to exactly the same state. Records after the horizon stay as they
  are. The engine swaps the old log for the new one atomically.

Retention rides on log compaction: a `RetentionPolicy` names memories that may
be dropped altogether when the log is folded: archived memories older than a
cutoff, say. A purged memory leaves no record, so the log stops growing with
the memories nobody will read again.

Every compaction writes a `compaction` memory (to the archive) with the
horizon, the record counts before and after, the purged ids, and the hash
chain of the old log up to the horizon. `audit.verify` uses it: seals from
before the horizon describe a log that no longer exists and are skipped, and
references to purged memories are expected, not forged. A new seal is written
over the compacted log.

What compaction gives up is history inside the horizon: `as_of(lsn)` for an
LSN before the horizon shows the folded log, not the moment it names. The
state now, and every history after the horizon, are exact.

`AutoSeal` seals the log every N commits, and `check_seal` rechecks the newest
seal (on open, say); `snapshot` support lives on `MemoryStore` itself
(`save_snapshot`, and `MemoryStore(engine, snapshot=path)`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .audit import SEAL, chain_digest, seal
from .records import ARCHIVE, LIVE, Commit, Memory, Record
from .state import State
from .store import SYSTEM, MemoryStore

COMPACTION = "compaction"


class RetentionPolicy(Protocol):
    def purge(self, memory: Memory, state: State, now: float) -> bool:
        """True if `memory` may be dropped from the log. `now` is the store clock's time."""


class KeepAll:
    def purge(self, memory: Memory, state: State, now: float) -> bool:
        return False


@dataclass(frozen=True)
class ArchivedOlderThan:
    """Purge memories that sit in the archive, and whose last record is older than `seconds`."""

    seconds: float
    dead_only: bool = False  # only memories that no longer hold

    def purge(self, memory: Memory, state: State, now: float) -> bool:
        if state.tier[memory.id] != ARCHIVE or now - state.last[memory.id].time < self.seconds:
            return False
        return not self.dead_only or state.status[memory.id] != LIVE


class KindsAfter:
    """Purge archived memories of these kinds once their last record is older than `seconds`."""

    def __init__(self, kinds: Iterable[str], seconds: float) -> None:
        self.kinds = frozenset(kinds)
        self.seconds = seconds

    def purge(self, memory: Memory, state: State, now: float) -> bool:
        return (
            memory.kind in self.kinds
            and state.tier[memory.id] == ARCHIVE
            and now - state.last[memory.id].time >= self.seconds
        )


@dataclass
class CompactionReport:
    horizon: int
    before: int
    after: int
    purged: list[str]
    manifest: Memory
    seal: Memory


def fold(records: list[Record], horizon: int) -> list[Record]:
    """The fewest original records up to `horizon` that replay to the same state: put, last status, last move."""
    put: dict[str, Record] = {}
    status: dict[str, Record] = {}
    move: dict[str, Record] = {}
    for r in records:
        if r.lsn > horizon:
            break
        memory_id = r.memory.id
        if r.op == "put" and memory_id not in put:
            put[memory_id] = r
        elif r.op == "move":
            move[memory_id] = r
        else:
            status[memory_id] = r
    kept = list(put.values())
    kept += [r for r in status.values() if r.status != LIVE]  # a last restore leaves it live, as the put does
    kept += [r for memory_id, r in move.items() if r.tier != (put[memory_id].tier or "short")]
    return sorted(kept, key=lambda r: r.lsn)


def compact(
    store: MemoryStore,
    horizon: int | None = None,
    retention: RetentionPolicy | None = None,
    agent: str = SYSTEM,
) -> CompactionReport:
    """Fold the log up to `horizon` (default: now), drop what `retention` purges, and seal the result."""
    records = store.engine.records()
    horizon = store.lsn if horizon is None else min(horizon, store.lsn)
    retention = retention or KeepAll()
    later = [r for r in records if r.lsn > horizon]
    touched_later = {r.memory.id for r in later}
    at_horizon = store.as_of(horizon)
    now = store.clock()
    purged = [
        m
        for m, memory in at_horizon.memories.items()
        if m not in touched_later and memory.kind not in (COMPACTION, SEAL) and retention.purge(memory, at_horizon, now)
    ]
    gone = set(purged)
    folded = [r for r in fold(records, horizon) if r.memory.id not in gone]
    old_digest = chain_digest(records, horizon)
    store.engine.rewrite(folded + later)
    store.state = State.replay(store.engine.records())
    body = {
        "horizon": horizon,
        "before": len(records),
        "after": len(folded) + len(later),
        "purged": purged,
        "digest": old_digest,
    }
    manifest = store.put(agent, COMPACTION, f"lsn:{horizon}", body, tier=ARCHIVE)
    return CompactionReport(horizon, len(records), body["after"], purged, manifest, seal(store, agent))


def compactions(store: MemoryStore) -> list[Memory]:
    """Compaction manifests, oldest first."""
    return sorted(store.find(kind=COMPACTION, status=None), key=lambda m: m.body["horizon"])


class AutoSeal:
    """Seal the log after every `every` records. An observer; the seal is written once the commit is done."""

    def __init__(self, store: MemoryStore, every: int = 1000, agent: str = SYSTEM) -> None:
        self.store = store
        self.every = every
        self.agent = agent
        seals = store.find(kind=SEAL, status=None)
        self.last = max((m.body["lsn"] for m in seals), default=0)
        store.subscribe(self._observe)

    def _observe(self, commit: Commit, state: State) -> None:
        if commit.agent == self.agent and any(r.memory.kind == SEAL for r in commit.records):
            return
        if commit.lsn - self.last >= self.every:
            self.last = commit.lsn
            self.store.defer(lambda: seal(self.store, self.agent))


def check_seal(store: MemoryStore) -> bool:
    """True if the newest seal still matches the log (or there is none)."""
    seals = [m for m in store.find(kind=SEAL, status=None) if m.source == SYSTEM]
    if not seals:
        return True
    newest = max(seals, key=lambda m: m.body["lsn"])
    return chain_digest(store.engine.records(), newest.body["lsn"]) == newest.body["digest"]
