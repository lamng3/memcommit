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
