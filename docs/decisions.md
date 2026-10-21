# Decisions

A log of choices made while building MemCommit, newest last: what was chosen,
what else was possible, why, and what it costs. `design.md` explains the
design as it stands; this file explains how it got there.

## 2026-10-21: Rename Mneme to MemCommit

- **Choice.** The project is MemCommit: repo `lamng3/memcommit`, package and CLI `memcommit`.
- **Alternatives.** Keep Mneme; MemJournal, MemTx, MemCatalog, MemLake, MemLineage.
- **Why.** "Mem" plus a database word people already know. "Commit" names the one guarantee every write has: it is atomic, logged, and attributable. Three syllables, free on PyPI.
- **Cost.** Every importer changed: `kroma`, `aegis`, `agentic-ontologist`, and the `ontodb` README. The OntoDB engine's RDF namespace moved from `urn:mneme:` to `urn:memcommit:`, so a database written before the rename needs its namespace migrated. The library was pre-alpha and none were kept.

## 2026-10-21: MIT license for the code

- **Choice.** MIT. The paper, when written, stays CC BY.
- **Alternatives.** CC BY 4.0 (what the repo had), Apache-2.0.
- **Why.** Creative Commons advises against its licenses for software: CC BY says nothing about patents or about source distribution. MIT is the shortest license people recognize, and the library has no dependencies to reconcile with.
- **Cost.** None the project cares about; Apache-2.0's patent grant is the one thing given up.

## 2026-10-21: A generic core, a declared structure on top

- **Choice.** The core names no domain. A project declares its own memory structure with `Schema` and `Kind`, and the store enforces it with a guard.
- **Alternatives.** Hard-code the kinds the Agentic Ontologist uses; leave bodies free-form and document conventions.
- **Why.** The Agentic Ontologist's structure lived as constants (`QUESTION`, `ANSWER`) in its own integration, enforced by nothing. A declared structure makes the same idea reusable and checkable, and lets an agent read what it may write.
- **Cost.** One more concept to learn. It is optional: a store with no schema behaves as before.

## 2026-10-21: The schema is a guard, and itself a memory

- **Choice.** `Schema.attach(store)` adds a commit guard and writes the schema to the log as a `schema` memory. A changed schema writes a new one and marks the old one stale.
- **Alternatives.** Validate in a wrapper around `put` only; keep the schema outside the log; stamp each memory with a schema version in `derived_from`.
- **Why.** A guard holds for every writer in every process, including the server and the blackboard, and it sees a whole commit, so `requires` can be satisfied by a premise written in the same transaction. Putting the schema in the log means `as_of(lsn)` also shows which structure held then. Stamping each memory with a version in `derived_from` would stale all data on every schema change, which is the wrong blast radius.
- **Cost.** Old data is not revalidated when the schema changes. `Schema.audit(store)` finds the live memories that no longer conform.

## 2026-10-21: A small JSON-schema subset, no dependency

- **Choice.** `check_body` implements `type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `minItems`, `maxItems`.
- **Alternatives.** Depend on `jsonschema`; use dataclasses or pydantic models for bodies.
- **Why.** MemCommit has no runtime dependencies and should stay that way. The subset covers what memory bodies need, and a schema written in it stays valid JSON Schema, so it can move to a full validator later.
- **Cost.** No `$ref`, `oneOf`, or `pattern`. A kind that needs them can add a guard.

## 2026-10-21: `requires` ties structure to provenance

- **Choice.** A kind can require that memories of it derive from memories of named kinds (an `answer` from a `question`).
- **Alternatives.** Only validate bodies; leave provenance rules to `provenance_guard`.
- **Why.** Provenance is what invalidation runs on. Declaring the expected premises with the structure means a memory cannot be written with its lineage missing.
- **Cost.** `requires` checks the kind of a premise, not that it is live.

## 2026-10-21: A session is a trace, not a new structure

- **Choice.** `Session` wraps `Trace`: user and agent turns are `message` steps with a `role`, tool calls are `action` plus `observation` (or `error`) steps. `Episodes` finds sessions, and `new_since(lsn)` lists those that ended after a point in the log.
- **Alternatives.** A separate episodic table or kind with its own log; storing a transcript as one memory per session.
- **Why.** A trace already commits each step on its own (a crash keeps what was said), has handoffs, compacts into summaries, and archives. Reusing it keeps one episodic format for the compactor, recall, and audit. One memory per session would lose the step-by-step durability.
- **Cost.** A session's turns are generic `message` memories, so queries for "user turns" filter on `body.role`.

## 2026-10-21: A reply derives from what it read

- **Choice.** `agent_says(text, used=...)` derives the reply from the memory ids the agent read; `Knowledge.take_reads()` collects them.
- **Alternatives.** Log reads separately and join them later; do not track reads.
- **Why.** Provenance is the library's lever. A reply built on a note that is later retracted should go stale, and this is the cheapest way to make that true for every reply.
- **Cost.** The caller passes `used`. Nothing infers which reads mattered.

## 2026-10-21: Derived memory is notes at paths, with a generated index

- **Choice.** `Knowledge` keeps notes as `knowledge` memories keyed by path (`user/alice/city`). `index()` renders a Markdown catalog by directory, sized to a character budget, keeping the most important and most recent notes when it must cut. It is generated on demand, not stored.
- **Alternatives.** Notes as flat key-value facts; storing an index file as a memory and rewriting it on every change.
- **Why.** Paths give an agent a hierarchy it can scan and reason about, and a bounded index is what fits in context at the start of a session. A stored index would be one more memory to keep consistent and would stale on every write.
- **Cost.** The index is recomputed per call, linear in the number of live notes.

## 2026-10-21: Updates supersede; deletes retract

- **Choice.** `update` writes a new version and marks the old one stale, and by default everything derived from the old version. `delete` retracts, which stales dependents through the store's existing rule.
- **Alternatives.** Edit in place (impossible on an immutable log); update without cascade; delete as stale.
- **Why.** Stale means "something it was built on no longer holds", which is exactly what happens to work built on an outdated note. Retraction means "it was wrong", which is what `delete_memory` is for. Cascade is on by default because that is the library's promise; `cascade=False` is there for typo fixes.
- **Cost.** A frequent update to a heavily cited note stales a lot. The consolidation pass is where stale notes get redone.

## 2026-10-21: Five tools over two tiers

- **Choice.** `tools()` returns specs for `add_memory`, `update_memory`, `delete_memory`, `search_memory`, `fetch_memory`; `call` runs them, and with a `session` records each call and result. `search_memory` takes `where` (`derived`, `episodic`, `all`); `fetch_memory` takes a note path or any memory id, and a session root returns its transcript.
- **Alternatives.** Automatic reads and writes only (no agent-facing tools); one search tool per tier.
- **Why.** Tools let an agent decide when to save and look something up, and cost nothing when unused. One search across tiers, with `fetch` reaching raw episodes, lets an agent recover detail that distillation dropped.
- **Cost.** The tool schemas use a model-agnostic shape (`name`, `description`, `input_schema`); an adapter is needed for providers that differ.

## 2026-10-21: Dreaming is an offline pass with a pluggable extractor

- **Choice.** `Dreamer.run()` reads sessions that ended since the last pass, asks an `Extractor` (a function from a session to `Learning`s) for what is worth keeping, writes each as a note derived from the session and its source turns, then maintains the store. `heuristic_extractor` needs no model: it finds stated facts, preferences, "remember that" statements, and tool sequences that ended well. A language model is a function with the same signature.
- **Alternatives.** Only online tools (the agent saves what it thinks to save); an extractor tied to one model API; extraction inside the commit path.
- **Why.** Online saving misses what is implied, and a long session is expensive to reread at answer time. An offline pass costs nothing at runtime and updates later. A pure function as the extension point keeps the core free of any model dependency and makes the pass testable with exact expectations.
- **Cost.** The heuristic only finds what its patterns match, and notes are only as fresh as the last pass. `Dreamer.attach(min_sessions)` triggers a pass after enough sessions end.

## 2026-10-21: The log decides where a pass starts

- **Choice.** A pass writes a `dream` memory recording the log position it covered. The next pass starts from the highest `covers_lsn`, counting records of any status.
- **Alternatives.** A watermark stored outside the log; a flag on each session.
- **Why.** Everything else about the store is in the log, so the pass history is replayable and auditable with the rest. Counting stale records means a pass whose source session was later retracted is not run again: the notes went stale, and a new pass would not bring back what was invalidated.
- **Cost.** A pass that is undone is not retried automatically.

## 2026-10-21: Conflicts resolve by supersession, not by judgement

- **Choice.** A learning whose path already holds a different note replaces it. The extractor chooses the path (a slot such as `user/alice/city`), and the last statement in the newest session wins.
- **Alternatives.** Ask a model to adjudicate; keep both and let retrieval choose; reject the newer statement.
- **Why.** For facts that change (a city, a preference), the newest statement is almost always right, and the old note goes stale with its dependents, so nothing keeps leaning on it. The history stays in the log, so a wrong choice can be audited and repaired.
- **Cost.** A mistaken or spoofed later statement overwrites a correct note until someone retracts that session; retracting the session restores the staleness chain but does not bring the old note back by itself.

## 2026-10-21: Maintenance archives, it does not delete

- **Choice.** Near-duplicate notes in a directory (word overlap of at least 0.85) are moved to the archive tier, keeping the most important. A note over `max_chars` is split at paragraph breaks into `-part-N` notes derived from it, and the original goes to the archive.
- **Alternatives.** Retract duplicates; merge by rewriting; split into subdirectories.
- **Why.** A duplicate was not wrong, so it should not be retracted (that would stale what was built on it). Archiving removes it from the index and from search by default while keeping it fetchable by id. Sibling `-part-N` paths avoid a path being both a note and a directory.
- **Cost.** Word overlap misses duplicates with different wording. An embedding-based check is an easy extension through the same hook.

## 2026-10-21: A pass can be previewed and undone

- **Choice.** `run(dry_run=True)` returns the changes without writing. `undo(report)` rolls the log back to before the pass, and refuses if any other agent has written since.
- **Alternatives.** No preview; undo by retracting each written note.
- **Why.** Consolidation edits shared knowledge, so a person or a test should be able to see it first. Rollback is compensation, so history is kept. Refusing when someone else wrote avoids undoing their work.
- **Cost.** A dry run previews session learnings against the current notes, and maintenance against the notes as they are now, not as they would be after the new ones land.

## 2026-10-21: Measure retrieval with a recency-aware reader, not a model

- **Choice.** `memcommit.evaluation` builds a synthetic multi-session workload and scores configurations with a deterministic reader: right if the latest-mentioned value the user ever gave is the current one. Configurations are none, full context, episodic, derived with online tools, derived with dreaming, derived with both, and both tiers.
- **Alternatives.** Score with a language model as reader; use an existing long-conversation benchmark.
- **Why.** A model reader adds cost, noise, and a dependency, and mixes how well memory retrieves with how well a model reads. A deterministic reader makes every number reproducible and isolates retrieval and cleanliness. The workload is built so the questions need what each tier is for: facts said once, facts that change, facts implied, and facts asked to be remembered.
- **Cost.** The workload is synthetic, so the numbers show behaviour, not real-world accuracy. Recorded conversations, and a model reader, are the next step; the harness takes a workload and a retrieval function, so both slot in.

## 2026-10-21: Report the result that is unflattering

- **Choice.** The results state that dreaming alone beats dreaming plus online tools, and that episodic search alone fails on facts that change.
- **Why.** The first is a real effect of crowding and duplicate notes; the second is why the tiers are both needed. Hiding either would make the benchmark a demonstration instead of a measurement, and both point at work to do (deduplication across paths, stemming or embeddings).
- **Cost.** None the project minds.

## 2026-10-21: Stem and drop stopwords in the lexical index

- **Choice.** `LexicalIndex` searches by lowercase terms without a small list of stopwords, with a light stemmer (plural, past, and gerund endings), so `live`, `lives`, `lived`, and `living` agree.
- **Alternatives.** Leave exact word matching; require an embedding index for any fuzzy match.
- **Why.** The demo answered "How is the weather?" with a note about a wifi password (the shared words were "is" and "the"), and the benchmark showed "live" failing to find "lives". Both are standard lexical-search failures with standard fixes, and MemCommit has no dependencies to lean on for better ones.
- **Cost.** A crude stemmer conflates some unrelated words, and stopword removal drops them from exact phrase intent. An embedding index (`VectorIndex`) remains the way to match meaning.

## 2026-10-21: Fix a circular benchmark rather than report it

- **Choice.** Half of the workload's statements are now paraphrases the rule-based extractor cannot parse. An oracle extractor, labelled as an upper bound, separates the extractor from the rest of the pipeline.
- **Why.** The first benchmark gave the rules text written to match them and showed derived memory at 1.000, which measured nothing. With paraphrases, derived memory falls to about 0.49 and the two tiers roughly tie search alone, which is the true state: the pipeline works, the rule extractor is the limit.
- **Cost.** The headline is less flattering, and the case for the notes now rests on a model-backed extractor that is not built yet. The results page says both.

## 2026-10-21: A demo that needs no model

- **Choice.** `memcommit demo` runs a scripted assistant through declaring the structure, sessions with tools, the index at session start, dreaming, a changed fact, a retracted episode, and the benchmark, with no network and no model. `index`, `search`, and `dream` inspect any store from the command line.
- **Why.** A first run should work in seconds, offline, and show the whole loop. A scripted assistant keeps the output deterministic, so it can be tested and quoted.
- **Cost.** The assistant is a stand-in. Wiring a real model means writing its tool-calling loop against `Knowledge.tools()`.
