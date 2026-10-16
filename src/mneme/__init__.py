"""Mneme: a transactional memory layer for agents.

import mneme
store = mneme.open("memory.db")                  # or .jsonl, or MemoryStore() in memory
with mneme.trace(store, "planner", "book a trip") as t:
    t.thought("dates first")
    t.action("search_flights", {"to": "LYO"})
"""

from pathlib import Path
from typing import Any

from .audit import blast_radius, diff, lineage, provenance_guard, quarantine, verify
from .blackboard import ERROR, RECEIPT, Agent, Blackboard, Step
from .cache import Cache, CacheSync, DictCache
from .compaction import SUMMARY, CompactionPolicy, Compactor, FinishedTraces, outline
from .engines import ENGINES, Engine, JsonlEngine, MemoryEngine, SqliteEngine, open_engine
from .policies import (
    FIFO,
    LFU,
    LRU,
    AccessCount,
    AllOf,
    AnyOf,
    Candidate,
    EvictionPolicy,
    ImportanceAtLeast,
    KindIn,
    Never,
    PromotionPolicy,
    Scored,
)
from .recall import Hit, LexicalIndex, Recall, VectorIndex
from .records import (
    ARCHIVE,
    LIVE,
    LONG,
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
from .tiers import AccessLog, Maintenance, TieredMemory
from .traces import Trace, trace

__version__ = "0.1.0"


def open(path: str | Path, **options: Any) -> MemoryStore:
    """A store on the engine `path`'s suffix names: .jsonl or .db. Options go to `MemoryStore`."""
    return MemoryStore(open_engine(path), **options)


__all__ = [
    "ARCHIVE",
    "ENGINES",
    "ERROR",
    "FIFO",
    "LFU",
    "LIVE",
    "LONG",
    "LRU",
    "RECEIPT",
    "RETRACTED",
    "SHARED",
    "SHORT",
    "STALE",
    "SUMMARY",
    "SYSTEM",
    "AccessCount",
    "AccessLog",
    "Agent",
    "AgentSession",
    "AllOf",
    "AnyOf",
    "Blackboard",
    "Cache",
    "CacheSync",
    "Candidate",
    "Commit",
    "CompactionPolicy",
    "Compactor",
    "DictCache",
    "Engine",
    "EvictionPolicy",
    "FinishedTraces",
    "Hit",
    "ImportanceAtLeast",
    "JsonlEngine",
    "KindIn",
    "LexicalIndex",
    "Maintenance",
    "Memory",
    "MemoryEngine",
    "MemoryStore",
    "Never",
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
    "VectorIndex",
    "blast_radius",
    "diff",
    "lineage",
    "open",
    "open_engine",
    "outline",
    "provenance_guard",
    "quarantine",
    "query_ref",
    "scope_guard",
    "trace",
    "verify",
]
