# Mneme

*Mneme: A Transactional Memory Layer for Agents.*

This is the feature list. `docs/design.md` explains why; this file says what is built and what is next. Mneme is a standalone library: it names no agent system, model, or domain, and every integration lives in the project that uses it.

## Runs now

- **Memories.** Immutable, content-addressed JSON values with kind, key, body, source, scope, importance, and provenance.
- **Log and state.** Records (`put`, `restore`, `stale`, `retract`, `move`) in LSN order; `State` is a fold over them, with indexes by (kind, key), by reference, and by read. `as_of(lsn)` rebuilds any past state.
- **Transactions.** All or nothing; a rewrite of a live memory is a no-op, and a put of a known memory is a restore.
- **Invalidation.** `invalidate(refs)` marks stale everything derived from the refs, transitively, in one transaction.
- **Reads as provenance.** `query_ref(kind, key, **where)` names a read; a commit that brings a matching memory live marks what was built on the read stale, in the same transaction.
- **Rollback.** `rollback_to(lsn)` writes compensating records for status and tier; history is kept.
- **Guards, observers, deferred work.** Guards reject before the commit; observers react after it; `defer` runs work that writes once the commit is done.
- **Traces.** `trace(...)` / `Trace`: a root and a tree of typed steps, each committed on its own; handoffs start sub-traces; `Trace.open(...).to_json()`.
- **Tiers.** `short`, `long`, `archive`. `TieredMemory` bounds short-term memory per partition and runs maintenance after commits.
- **Policies.** Eviction: `FIFO`, `LRU`, `LFU`, `Scored`. Promotion: `AccessCount`, `ImportanceAtLeast`, `KindIn`, `AnyOf`, `AllOf`, `Never`. `AccessLog` on a logical clock, saved and loaded.
- **Compaction.** `Compactor` turns finished traces into long-term summaries and archives their steps; `outline` summarizer; `FinishedTraces` policy.
- **Recall.** `Recall` scores relevance, recency, and importance; `LexicalIndex` (BM25) and `VectorIndex` (any embedder), kept in step by `CacheSync`.
- **Sessions and scopes.** `AgentSession` reads `shared` plus the agent's scopes; `scope_guard`.
- **Blackboard.** Agents consume kinds; results and a receipt commit together; exactly once across restarts; redo on invalidation; failures written as `error` memories.
- **Audit.** `verify`, `lineage`, `blast_radius`, `quarantine`, `diff`, `provenance_guard`.
- **Engines.** `MemoryEngine`, `JsonlEngine`, `SqliteEngine`; `mneme.open(path)`.
- **Example.** `examples/shared_memory.py`: two agents, traces, tiers, compaction, recall, a late read, a poisoned tool, quarantine, and recovery.

## Building

In order:

| Feature | What it adds |
|---|---|
| Policy benchmark | Replay recorded agent traces against each eviction and promotion policy; report hit rate of recall against what agents actually used, and working-memory size. |
| More eviction policies | 2Q and ARC (ghost lists), TTL by kind, and a forgetting curve (Ebbinghaus decay, reset on use). |
| More compaction policies | Compact by age or by working-memory pressure; merge summaries of similar tasks; keep failed traces whole longer. |
| Mid-term tier | Optional tier between short and long (MemoryOS's segments): traces grouped by topic before they are summarized. |
| Learned policies | A model that scores candidates for eviction and promotion, trained on which memories agents later recalled. |
| Tamper-evident log | A hash chain over transactions, so a deleted or reordered line is caught by `verify`. |
| Snapshots | Checkpoints of `State`, so replay starts from a snapshot instead of LSN 0. |
| Retention | Purge archived memories past a retention period, keeping the log's hash chain intact with tombstones. |
| Async API | `async` versions of commit and recall for agent frameworks that run in an event loop. |

## Later, on purpose

- A server, so agents in several processes share one store.
- An approximate nearest-neighbour index for large vector recall.
