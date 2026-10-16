# Design

Why Mneme is built the way it is. `PLAN.md` says what is built and what is next.

## What it is for

Agents write a lot down: thoughts, tool calls, observations, messages,
results, plans, decisions. In a multi-agent workflow one agent's output is
another's premise. Mneme is the place all of that lives: a store of JSON
memories with a log, provenance, tiers, and the policies that keep working
memory small. It is a library with no dependencies, meant to be plugged into
any agent system; it knows nothing about any particular agent, model, or
domain.

## How agent systems store memory, and what we took

| System | How it stores memory | What Mneme takes |
|:--|:--|:--|
| MemGPT / Letta (Packer et al., 2023) | Main context, recall storage, archival storage; the agent pages between them | Tiers: `short` is the context, `long` and `archive` the stores behind it |
| Generative Agents (Park et al., 2023) | A memory stream of observations; retrieval by recency, importance, and relevance; reflections summarize | The recall score; `Scored` eviction; summaries as reflections |
| Reflexion (Shinn et al., 2023) | Episodic self-reflections after each trial | Compaction: a finished trace becomes one long-term memory |
| MemoryOS (Kang et al., 2025) | Short-, mid-, and long-term stores; heat-based promotion | Promotion policies on use counts and importance |
| A-MEM (Xu et al., 2025) | Notes linked to each other, links updated as notes arrive | Provenance links, and invalidation along them |
| Mem0 (Chhikara et al., 2025) | Extracted facts with add, update, delete, no-op | Status ops on an append-only log instead of updates in place |
| Zep (Rasmussen et al., 2025) | A temporal knowledge graph; facts have validity intervals | `as_of(lsn)`: any past state, and why a fact stopped holding |

What none of them give together, and Mneme does: transactions, an
append-only log with compensation, provenance that invalidates downstream
work, and every tier decision recorded. Those are what make memory shared by
several agents safe to rely on and possible to defend.

## Memories are values

A `Memory` is immutable, and its id is a hash of its kind, key, body, source,
scope, importance, and `derived_from`. Two consequences:

- Writing a live memory again writes nothing, and redoing work that comes out identical restores the old memory instead of creating a new one.
- A body cannot be changed without its id changing, so tampering shows up on load and in `verify`.

## Status and tier are a fold over the log

Each `Record` puts, restores, retracts, or marks stale one memory, or moves it
to another tier. `State` is the result of applying records in LSN order, with
three indexes: by (kind, key), from each reference to the memories derived
from it, and from each kind to the reads (`find:` refs) over it. Replay
rebuilds exactly the state that wrote the log, and `as_of(lsn)` is a replay
that stops early.

Status (`live`, `stale`, `retracted`) says whether a memory holds. Tier
(`short`, `long`, `archive`) says where it sits. They are independent: a
stale long-term memory is still long-term, and a redo that restores it puts it
back where it was. Tiers decide what is in working memory and what recall
searches; they never hide a memory from `find`.

Rollback is compensation, as in ARIES: the store computes the status and tier
of every memory as of the target LSN and writes the records that move each
one back. Nothing is erased.

## Provenance drives invalidation

`derived_from` holds memory ids and outside references (`doc:handbook@3`,
`file:sha256...`). `invalidate(refs)` marks stale every live memory reachable
through the reference index, in one transaction. A client that versions
something outside the store (a document, a knowledge base, a model) records
the version as a reference, and invalidating the version invalidates what was
built on it.

## Reads are provenance too

An answer derives from the memories it read, but it also depends on the ones
it looked for and did not find. "No notes about this topic" is built on an
empty read, and the first note must undo it.

So a reference can name a read: `query_ref("note", topic="light")`, a `find:`
prefix and the query (kind, optional key, body fields). An agent derives from
it next to the memories the read returned. When a commit brings a memory live,
the store finds the reads that memory now belongs to and marks everything
built on them stale, in the same transaction. Memories written by that commit
are left alone.

Only additions need this. A memory that drops out of a read was returned by
it, so whatever used it already derives from it directly. Over-invalidation is
cheap: work redone with the same result restores the same memory id.

## Traces

A run is a root memory (`trace`: agent, task, run id, inputs) and one memory
per step. A step is derived from the root and its parent step, which is the
previous step unless one is given, so a trace is a tree and the tree is
provenance: invalidating a trace's inputs, or one observation, makes every
later step stale. Steps commit one at a time, because a trace is a record of
what happened, and a crash should keep what happened before it. A handoff
writes a `message` step and starts the other agent's trace derived from it.

## Tiers and policies

(The cache view of the same machinery is below, under "Short-term memory is a cache".)

Short-term memory is bounded per partition (a scope, by default), so each
agent's working memory and shared memory have their own room. When a commit
leaves a partition over capacity, `TieredMemory` runs `maintain()` through
`store.defer`, after the commit is done:

1. Compact finished traces into long-term summaries.
2. Promote what the promotion policy selects to long-term.
3. Evict from each short-term partition over capacity, memories that no longer hold first, then the eviction policy's victims, to the archive.
4. Trim long-term to its capacity, if it has one.

Policies see `Candidate`s, not the store: the memory, tier, status, and use
counts on a logical clock. That keeps them small and swappable, and it is the
seam for the policies we have not written yet (see `PLAN.md`).

Use is counted by an `AccessLog`, outside the log, because reads are many
and cheap. It can be saved and loaded; without it a restarted store counts
each memory as used once, at its write.

## Compaction

A finished trace has many steps an agent rarely needs again. `Compactor`
writes a `summary` in the long-term tier, derived from the root and every
step, and moves the steps to the archive, in one transaction. Because the
summary is derived from the steps, a step later found wrong takes the summary
with it. What a summary says is the summarizer's choice; `outline` needs no
model, and a language-model reflection has the same signature.

## Recall

Relevance comes from an index kept in step with the store by `CacheSync`, so
a stale memory is never recalled: `LexicalIndex` (BM25) needs nothing,
`VectorIndex` takes any embedding function. The score adds recency (decay per
log record since the memory last changed) and importance. Hits are touched in
the tiers, which is how recall feeds the policies.

## Guards before, observers after

A commit numbers its records, asks every guard, appends to the engine in one
call, applies to the state, then tells every observer. Neither may commit
during a commit; an observer calls `store.defer(work)` instead, and the store
runs the work once the commit is done.

## Built to be defended

Shared memory is an attack surface: inject a false observation, retract a
true one, rewire what a memory was derived from, poison a summary. Mneme does
not decide what is malicious, but it makes a defender's work exact:
content addressing catches altered bodies, `verify` replays the log for what a
well-behaved store could not have written, `blast_radius` finds everything a
suspect reached, `quarantine` retracts and invalidates in one transaction,
and `provenance_guard` rejects forged or stale premises at write time. A
defender that also detects (re-checking answers, watching scores) plugs in as
an observer or a guard.

## The database underneath

Mneme is a log-structured store, and it borrows a database's answers to a
database's problems.

**Two levels of compaction.** Memory compaction decides what agents see:
finished traces fold into summaries, cold mid-term segments consolidate,
summaries of the same task merge. It never deletes; it moves memories to the
archive. Log compaction decides what the disk holds. A memory's history can be
long (put, stale, restore, stale, move, ...), but replaying its state needs
three records: the put, the last status change, and the last move.
`storage.compact` keeps exactly those, as the original records, up to a
horizon, and keeps every record after the horizon verbatim, so the folded log
replays to the same state. That is the LSM move: rewrite the sorted history
into something smaller that answers the same reads. The price is history
inside the horizon, which `as_of` can then only show folded.

**Retention.** Purging is the one place a memory leaves the log, and it
happens only during log compaction, only for memories a `RetentionPolicy`
names (archived and old, by default), and only for memories with no record
after the horizon. The compaction manifest lists them, so a later reference
to a purged memory is known to be deliberate, not forged.

**Integrity across rewrites.** A seal hashes the log up to an LSN. Compaction
invalidates every earlier seal by design, so the manifest stores the old
log's chain and a new seal covers the new log; `verify` skips seals older than
the last horizon.

**Checkpoints.** A snapshot is the state as JSON: each memory's first and last
record, its status and tier. Opening from a snapshot reads only the records
after it, through the engine's `records_after`.

**One writer, many readers.** LSNs must come from one place. The server owns
the log and runs every commit, so guards hold for every process; clients
mirror the state by pulling the log's tail. This is the primary-replica
shape, with the replicas also able to submit transactions.

## Short-term memory is a cache

The store holds every memory; short-term memory holds the few an agent works
with now. `touch` is a cache access: a hit when the memory is in short-term
memory, warm when it is in mid- or long-term, a miss when it is archived or
no longer holds. A missed live memory is paged back in, and eviction makes
room. Replacement policies are the classic ones (LRU, LFU, FIFO, 2Q, ARC) and
the agent-memory ones (a forgetting curve, a recency-importance-frequency
score, TTL by kind), plus an online learned policy. A policy that keeps state
(ARC's ghost lists, the learned model's weights) is told about each use,
eviction, and page-in.

The mid-term tier is MemoryOS's segment store: evicted memories wait grouped
by trace; a segment that keeps being used is promoted whole, and the coldest
segment, when mid-term is full, is consolidated into one summary.

## Questions this design settled

- **Ordering on the blackboard.** Agents that name what they `produce` run upstream first, so a consumer rarely does work a producer is about to make stale; reads make the remaining reruns correct.
- **Access counts and the log.** Reads stay out of the store's log, which would otherwise grow with every lookup. `AccessLog(record=True)` keeps the full sequence beside it, which is what policy benchmarks replay.
- **Read references and scopes.** A read reference can carry the reader's scopes, so a memory it could not see does not invalidate its work.
