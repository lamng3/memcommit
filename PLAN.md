# Mneme

*Mneme: A Transactional Memory Layer for Multi-Agent Systems.*

This is the feature list. `docs/design.md` explains why; this file says what is built and what is next.

## Runs now

- **Memories.** Immutable, content-addressed `Memory` values with kind, key, JSON body, source, scope, and provenance.
- **Log and state.** Records (`put`, `restore`, `stale`, `retract`) in LSN order; `State` is a fold over them, with a (kind, key) index and a reference index. `as_of(lsn)` rebuilds any past state.
- **Transactions.** All or nothing; a rewrite of a live memory is a no-op, and a put of a known memory is a restore.
- **Invalidation.** `invalidate(refs)` marks stale everything derived from the refs, transitively, in one transaction.
- **Rollback.** `rollback_to(lsn)` writes compensating records; history is kept.
- **Guards and observers.** Guards reject before the commit; observers react after it. `scope_guard` keeps agents to `shared` and their own scopes.
- **Sessions.** `AgentSession` reads `shared` plus the agent's scopes and stamps every write.
- **Blackboard.** Agents consume kinds; results and a receipt commit together; exactly-once across restarts; redo on invalidation; failures written as `error` memories.
- **Caches.** `CacheSync` keeps any `Cache` in step with the store; `DictCache`.
- **Engines.** `MemoryEngine`, `JsonlEngine` (fsynced, torn final line dropped), `OntoDBEngine` (one OntoDB transaction per commit, provenance queryable by SPARQL). Registry in `ENGINES`.
- **Aegis.** `OntologyVersions` turns every Aegis commit into a version memory, supersedes old versions, and invalidates on rollback; `version_guard`.
- **MOIRA.** `MoiraAnswerCache` (MOIRA's three tiers over the store) and `MoiraFrontDesk`.
- **KROMA.** `KromaMatcher` runs KROMA's online refinement on the blackboard and replays accepted matches on restart.
- **Demo.** `examples/four_systems.py`: align, answer, honest edit, attack, cached poison, detect, roll back, restore the honest memory, reject late work.

## Building

In order:

| Feature | What it adds |
|---|---|
| Reads of absence | Answers that depend on "no match for X" go stale when a match for X arrives (`docs/design.md`, open questions). |
| Memory as an attack surface | Aegis's four primitives on memories: inject, remove, rewire; measure harm through answers and recover with `rollback_to`. |
| Cache policy | MOIRA's promotion and eviction as a policy over the store, with hit rate and staleness measured per version. |
| Language-model agents | KROMA's debate as the judge and a MOIRA answerer backed by a model, in place of the demo's fixed verdicts. |
| OntoDB native reads | Read status by SPARQL instead of replaying the log, once OntoDB can restore to an LSN. |

## Later, on purpose

- Embedding indexes as caches over the store.
- Memory compaction: checkpoints of `State` so replay starts from a snapshot.
