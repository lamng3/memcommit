<div align="center">

# 🧠 Mneme

**A Transactional Memory Layer for Multi-Agent Systems**

[![CI](https://github.com/lamng3/mneme/actions/workflows/tests.yml/badge.svg)](https://github.com/lamng3/mneme/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3b82f6)](pyproject.toml)
[![License: CC BY 4.0](https://img.shields.io/badge/license-CC%20BY%204.0-green.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Design](https://img.shields.io/badge/docs-design-8b5cf6)](docs/design.md)

[⚡ Quick start](#-quick-start) · [✨ What it is](#-what-it-is) · [🔧 Usage](#-usage) · [🧭 Roadmap](#-roadmap) · [📝 Citation](#-citation)

</div>

<p align="center"><b>Lam Nguyen</b></p>

> [!NOTE]
> **Work in progress.** The store, the blackboard, caches, and adapters to
> OntoDB, Aegis, MOIRA, and KROMA are built and tested on a toy ontology.

## ⚡ Quick start

```bash
git clone https://github.com/lamng3/mneme.git
cd mneme
pip install -e ".[dev]"
python -m pytest -q
```

The end-to-end demo needs [Aegis](https://github.com/lamng3/aegis), MOIRA, and
KROMA cloned beside this repository. [OntoDB](https://github.com/lamng3/OntoDB)
is optional; without it, the ontology and the memory are kept in JSONL.

```bash
python examples/four_systems.py --ontodb ../ontodb/build/dev/ontodb
```

```text
3. An honest edit through Aegis: add Ceramic under Material, and align it
   MOIRA (cache ): Alloy; Ceramic (MatOnto: Ceramics); Metal (MatOnto: MetallicMaterial); ...
4. A counterfactual attack: rewire Metal under Temperature, flood in a look-alike
   MOIRA (cache ): Ceramic (MatOnto: Ceramics); Metall; Polymer (MatOnto: Plastic)
5. Aegis's detector alarms and rolls the ontology back
   poisoned answers now stale: 1; MOIRA cache rebuilds: 3
6. Ask again: the agents redo the work, and the result is the honest memory, restored
   same answer as before the attack: True; same memory id: True
7. A slow agent tries to commit work built on the poisoned version
   rejected: answer 'late' is derived from materials@6, which is stale
```

Python 3.11 or newer; the core has no runtime dependencies.

## ✨ What it is

Agents remember things: facts they derived, decisions they made, answers they
cached. When what those memories were built on turns out to be wrong (an
ontology is rolled back after an attack, a source file changes), the memories
have to go too, and so does everything built on them. Mneme treats agent
memory as a database problem.

- **Content-addressed memories.** A memory is an immutable value; its id is a hash of its kind, key, body, source, scope, and provenance. Writing the same memory twice names the same memory.
- **Provenance.** Every memory records what it was `derived_from`: other memories, or outside references such as an ontology version. Invalidating a reference marks everything built on it stale, transitively, in one transaction.
- **A log, not a table.** Status (`live`, `stale`, `retracted`) is never stored on a memory. It is a fold over an append-only log, so any past state can be rebuilt, and rollback is compensation that keeps history.
- **Transactions with guards and observers.** Guards reject a commit before it is written (scopes, stale provenance, a defense layer's checks); observers react after it is durable (caches, schedulers).
- **A blackboard.** Agents never call each other. Each consumes kinds of memory and writes results with a receipt in the same transaction, so work is done exactly once, survives a restart, and is redone when its inputs go stale.

Where it sits:

<div align="center">

| Layer | Owns |
|:--|:--|
| OntoDB | Triples, transactions, the write-ahead log. Knows nothing about agents. |
| **Mneme** | Typed memories with provenance and the ontology version they came from. |
| Aegis | Defense of the ontology and of the memory: constraints, detection, recovery. |
| MOIRA, KROMA | Agents, clients of the memory layer. |

</div>

## 🔧 Usage

```python
from mneme import JsonlEngine, MemoryStore, scope_guard

store = MemoryStore(JsonlEngine("memory.jsonl"), guards=[scope_guard])
with store.transaction("kroma.matcher") as tx:                    # all or nothing
    match = tx.put("match", "Metal|MetallicMaterial", {"confidence": 0.94},
                   derived_from=["ontology:materials@3"])
answer = store.put("moira.answerer", "answer", "What is a Metal?", {"answer": "..."},
                   derived_from=[match.id])

store.invalidate(["ontology:materials@3"], reason="rolled back")  # match and answer go stale
store.rollback_to(2)                                              # compensation; history kept
```

<details>
<summary><b>Blackboard</b></summary>

```python
from mneme import Blackboard

class Answerer:
    name = "moira.answerer"
    consumes = frozenset({"question"})

    def handle(self, memory, tx, session):
        tx.put("answer", memory.key, {"answer": "..."}, derived_from=[memory.id])

board = Blackboard(store, [Answerer()])
store.put("user", "question", "What is a Metal?")
board.run()                                    # one step: answer + receipt in one transaction
```

A failing agent writes nothing but an `error` memory in its own scope. A
restarted blackboard re-queues only inputs without a live receipt.

</details>

<details>
<summary><b>Caches</b></summary>

`CacheSync(store, cache, kinds=["answer"])` admits a memory into any `Cache`
when it goes live and evicts it the moment it goes stale, so a cache hit never
serves what the store has invalidated. `DictCache` is the simplest cache;
`MoiraAnswerCache` wraps MOIRA's three-tier query cache.

</details>

<details>
<summary><b>Engines and integrations</b></summary>

| Engine | Where the log lives |
|:--|:--|
| `MemoryEngine` | A list, for tests |
| `JsonlEngine` | One fsynced JSON line per transaction; a torn final line is dropped |
| `OntoDBEngine` | One OntoDB transaction per commit; memories are RDF that SPARQL can query |

| Integration | What it does |
|:--|:--|
| `integrations.aegis` | Every Aegis commit is an `ontology_version` memory; an Aegis rollback invalidates what was built on undone versions; `version_guard` rejects work derived from a stale version |
| `integrations.moira` | MOIRA's cache as a `Cache`, and a front desk that asks through it |
| `integrations.kroma` | KROMA's online refinement as a matching agent on the blackboard |
| `integrations.ontodb` | OntoDB as a storage engine, through its shell |

Each integration imports its system only when used.

</details>

## 🧭 Roadmap

See [`PLAN.md`](PLAN.md) for what runs now and what is next, and
[`docs/design.md`](docs/design.md) for why.

## 📝 Citation

```bibtex
@misc{nguyen2026mneme,
  title  = {Mneme: A Transactional Memory Layer for Multi-Agent Systems},
  author = {Nguyen, Lam},
  year   = {2026},
  url    = {https://github.com/lamng3/mneme}
}
```

## License

[CC BY 4.0](LICENSE). Copyright (c) 2026 Lam Nguyen.
