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

- **Choice.** MIT. The paper keeps its own text license.
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

## 2026-10-21: A kind can require its premises

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

- **Choice.** The results page states what the benchmark shows against the library: episodic search alone fails on facts that change, tools alone remember only what they are asked to, and, with the built-in extractor, the notes add little over search.
- **Why.** The first is why both tiers are needed; the last is the true state and points at the extractor. Hiding any of them would make the benchmark a demonstration instead of a measurement. (An earlier run also showed dreaming alone beating dreaming plus tool notes, from duplicates crowding the top results. After the stemming and paraphrase changes below the two are equal, so the results no longer claim it.)
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

## 2026-10-21: Manuals share one design, and each has a demo that replays real output

- **Choice.** The five manuals (MemCommit, OntoDB, KROMA, Aegis, Agentic Ontologist) keep their own repos and sites but share one stylesheet with a per-project accent, modelled on the keras-js site. Each home page has a live demo that replays output the project really produced: a memory log and its stale-cascade, queries and plans from the OntoDB shell, KROMA prompts built by its own code, Aegis attack and defence results, and one recorded routing run. `restyle.py` in `memcommit-docs` applies the design to a sibling manual.
- **Alternatives.** One combined site for all five; a hosted live model behind each demo; demos that recompute in the browser.
- **Why.** Each project has its own release cycle and audience, so a combined site would blur them. A demo that replays recorded output cannot drift from the code or need a key, and the page says it computes nothing.
- **Cost.** A demo is only as fresh as its export: each manual has a `demo_data.py` to rerun when results change. The demos show a replay, not a live system, so a visitor cannot try their own input. KROMA's answers come from another project's recorded run.

## 2026-10-21: Serve the manuals from lamng3.com

- **Choice.** Each manual is proxied by Netlify under lamng3.com (`/memcommit/`, `/ontodb/`, `/kroma/`, `/aegis/`, `/agentic-ontologist/`, with the showcase at `/agentic-ontologist/showcase/`), like keras-js. Links point at lamng3.com, not lamng3.github.io.
- **Alternatives.** Leave the manuals on GitHub Pages addresses; a custom domain per manual.
- **Why.** One domain for the portfolio, and the manuals' relative links work under a prefix.
- **Cost.** A new manual needs a rule in `_redirects`, and GitHub Pages still answers at the old address, so old links keep working but are not the canonical ones.

## 2026-10-22: Playgrounds run in the browser, and say what they are

- **Choice.** Each manual has a Playground page with no server of ours. Where a project can be stood in for by a web engine, the page runs that engine on the real data (OntoDB and Hermes use Oxigraph, a SPARQL engine in WebAssembly, and say so). Where part of a Python project can be ported, the page runs a JavaScript port and tests it against the Python: MemCommit's log, Aegis's toy ontology and attack searches, KROMA's prompt, Hermes's prompt and extraction, and the Agentic Ontologist's evidence tier. A model is never hosted: the visitor points the page at their own endpoint, or pastes a model's reply.
- **Alternatives.** Compile each project to WebAssembly (OntoDB needs Emscripten and raptor2; the Python projects need a Python runtime); host a model behind each page; show recordings only.
- **Why.** A hosted model costs money and needs keys; a recording cannot be tried. A port that is compared with the Python on recorded scenarios is a claim a reader can trust, and the comparison found real mismatches while it was being built: retracting a memory also stales what was built on it, an ontology cycle can return a query's own root, and two candidates with identical similarity can be kept in a different order.
- **Cost.** A port covers only part of its project and can drift, so each page states what is not ported and the tests must be rerun when the code changes. Calling a model on the visitor's machine from an HTTPS page needs their browser's permission (Chrome asks; Safari refuses), hence the paste option.

## 2026-10-23: A line-by-line visualizer, served under the manual

- **Choice.** A separate static site, `memcommit-visualization`, steps through the real code one executed line at a time: the Python on the left with the current line marked, and on the right the log, the state with its tier lanes, and the provenance graph, redrawn after every line. Four walk-throughs: invalidation and rollback, log compaction, trace compaction with eviction, and one write followed by a crash and a reopen. It is served at `lamng3.com/memcommit/visualize/` (a rule ahead of `/memcommit/*` in `_redirects`) and linked from the manual's header (a Showcase tab), sidebar, and home page.
- **Alternatives.** More text in `memcommit demo`; a page inside the manual that replays a recording; a visualizer driven by the Python library through a local server.
- **Why.** The demo's output is correct but the thing it describes (a log, a status per memory, a tier per memory, edges between memories) cannot be seen in text, so it is hard to follow for visual readers. A traced port of the library, with a hook at each line worth showing, makes the state visible without a server. Its tests replay the same scenarios on the Python library (`golden.py` writes the expected records) and compare every memory id, log record, and hash, and check that each Python line shown exists in the library.
- **Cost.** The port covers the store, rollback, log and trace compaction, and eviction, not guards, observers, query references, or the SQLite and JSONL engines. `golden.json` must be regenerated and the tests rerun when the library changes; the test that checks the displayed lines needs the library checked out next to it.

## 2026-10-23: The showcase shows steps, not code

- **Choice.** The visualizer no longer shows the Python, the locals, or the call stack. Each step has a name ("Find what was built on it", "Commit", "Fold the log") and a one-sentence description, and the memory layers, the log, the tier lanes, and the provenance graph redraw after it. Steps that only made sense next to the source (reading the clock, bumping a counter) are skipped. The Showcase tab opens it from every page of the manual, phones included.
- **Alternatives.** Keep the source pane, as the ART stepper does; offer source as a toggle.
- **Why.** The page is for readers who want to see how memory behaves, not how the library is written; code beside the picture competes with it. The steps are still anchored to library lines, and the tests check those lines exist and that no description names a code identifier.
- **Cost.** A reader who wants the code has to open the library. The step names are a second vocabulary to keep in line with the code.

## 2026-10-23: The showcase speaks in agent and database terms

- **Choice.** Each step names the database idea it is an instance of (BEGIN, buffered write, write-ahead log append as the commit point, redo, checkpoint, crash recovery, compensation records, log compaction), the boxes carry database names, and the state shows how many memories the agent's recall can still see. A panel says what live, stale, retracted, and the archive mean to an agent.
- **Alternatives.** Library terms only (put, status, tier); a glossary page in the manual.
- **Why.** "Stale" was clear as a colour but not as a consequence. For an agent, a bad memory is one it can no longer rely on: stale (outdated) or retracted (wrong), and the library's answer is that recall and search stop returning it while fetching by id still shows it with its state. Database names let a reader who knows databases map the design at a glance.
- **Cost.** The analogies are approximate where MemCommit differs: the log is the only durable copy, not a journal in front of data pages, and a snapshot is a convenience, not required for recovery.
