"""Mneme: a transactional memory layer for agents.

import mneme
store = mneme.open("memory.db")                  # or .jsonl, or MemoryStore() in memory
with mneme.trace(store, "planner", "book a trip") as t:
    t.thought("dates first")
    t.action("search_flights", {"to": "LYO"})
"""

from pathlib import Path
from typing import Any

from .audit import SEAL, blast_radius, chain_digest, diff, lineage, provenance_guard, quarantine, seal, verify
from .blackboard import ERROR, RECEIPT, Agent, Blackboard, Step
from .cache import Cache, CacheSync, DictCache
from .compaction import (
    SUMMARY,
    CompactionPolicy,
    Compactor,
    FinishedTraces,
    KeepFailed,
    OlderThan,
    UnderPressure,
    outline,
)
from .engines import ENGINES, Engine, JsonlEngine, MemoryEngine, SqliteEngine, open_engine
from .policies import (
    ARC,
    FIFO,
    LFU,
    LRU,
    TTL,
    AccessCount,
    AllOf,
    AnyOf,
    Candidate,
    EvictionPolicy,
    Forgetting,
    ImportanceAtLeast,
    KindIn,
    Learned,
    Never,
    PromotionPolicy,
    Scored,
    TwoQueue,
)
from .recall import Hit, LexicalIndex, Recall, VectorIndex
from .records import (
    ARCHIVE,
    LIVE,
    LONG,
    MID,
    RETRACTED,
    SHARED,
    SHORT,
    STALE,
    Commit,
    Memory,
    Record,
    query_ref,
)
from .session import AgentSession, scope_guard
from .state import State
from .store import SYSTEM, MemoryStore, Rejected, Transaction
from .tiers import AccessLog, CacheStats, Maintenance, TieredMemory, by_scope, by_trace
from .traces import Trace, trace

__version__ = "0.1.0"


def open(path: str | Path, **options: Any) -> MemoryStore:
    """A store on the engine `path`'s suffix names: .jsonl or .db. Options go to `MemoryStore`."""
    return MemoryStore(open_engine(path), **options)


__all__ = [
    "ARC",
    "ARCHIVE",
    "ENGINES",
    "ERROR",
    "FIFO",
    "LFU",
    "LIVE",
    "LONG",
    "LRU",
    "MID",
    "RECEIPT",
    "RETRACTED",
    "SEAL",
    "SHARED",
    "SHORT",
    "STALE",
    "SUMMARY",
    "SYSTEM",
    "TTL",
    "AccessCount",
    "AccessLog",
    "Agent",
    "AgentSession",
    "AllOf",
    "AnyOf",
    "Blackboard",
    "Cache",
    "CacheStats",
    "CacheSync",
    "Candidate",
    "Commit",
    "CompactionPolicy",
    "Compactor",
    "DictCache",
    "Engine",
    "EvictionPolicy",
    "FinishedTraces",
    "Forgetting",
    "Hit",
    "ImportanceAtLeast",
    "JsonlEngine",
    "KeepFailed",
    "KindIn",
    "Learned",
    "LexicalIndex",
    "Maintenance",
    "Memory",
    "MemoryEngine",
    "MemoryStore",
    "Never",
    "OlderThan",
    "PromotionPolicy",
    "Recall",
    "Record",
    "Rejected",
    "Scored",
    "SqliteEngine",
    "State",
    "Step",
    "TieredMemory",
    "Trace",
    "Transaction",
    "TwoQueue",
    "UnderPressure",
    "VectorIndex",
    "blast_radius",
    "by_scope",
    "by_trace",
    "chain_digest",
    "diff",
    "lineage",
    "open",
    "open_engine",
    "outline",
    "provenance_guard",
    "quarantine",
    "query_ref",
    "scope_guard",
    "seal",
    "trace",
    "verify",
]
