"""MemCommit: a transactional memory layer for agents.

import memcommit
store = memcommit.open("memory.db")                  # or .jsonl, or MemoryStore() in memory
with memcommit.trace(store, "planner", "book a trip") as t:
    t.thought("dates first")
    t.action("search_flights", {"to": "LYO"})
"""

from pathlib import Path
from typing import Any

from .aio import AsyncMemoryStore
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
from .dream import DREAM, Change, Dreamer, DreamReport, Learning, heuristic_extractor
from .engines import ENGINES, Engine, JsonlEngine, MemoryEngine, SqliteEngine, open_engine
from .episodes import Episodes, Session
from .evaluation import Result, Workload, evaluate, make_workload
from .knowledge import KNOWLEDGE, Knowledge
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
from .recall import AnnIndex, Hit, LexicalIndex, Recall, VectorIndex
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
from .schema import AGENT, BUILTIN, SCHEMA, Kind, Schema, check_body
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
    "AGENT",
    "ARC",
    "ARCHIVE",
    "BUILTIN",
    "DREAM",
    "ENGINES",
    "ERROR",
    "FIFO",
    "KNOWLEDGE",
    "LFU",
    "LIVE",
    "LONG",
    "LRU",
    "MID",
    "RECEIPT",
    "RETRACTED",
    "SCHEMA",
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
    "AnnIndex",
    "AnyOf",
    "AsyncMemoryStore",
    "Blackboard",
    "Cache",
    "CacheStats",
    "CacheSync",
    "Candidate",
    "Change",
    "Commit",
    "CompactionPolicy",
    "Compactor",
    "DictCache",
    "DreamReport",
    "Dreamer",
    "Engine",
    "Episodes",
    "EvictionPolicy",
    "FinishedTraces",
    "Forgetting",
    "Hit",
    "ImportanceAtLeast",
    "JsonlEngine",
    "KeepFailed",
    "Kind",
    "KindIn",
    "Knowledge",
    "Learned",
    "Learning",
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
    "Result",
    "Schema",
    "Scored",
    "Session",
    "SqliteEngine",
    "State",
    "Step",
    "TieredMemory",
    "Trace",
    "Transaction",
    "TwoQueue",
    "UnderPressure",
    "VectorIndex",
    "Workload",
    "blast_radius",
    "by_scope",
    "by_trace",
    "chain_digest",
    "check_body",
    "diff",
    "evaluate",
    "heuristic_extractor",
    "lineage",
    "make_workload",
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
