# Design

Why Mneme is built the way it is. `PLAN.md` says what is built and what is next.

## Why a separate layer

MOIRA keeps a three-tier query cache, and it answers from that cache before it
retrieves. Two things go wrong when the ontology under it changes:

- **Poisoned answers outlive a rollback.** Answers cached while the ontology was poisoned are still served after Aegis rolls the ontology back.
- **The cache is keyed by file, not by version.** An ontology that lives in a database has no file to hash.

OntoDB is mechanism (triples, transactions, recovery) and should stay a
general database. Aegis is defense, and its paper is security. What to
remember, when to promote, and what to evict is policy driven by how agents
behave, so it gets its own layer (Aegis `docs/decisions.md`, Oct 15, 2026).

## Memories are values

A `Memory` is immutable, and its id is a hash of its kind, key, body, source,
scope, and `derived_from`. Two consequences:

- Writing a live memory again writes nothing, and redoing work that comes out identical *restores* the old memory instead of creating a new one. After a rollback, the demo's answer has the same id it had before the attack.
- A memory read back from storage is checked against its id, so an altered body is caught on load.

## Status is a fold over the log

Each `Record` puts, restores, retracts, or marks stale one memory. `State` is
the result of applying records in LSN order, with two indexes: by
(kind, key), and from each reference to the memories derived from it. Replay
rebuilds exactly the state that wrote the log, and `as_of(lsn)` is a replay
that stops early.

`stale` and `retracted` are different on purpose. Stale means something it was
built on no longer holds, and redone work may bring it back. Retracted means it
was wrong.

Rollback is compensation, as in ARIES: the store computes the status of every
memory as of the target LSN and writes the records that move each one back.
Nothing is erased.

## Provenance drives invalidation

`derived_from` holds memory ids and outside references (`ontology:materials@3`,
`file:sha256...`). `invalidate(refs)` marks stale every live memory reachable
through the reference index, in one transaction. This is the same idea as
Aegis's action log: each memory records the ontology version it was derived
from, and a rollback past that version invalidates it.

## Reads are provenance too

An answer derives from the memories it read, but it also depends on the ones
it looked for and did not find. "Ceramic has no alignment" is built on an
empty read, and the first match for `Ceramic` must undo it.

So a reference can name a read: `query_ref("match", source="Ceramic")`, a
`find:` prefix and the query (kind, optional key, body fields). An agent
derives from it next to the memories the read returned. `State` indexes query
references by kind; when a commit brings a memory live, the store finds the
reads that memory now belongs to and marks everything built on them stale,
in the same transaction. Memories written by that commit are left alone.

Only additions need this. A memory that drops out of a read was returned by
it, so whatever used it already derives from it directly. Over-invalidation is
cheap: work redone with the same result restores the same memory id.

## Guards before, observers after

A commit numbers its records, asks every guard, appends to the engine in one
call, applies to the state, then tells every observer. Neither a guard nor an
observer may commit during a commit; an observer that wants to write queues
the work. That keeps every commit's effects in one place.

- `scope_guard`: an agent writes only to `shared` or its own scopes.
- `version_guard`: rejects a memory derived from an ontology version that is no longer current, so a slow agent cannot commit work done against an ontology that has since changed.

## The blackboard

Agents coordinate only through the store. An agent's results and a `receipt`
are written in one transaction; the receipt is derived from the input and
every result. So:

- **Exactly once.** A restarted blackboard queues only inputs without a live receipt.
- **Redo on invalidation.** When a result goes stale, its receipt goes stale with it, and the input is queued again while it still holds.
- **Failure is isolated.** A failing agent's transaction is dropped and an `error` memory is written in its scope.

## Caches are views

The store is the truth; a cache is a copy shaped for fast lookups. `CacheSync`
admits on `live` and evicts on anything else. MOIRA's tiers have no delete,
so `MoiraAnswerCache` rebuilds them from the answers still live, which keeps
promotion counts honest.

## Open questions

- **Ordering on the blackboard.** The queue is FIFO, so work can run before work that would change its result and then run again. Reads make that correct; a dependency-aware order would make it cheaper.
- **Poisoning the memory itself.** Injecting a fake memory, removing a real one, or rewiring what a memory names is a counterfactual attack with Aegis's four primitives. That is Aegis's second paper.
