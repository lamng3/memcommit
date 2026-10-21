# MemCommit

*MemCommit: A Transactional Memory Layer for Agents.*

This is the feature list. `docs/design.md` explains why; this file says what is built. MemCommit is a standalone library: it names no agent system, model, or domain, and every integration lives in the project that uses it.

## Runs now

### The log-structured store

- **Memories.** Immutable, content-addressed JSON values with kind, key, body, source, scope, importance, and provenance.
- **Log and state.** Records (`put`, `restore`, `stale`, `retract`, `move`) in LSN order; `State` is a fold over them, with indexes by (kind, key), by reference, and by read. `as_of(lsn)` rebuilds any past state.
- **Transactions.** All or nothing; a rewrite of a live memory is a no-op, and a put of a known memory is a restore.
- **Invalidation.** `invalidate(refs)` marks stale everything derived from the refs, transitively, in one transaction.
- **Reads as provenance.** `query_ref(kind, key, scopes, **where)` names a read; a commit that brings a matching memory, visible to the reader, live marks what was built on the read stale, in the same transaction.
- **Rollback.** `rollback_to(lsn)` writes compensating records for status and tier; history is kept.
- **Guards, observers, deferred work.** Guards reject before the commit; observers react after it; `defer` runs work that writes once the commit is done.
- **Engines.** `MemoryEngine`, `JsonlEngine`, `SqliteEngine`; each appends a transaction atomically, reads the tail after an LSN, and rewrites its log atomically. `memcommit.open(path)`.

### The database underneath

- **Log compaction.** `storage.compact` folds every record up to a horizon into each memory's put, last status change, and last move, keeping the original records; the folded log replays to the same state. A `compaction` manifest records the horizon, the counts, the purged ids, and the old log's hash chain.
- **Retention.** `ArchivedOlderThan`, `KindsAfter`, or any `RetentionPolicy` names memories to purge during compaction.
- **Snapshots.** `save_snapshot` writes the state; `MemoryStore(engine, snapshot=path)` replays only the records after it.
- **Seals.** `seal` writes a hash chain over the log; `AutoSeal` seals every N records; `check_seal` rechecks the newest; `verify` rechecks all of them after the last compaction.
- **Many readers.** `refresh` applies records another writer appended.
- **Server.** `MemoryServer` owns the log and runs every commit, so LSNs and guards hold across processes; `RemoteStore` is a `MemoryStore` on it. `python -m memcommit serve | verify | compact | stats`.
- **Async.** `AsyncMemoryStore`: commits in a worker thread, one at a time.

### The cache and the tiers

- **Tiers.** `short` (the cache), `mid` (optional segments), `long`, `archive`.
- **Short-term as a cache.** `TieredMemory` bounds short-term memory per partition; `touch` counts a hit, a warm hit (mid or long), or a miss in `stats`, and pages a live archived memory back in.
- **Eviction.** `FIFO`, `LRU`, `LFU`, `Scored`, `Forgetting` (Ebbinghaus), `TTL` by kind, `TwoQueue` (2Q), `ARC` (ghost lists, adaptive target), `Learned` (online logistic model). Stateful policies get `on_use`, `on_evict`, and `on_page_in`.
- **Promotion.** `AccessCount`, `ImportanceAtLeast`, `KindIn`, `AnyOf`, `AllOf`, `Never`.
- **Mid-term.** Evicted memories wait in segments (`by_trace`); a segment past `heat_threshold` is promoted whole; while mid-term is over capacity, the coldest segment is consolidated by `segment_summarizer` and archived.
- **Access log.** A logical clock of uses, saved and loaded; with `record=True`, every write and use in order.

### Memory compaction

- **Traces.** `trace(...)` / `Trace`: a root and a tree of typed steps, each committed on its own; handoffs start sub-traces; `Trace.open(...).to_json()`.
- **Compactor.** Finished traces become long-term summaries, steps go to the archive; `outline` or any summarizer.
- **Policies.** `FinishedTraces`, `OlderThan`, `KeepFailed`, `UnderPressure`.
- **Merging.** `Compactor.merge` folds summaries of the same task into one.

### A declared structure

- **Schema.** `Schema` and `Kind`: a JSON-schema subset for each kind's body, allowed scopes (`shared`, `agent`, or a name), `requires` (kinds a memory must derive from), and default tier and importance. `strict` rejects undeclared kinds; MemCommit's own kinds are always allowed.
- **Enforced and recorded.** `Schema.attach(store)` adds a commit guard and writes the schema to the log; a new version supersedes the old. `Schema.load`, `audit`, and `describe` (Markdown for agents).

### Two tiers of memory

- **Episodic.** `Session` (a trace with user and agent turns, recorded tool calls with `wrap`/`call`, replies derived from what they read) and `Episodes` (`sessions`, `new_since(lsn)`).
- **Derived.** `Knowledge`: notes at paths with title, tags, and importance; `add`, `update` (old version stale, dependents too), `delete` (retract); `ls`, `read`, `index(max_chars)`, `search(where=derived|episodic|all)`, `fetch` (a session id returns its transcript); `tools()` and `call` for five model-callable tools.
- **Dreaming.** `Dreamer`: sessions that ended since the last `dream` record, an `Extractor` (`heuristic_extractor`, or any function), conflicts by supersession, duplicates archived, oversized notes split; `dry_run`, `undo`, `due`, `attach`.

### Agents, recall, and defense

- **Sessions and scopes.** `AgentSession` reads `shared` plus the agent's scopes; `scope_guard`.
- **Blackboard.** Agents consume kinds (and may name what they produce, which orders them upstream first); results and a receipt commit together; exactly once across restarts; redo on invalidation; failures written as `error` memories.
- **Recall.** Relevance, recency, and importance; `LexicalIndex` (BM25), `VectorIndex` (any embedder), `AnnIndex` (random-hyperplane LSH, multi-probe), all kept in step by `CacheSync`.
- **Audit.** `verify`, `lineage`, `blast_radius`, `quarantine`, `diff`, `provenance_guard`, `seal`.

### Measurement and examples

- **Memory benchmark.** `memcommit.evaluation`: a synthetic multi-session workload and a deterministic reader score seven configurations (none, full context, episodic, derived by tools, by dreaming, by both, both tiers). `examples/memory_benchmark.py` writes `results/memory/`.
- **Benchmark.** `memcommit.bench`: replay writes and uses through the real store under any policies; `synthetic` workloads; `from_access_log` replays what agents did. `examples/policy_benchmark.py` writes `results/policies/`.
- **Example.** `examples/shared_memory.py`: two agents, traces, tiers, compaction, recall, a late read, a poisoned tool, quarantine, and recovery.

## Building

Nothing planned is unbuilt. New work starts as an experiment: a recorded workload to benchmark, or a policy to compare, measured with `memcommit.bench` and written to `results/`.
