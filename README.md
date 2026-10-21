<div align="center">

# 🧠 MemCommit

**A Transactional Memory Layer for Agents**

[![CI](https://github.com/lamng3/memcommit/actions/workflows/tests.yml/badge.svg)](https://github.com/lamng3/memcommit/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3b82f6)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Design](https://img.shields.io/badge/docs-design-8b5cf6)](docs/design.md)

[⚡ Quick start](#-quick-start) · [✨ What it is](#-what-it-is) · [📊 Results](#-results) · [🔧 Usage](#-usage) · [🧭 Roadmap](#-roadmap) · [📝 Citation](#-citation)

</div>

<p align="center"><b>Lam Nguyen</b></p>

> [!NOTE]
> **Research software.** Every feature in the [plan](PLAN.md) is built and
> tested: the log-structured store, a declared memory structure, episodic
> sessions, derived notes with an index and agent tools, offline consolidation
> ("dreaming"), the short-term cache and its policies, compaction of memories
> and of the log, recall, the blackboard, audit, and a server. Results so far
> are on synthetic workloads.

## ⚡ Quick start

```bash
git clone https://github.com/lamng3/memcommit.git
cd memcommit
pip install -e ".[dev]"
python examples/shared_memory.py
```

```text
1. Ask three questions; the agents answer through the shared memory
   6 agent steps, 3 traces
2. Working memory stays bounded; finished traces become long-term summaries
   tiers: {'short': 12, 'archive': 18, 'long': 3}
4. A note arrives later; the brief that read 'no notes' is redone
   Speed of light: 299,792 km/s (in a vacuum).
5. The lookup tool is compromised, and a re-asked question picks up the lie
   Speed of light: about 30 km/s (in a vacuum).
6. A defender traces the damage and quarantines the poisoned observation
   Speed of light: 299,792 km/s (in a vacuum).
7. The record
   verify: ok
```

Two agents share one memory: a researcher calls a tool and records what it
found, and a writer turns findings into briefs. Neither calls the other. When
the tool is compromised, the defender finds everything the bad observation
reached and quarantines it, and the agents redo only that work. Python 3.11
or newer; no runtime dependencies.

## ✨ What it is

Agents produce traces: thoughts, tool calls, observations, messages, results.
Several agents working together read each other's traces as premises. MemCommit
stores all of it as JSON memories in one log, and treats agent memory as a
database problem: a log-structured store, a cache in front of it, and
compaction at two levels.

It keeps two kinds of memory. **Episodic memory** is the full record of what
happened: every session, turn, and tool call, appended as it occurs. **Derived
memory** is what was learned from it: short notes at paths (`user/alice/city`),
indexed so an agent can read the catalog first, searched across both tiers, and
fetched in full when the detail matters. Agents write notes with tools; an
offline pass ("dreaming") also extracts what the agents did not think to save,
and keeps the notes tidy. Every note records the episode it came from, so a bad
episode takes its notes with it.

<div align="center">

| Database idea | In MemCommit |
|:--|:--|
| Write-ahead log, replay, compensation | Every change is a record; state is a fold over the log; rollback writes compensating records |
| Buffer pool / cache | Short-term memory: bounded per agent, `touch` is a hit or a miss, misses page back in |
| Replacement policy | LRU, LFU, FIFO, 2Q, ARC, a forgetting curve, TTL, a scored or a learned policy |
| Compaction (LSM) | Memory compaction: traces fold into summaries, cold segments consolidate. Log compaction: history folds to the fewest records that replay the same state |
| Retention, vacuum | Purge archived memories past a retention policy when the log is compacted |
| Checkpoints | Snapshots of the state; open replays only the tail |
| Integrity | Content-addressed memories, hash-chain seals, `verify` |
| Client/server | One process owns the log and runs every commit; agents in other processes use `RemoteStore` |

</div>

- **Two tiers of memory.** Episodic sessions (`Session`) keep everything; derived notes (`Knowledge`) keep what matters, with a generated index and five agent tools (`add_memory`, `update_memory`, `delete_memory`, `search_memory`, `fetch_memory`).
- **Dreaming.** An offline pass (`Dreamer`) reads sessions that ended since the last one, extracts learnings with a pluggable extractor (a model, or the built-in rules), supersedes conflicting notes, merges duplicates, splits oversized notes, and can be previewed (`dry_run`) or undone.
- **A declared memory structure.** A project names its kinds once (`Schema`, `Kind`): the shape of each body, where it may be written, what it must be derived from. A commit guard enforces it for every writer, and the schema itself is recorded in the log.
- **Traces are first-class.** A run is a root and a tree of typed steps. Each step commits on its own, so a crash keeps what happened, and a handoff starts another agent's sub-trace.
- **Provenance drives invalidation.** Every memory records what it was `derived_from`. When a premise goes bad, everything built on it goes stale, transitively, in one transaction, and the agents that built on it redo their work.
- **Reads are provenance too.** A memory can derive from a read (`query_ref("note", topic=...)`), so work that saw "nothing known" is redone when something becomes known.
- **Short-term memory is a cache.** It is bounded per scope; eviction moves memories out (to the archive, or to a mid-term tier of segments), promotion moves them to long-term, a used archived memory is paged back in, and compaction turns finished traces into long-term summaries. Every decision is a log record, and `tiers.stats` counts hits and misses.
- **A database underneath.** Log compaction, retention, snapshots, seals, and a server for agents in many processes, on SQLite, JSONL, or any engine with two methods.
- **A log, not a table.** Status and tier are a fold over an append-only log, so any past state can be rebuilt, and rollback is compensation that keeps history.
- **Built to be defended.** Memories are content-addressed, writes pass through guards, and audit tools (`verify`, `lineage`, `blast_radius`, `quarantine`, `provenance_guard`) give a defender exact repairs.

<div align="center">

| Tier | Holds | Moves out by |
|:--|:--|:--|
| `short` | The cache: new memories, an agent's working context | Eviction, to `archive` (or `mid`); promotion, to `long` |
| `mid` | Optional: evicted memories grouped into segments (by trace) | A hot segment is promoted to `long`; a cold one is consolidated into a summary and archived |
| `long` | Consolidated memories and summaries | Optional capacity and eviction, to `archive` |
| `archive` | Everything evicted or compacted; searched on request; paged back in on use | Purged only by log compaction under a retention policy |

</div>

## 📊 Results

**Does memory help?** 20 users chat over 8 to 64 sessions each; facts come up in
passing (half in paraphrases the built-in extractor cannot parse), some change
later, and the rest is chatter and distractors. Each user is then asked for their
current city, dog, drink, and locker code. A deterministic reader (no model)
answers from whatever each configuration retrieves, so this measures retrieval
and how clean it is. Synthetic; means over 3 seeds.

<div align="center">

| Configuration | 8 sessions | 32 sessions | 64 sessions | Context words at 64 |
|:--|:--:|:--:|:--:|--:|
| none | 0.000 | 0.000 | 0.000 | 0 |
| full history in context | 1.000 | 1.000 | 1.000 | 1,358 |
| episodic search only | 0.958 | 0.958 | 0.958 | 20 |
| notes from dreaming only | 0.492 | 0.512 | 0.487 | 15 |
| episodic + notes | 0.942 | 0.971 | 0.958 | 35 |
| notes from dreaming, oracle extractor * | 1.000 | 1.000 | 1.000 | 12 |
| episodic + notes, oracle extractor * | **1.000** | **1.000** | **1.000** | 33 |

<sub>`python examples/memory_benchmark.py` · * an extractor that parses every phrasing, an upper bound · more in [`results/memory/`](results/memory/README.md)</sub>

</div>

Search over the record reads 20 words where the full history needs 1,358, and is
right 96% of the time; its misses are facts that changed, since the move often
shares few words with the question. Notes fix that when extraction is good: with
a perfect extractor they are right every time from 12 words, because the later
statement supersedes the old note. With the built-in rule extractor, which parses
half the phrasings, they are right about half the time, and the two tiers together
only match search alone. So the pipeline holds up, and extraction quality is what
limits it; a model-backed `Extractor` is the next step. (An earlier version of this
benchmark used only phrasings the rules were written for and scored 1.000: that was
circular, and the results page says so. Its paraphrases also share words with the questions, which favours search; the results page says that too.)

**Which eviction policy?** How often is a memory an agent needs still in working memory? A synthetic,
agent-shaped workload (60 tasks writing 6 trace steps each, then 472 skewed
reuses of earlier memories) replayed through the real store under each
policy. A sanity check, not a benchmark on recorded agent traces.

<div align="center">

| Policy | Hit rate, cap 16 | cap 32 | cap 64 | Peak working memory | Log records | Replay ms |
|:--|:--:|:--:|:--:|:--:|--:|--:|
| FIFO | 0.506 | 0.551 | 0.602 | 32 | 1,112 | 42 |
| LRU | 0.642 | 0.744 | 0.828 | 32 | 930 | 38 |
| LFU | 0.354 | 0.460 | 0.606 | 32 | 1,198 | 46 |
| Scored (recency, importance, frequency) | 0.377 | 0.542 | 0.826 | 32 | 1,120 | 48 |
| Forgetting curve | **0.648** | **0.771** | **0.867** | 32 | **904** | 41 |
| 2Q | 0.381 | 0.481 | 0.606 | 32 | 1,178 | 47 |
| ARC | 0.551 | 0.663 | 0.763 | 32 | 1,006 | 41 |
| Learned (online logistic) | 0.627 | 0.742 | 0.803 | 32 | 932 | 51 |
| LRU + promote used (≥ 3 uses) | 0.869 | 0.894 | 0.924 | 121 | 790 | 51 |
| Scored + promote important | 0.631 | 0.867 | 0.922 | 103 | 814 | 62 |

<sub>Peak working memory, log records, and replay time at cap 32 · `python examples/policy_benchmark.py` · all tables per capacity in [`results/README.md`](results/README.md), raw reports in `results/policies/`</sub>

</div>

Cost follows quality: every miss is a page-in and an eviction, both log records, so the policy with the best hit rate under the cap (the forgetting curve) also writes the fewest records, 19% fewer than FIFO. Promotion writes fewer still, because promoted memories stop churning, at the price of holding more. A missed memory is paged back in on use. With working memory held to its
cap, the forgetting curve does best: it keeps what was used often or marked
important, but lets even those fade. LFU and 2Q do poorly here, for one
reason: this workload reuses fresh trace steps often, and both evict memories
used only once first, which is exactly what a fresh step is. Promotion raises
the hit rate by moving memories to long-term, which grows what is held (peak
121 against a cap of 32); bound long-term with `long_capacity` to trade that
back. ARC and the learned policy sit between: both adapt, but on a workload
this short they have little history to adapt from. A second seed gives the
same order. Which policy wins depends on the workload, which is why policies
are pluggable and the benchmark replays any workload, including one recorded
from real agents (`AccessLog(record=True)`, then `bench.from_access_log`).

## 🔧 Usage

```python
import memcommit
from memcommit import LRU, AccessCount, Compactor, Recall, TieredMemory

store = memcommit.open("memory.db")                    # SQLite; .jsonl works too
tiers = TieredMemory(store, capacity=64, eviction=LRU(), promotion=AccessCount(3),
                     compactor=Compactor(store))
recall = Recall(store, tiers=tiers)

with memcommit.trace(store, "researcher", "how fast is light?") as t:
    t.thought("look it up")
    call = t.action("lookup", {"topic": "speed of light"})
    seen = t.observation("299,792 km/s", parent=call)
    t.result("299,792 km/s")

store.put("researcher", "fact", "speed of light", {"value": "299,792 km/s"}, derived_from=[seen.id])
hits = recall.search("speed of light", k=3)        # relevance + recency + importance
store.invalidate([seen.id], reason="the tool was wrong")   # the fact goes stale too
```

<details>
<summary><b>A declared memory structure</b></summary>

```python
from memcommit import Kind, Schema

schema = Schema(
    Kind("question", body={"type": "object", "required": ["text"]}),
    Kind("answer", requires=("question",), scopes=("shared", "agent"), tier="long",
         body={"type": "object", "required": ["text", "confidence"],
               "properties": {"confidence": {"type": "number", "minimum": 0, "maximum": 1}}}),
    strict=True,                      # undeclared kinds are rejected
)
schema.attach(store)                  # a commit guard, and the schema recorded in the log
schema.put(store, "asker", "question", "q1", {"text": "why?"})
schema.audit(store)                   # live memories that do not conform
print(schema.describe())              # the structure as Markdown, for agents
```

A kind declares the body's shape (a JSON-schema subset), where it may be
written, the kinds it must be derived from, and its default tier. The guard
holds for every writer, in every process, and a changed schema supersedes the
old one in the log, so `as_of(lsn)` shows which structure held when.

</details>

<details>
<summary><b>Sessions, notes, and dreaming</b></summary>

```python
from memcommit import Dreamer, Knowledge, Session

with Session.start(store, "assistant", user="alice") as session:   # episodic: everything, as it happens
    session.user_says("I live in Lyon, and my dog is named Rex.")
    search = session.wrap(web_search, "web_search")                 # tool calls recorded with their results
    session.agent_says("Noted.")

notes = Knowledge(store, "assistant")                               # derived: what was learned
notes.add("user/alice/city", "Alice lives in Lyon.", derived_from=[session.id])
print(notes.index())                                                # the catalog, to inject at session start
notes.search("where does alice live", where="all")                  # derived notes and episodes together
notes.fetch("user/alice/city")                                      # in full; a session id returns its transcript
notes.tools()                                                       # five tool specs a model can call

dreamer = Dreamer(store)                    # or Dreamer(store, extractor=my_model_extractor)
dreamer.run(dry_run=True).changes           # preview what it would change
report = dreamer.run()                      # extract, supersede conflicts, merge duplicates, split big notes
dreamer.undo(report)                        # roll a pass back, if nobody else has written since
```

An update writes a new version and marks the old one stale, and what was built
on it; a retracted note, or a retracted episode, takes everything derived from
it down too. `Dreamer.attach(min_sessions=3)` runs a pass whenever enough
sessions have ended.

</details>

<details>
<summary><b>Memories, transactions, and provenance</b></summary>

A memory is an immutable JSON value: `kind`, `key`, `body`, `source` (the agent
that wrote it), `scope` (who may read it), `importance`, and `derived_from`.
Its id is a hash of all of that, so writing the same memory twice names the
same memory, and redone work that comes out the same restores the old id.

```python
with store.transaction("planner") as tx:                 # all or nothing
    plan = tx.put("plan", "trip", {"steps": ["book", "pack"]}, derived_from=["doc:itinerary@3"])
    tx.put("task", "book", {"by": "friday"}, derived_from=[plan.id])

store.invalidate(["doc:itinerary@3"], reason="the itinerary changed")   # plan and task go stale
store.rollback_to(2)                                     # compensation; history kept
store.as_of(1)                                           # the state at any LSN
```

`stale` means something it was built on no longer holds; `retracted` means it
was wrong. Both keep the memory in the log.

</details>

<details>
<summary><b>Traces</b></summary>

```python
from memcommit import Trace

with memcommit.trace(store, "planner", "book a trip", inputs={"city": "Lyon"}) as t:
    call = t.action("search_flights", {"to": "LYO"})
    t.observation({"flights": 3}, parent=call)
    helper = t.handoff("booker", "book the cheapest flight")   # another agent's sub-trace
    helper.result({"booked": "AF123"})
    helper.end()

Trace.open(store, t.id).to_json()     # the run as one JSON document
```

Each step is derived from the root and its parent step. A run that raises ends
with status `error`. Step kinds are open; `thought`, `action`, `observation`,
`message`, `result`, and `error` have helpers.

</details>

<details>
<summary><b>Tiers, policies, and compaction</b></summary>

```python
from memcommit import ARC, Learned, TTL, Forgetting, Scored, AnyOf, ImportanceAtLeast, KindIn, KeepFailed

tiers = TieredMemory(
    store,
    capacity=64,                                   # per partition; a partition is a scope by default
    eviction=Scored(recency=1.0, importance=1.0, frequency=0.5),
    promotion=AnyOf(AccessCount(3), ImportanceAtLeast(0.8), KindIn(["decision"])),
    compactor=Compactor(store, policy=KeepFailed(records=5000), summarizer=my_llm_summary),
    mid_capacity=256, heat_threshold=10.0,          # a mid-term tier of segments, MemoryOS-style
    long_capacity=10_000,
)
tiers.touch([m.id for m in context])   # count a use; archived memories are paged back in
tiers.working(scopes=["shared", "planner"])
tiers.maintain()                       # runs on its own after a commit leaves a partition over capacity
```

A policy is any object with `victims(candidates, n, now)` or
`select(candidates, now)`. A candidate carries the memory, its tier and
status, and its use counts on a logical clock, so policies are small and
testable alone. A summarizer is any function from a `Trace` to JSON.

</details>

<details>
<summary><b>Agents on a blackboard</b></summary>

```python
from memcommit import Blackboard

class Writer:
    name = "writer"
    consumes = frozenset({"finding"})

    def handle(self, finding, tx, session):
        tx.put("brief", finding.key, {"text": "..."}, derived_from=[finding.id])

board = Blackboard(store, [Researcher(), Writer()])
board.run()
```

An agent's results and a receipt commit together, so work is done exactly
once, survives a restart, and is redone when its inputs go stale. A failing
agent writes nothing but an `error` memory in its own scope.

</details>

<details>
<summary><b>Audit and defense</b></summary>

| Tool | What it does |
|:--|:--|
| `verify(store)` | Replays the log; reports altered bodies, records out of order, provenance to memories that did not exist, writes into another agent's scope |
| `lineage(store, id)` | Everything a memory was built on |
| `blast_radius(store, refs)` | Everything built on a reference, in any status |
| `quarantine(store, ids, reason)` | Retracts suspects and marks what was built on them stale, in one transaction |
| `diff(store, a, b)` | Memories whose status or tier changed between two LSNs |
| `provenance_guard()` | Rejects at write time a memory built on a memory that is missing or no longer holds |
| `seal(store)` | Writes a hash chain over the log; `verify` rechecks it, so a deleted or reordered record is caught |
| `scope_guard` | An agent writes only to `shared` or its own scopes |

</details>

<details>
<summary><b>The database: log compaction, retention, snapshots, seals</b></summary>

```python
from memcommit.storage import ArchivedOlderThan, AutoSeal, check_seal, compact

report = compact(store, retention=ArchivedOlderThan(seconds=30 * 86400))
print(report.before, "->", report.after, "records; purged", len(report.purged))

store.save_snapshot("state.json")
store = memcommit.open("memory.db", snapshot="state.json")   # replays only the records after the snapshot
AutoSeal(store, every=1000)                               # a hash-chain seal every 1000 records
assert check_seal(store)
```

Log compaction keeps, for each memory, its put, its last status change, and
its last move, as the original records, so the folded log replays to the same
state; records after the horizon are kept verbatim, and the engine swaps logs
atomically. A `compaction` manifest records what was folded and purged, so
`verify` still passes. History inside the horizon is what compaction gives up.

</details>

<details>
<summary><b>Many processes, an event loop, many vectors</b></summary>

```bash
python -m memcommit serve memory.db --port 7340      # one process owns the log
python -m memcommit verify memory.db                 # also: compact, stats
```

```python
from memcommit.server import RemoteStore
store = RemoteStore("http://127.0.0.1:7340")      # a MemoryStore: traces, tiers, recall all work
store.refresh()                                   # pull commits from other processes

from memcommit import AsyncMemoryStore, AnnIndex
astore = AsyncMemoryStore(memcommit.open("memory.db"))           # await astore.put(...)
recall = Recall(store, index=AnnIndex(embed, dimensions=768))  # LSH for large vector recall
```

The server runs every commit, so LSNs and guards hold across processes; a
refused commit raises `Rejected` in the client. Agents on a blackboard that
name what they `produce` run upstream first.

</details>

<details>
<summary><b>Engines</b></summary>

| Engine | Where the log lives |
|:--|:--|
| `MemoryEngine` | A list, for tests |
| `JsonlEngine` | One fsynced JSON line per transaction; a torn final line is dropped |
| `SqliteEngine` | One SQLite transaction per commit, WAL journaling |

Another backend subclasses `Engine` (two methods: `append` and `records`;
`records_after` and `rewrite` have defaults it may speed up) and registers in
`ENGINES`.

**Provenance as a graph.** An engine can keep the log in an RDF store, writing
each record and each memory as triples, with `derivedFrom` edges. Provenance
questions then become SPARQL. Which memories were built on this one?

```sparql
SELECT ?m WHERE { ?m <urn:memcommit:derivedFrom> <urn:memcommit:m:4e33527c048490ae> . }
```

OntoDB is one such store; its engine lives with the workflow that uses it.

</details>

## 🧭 Roadmap

See [`PLAN.md`](PLAN.md) for what runs now and what is next,
[`docs/design.md`](docs/design.md) for why, and
[`docs/decisions.md`](docs/decisions.md) for the choices made along the way, with
the alternatives and costs. Next: deduplication across paths and a stemming or
embedding index (the benchmark shows where lexical search falls short), and the
same benchmark on recorded conversations with a model as reader.

## 📝 Citation

```bibtex
@misc{nguyen2026memcommit,
  title  = {MemCommit: A Transactional Memory Layer for Agents},
  author = {Nguyen, Lam},
  year   = {2026},
  url    = {https://github.com/lamng3/memcommit}
}
```

## License

[MIT](LICENSE). Copyright (c) 2026 Lam Nguyen.
