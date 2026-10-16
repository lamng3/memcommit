"""MOIRA, KROMA, OntoDB, and Aegis working together through one memory layer.

Agents never call each other. KROMA's matcher and MOIRA's answerer share a
blackboard in Mneme; MOIRA's three-tier cache sits in front of it; Aegis
versions the ontology and defends it; OntoDB stores both the ontology and the
memory. The story: answer a question, make an honest edit, get attacked, serve
a poisoned answer from cache, detect, roll back, and watch every layer recover.

Run from the mneme repository root, with the other projects cloned beside it:

    uv run python examples/four_systems.py [--ontodb ../ontodb/build/dev/ontodb]

Without an OntoDB binary, the ontology and the memory are kept in JSONL instead.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
for project in ("mneme", "aegis", "moira", "kroma"):  # run from clones without installing
    src = SOURCE / project / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

from aegis import (  # noqa: E402
    Detector,
    JsonlBackend,
    KGQAScorer,
    OntoDBBackend,
    Ontology,
    SubclassAnswerer,
    VersionedOntology,
    Workload,
    WorkloadQuery,
    inject_concept,
    inject_relation,
    remove_relation,
)
from moira.memory import normalize_question  # noqa: E402

from mneme import Blackboard, CacheSync, JsonlEngine, MemoryStore, Rejected, scope_guard  # noqa: E402
from mneme.integrations.aegis import OntologyVersions, version_guard  # noqa: E402
from mneme.integrations.kroma import KromaMatcher, accepted_matches, alignment_read  # noqa: E402
from mneme.integrations.moira import MoiraAnswerCache, MoiraFrontDesk  # noqa: E402

SUB = "subClassOf"
QUESTION = "What kinds of material are there?"
REWORDED = "what kinds of material are there"

# The source ontology Aegis defends, and the target ontology KROMA aligns it to.
MATERIALS = Ontology.of(
    ["Material", "Metal", "Alloy", "Steel", "Polymer", "Temperature", "MeltingPoint"],
    [
        ("Metal", SUB, "Material"),
        ("Alloy", SUB, "Metal"),
        ("Steel", SUB, "Alloy"),
        ("Polymer", SUB, "Material"),
        ("MeltingPoint", SUB, "Temperature"),
    ],
)
MATONTO = {"Substance": {"MetallicMaterial", "Plastic", "Ceramics"}}

# KROMA's language-model debate stands here: (prediction, confidence, accept) per pair.
VERDICTS = {
    ("Metal", "MetallicMaterial"): (1, 0.94, True),
    ("Polymer", "Plastic"): (1, 0.91, True),
    ("Metal", "Plastic"): (0, 0.89, True),
    ("Ceramic", "Ceramics"): (1, 0.96, True),
}

WORKLOAD = Workload(
    [WorkloadQuery(QUESTION, target="Material", gold=frozenset({"Metal", "Alloy", "Steel", "Polymer", "Ceramic"}))]
)


class MoiraAnswerer:
    """A MOIRA agent: answers from the ontology's hierarchy, naming each concept's KROMA alignment."""

    name = "moira.answerer"
    consumes = frozenset({"question"})

    def __init__(self, versioned: VersionedOntology, versions: OntologyVersions) -> None:
        self.versioned = versioned
        self.versions = versions
        self.targets = {normalize_question(q.question): q.target for q in WORKLOAD}

    def handle(self, memory, tx, session) -> None:
        question = memory.body["question"]
        root = self.targets[normalize_question(question)]
        state = self.versioned.state
        kinds = sorted(state.descendants(root) - {root}) if root in state.concepts else []
        parts, used = [], []
        for concept in kinds:
            matches = accepted_matches(session, concept)
            used += [alignment_read(concept), *(m.id for m in matches)]
            aligned = ", ".join(m.body["target"] for m in matches)
            parts.append(f"{concept} (MatOnto: {aligned})" if aligned else concept)
        body = {
            "question": question,
            "answer": "; ".join(parts) or "none",
            "concepts": [root, *kinds],
            "edges": [list(r) for r in sorted(state.relations) if r[1] == SUB and r[0] in kinds],
        }
        tx.put("answer", question, body, derived_from=[memory.id, self.versions.ref(), *used])


def say(step: str) -> None:
    print(f"\n{step}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ontodb", type=Path, default=SOURCE / "ontodb/build/dev/ontodb")
    args = parser.parse_args()
    state_dir = HERE / "demo-state"
    shutil.rmtree(state_dir, ignore_errors=True)
    use_ontodb = args.ontodb.is_file()

    # Storage: OntoDB holds the ontology (through Aegis) and the memory (through Mneme).
    if use_ontodb:
        from mneme.integrations.ontodb import OntoDBEngine

        ontology_store = OntoDBBackend(state_dir / "ontology", binary=args.ontodb)
        memory_engine = OntoDBEngine(args.ontodb, state_dir / "memory-db")
    else:
        ontology_store = JsonlBackend(state_dir / "ontology")
        memory_engine = JsonlEngine(state_dir / "memory.jsonl")
    print("storage:", "OntoDB" if use_ontodb else "JSONL (no ontodb binary found)")
    ontology_store.save_base(MATERIALS)
    v = VersionedOntology.open(ontology_store)
    store = MemoryStore(memory_engine, guards=[scope_guard])

    # Aegis: every version is a memory; the detector watches answer quality.
    versions = OntologyVersions(store, "materials")
    store.guard(version_guard(versions))
    versions.watch(v)
    answer_fn = SubclassAnswerer(WORKLOAD)
    detector = Detector(
        lambda q: KGQAScorer(answer_fn, [(q, WORKLOAD.get(q).gold)]),
        {q.question: q.seeds for q in WORKLOAD},
        threshold=0.05,
        predicates={SUB},
    )
    detector.watch(v)

    # Agents on the blackboard, and MOIRA's cache in front of it.
    def judge(s, t):
        return VERDICTS.get((s, t), (0, 0.5, False))

    matcher = KromaMatcher({**{c: set() for c in MATERIALS.concepts}, **MATONTO}, judge)
    board = Blackboard(store, [matcher, MoiraAnswerer(v, versions)])
    cache = MoiraAnswerCache(state_dir / "moira-cache")
    CacheSync(store, cache, kinds=["answer"])
    desk = MoiraFrontDesk(store, board, cache)

    def ask(question: str) -> str:
        answer, origin = desk.ask(question)
        print(f"   MOIRA ({origin:6}): {answer}")
        return answer

    def request(*pairs: tuple[str, str]) -> None:
        for s, t in pairs:
            store.put("user", "match_request", f"{s}|{t}", {"source": s, "target": t})

    def report(*pairs: tuple[str, str]) -> None:
        for s, t in pairs:
            m = store.latest("match", f"{s}|{t}").body
            verdict = "same" if m["prediction"] else "different"
            print(f"   KROMA: {s} ~ {t}: {verdict} (confidence {m['confidence']})")

    say("1. KROMA aligns the materials ontology with MatOnto")
    pairs = [("Metal", "MetallicMaterial"), ("Polymer", "Plastic"), ("Metal", "Plastic")]
    request(*pairs)
    board.run()
    report(*pairs)

    say("2. Ask MOIRA, then ask again in other words")
    ask(QUESTION)
    ask(REWORDED)

    say("3. An honest edit through Aegis: add Ceramic under Material, and align it")
    v.apply_chain([inject_concept("Ceramic"), inject_relation("Ceramic", SUB, "Material")])
    honest_lsn = v.lsn
    print(f"   version now {versions.current().key}; the old answer went stale with the old version")
    request(("Ceramic", "Ceramics"))
    board.run()  # the answer is redone for the new version, then again once Ceramic has a match
    report(("Ceramic", "Ceramics"))
    honest = ask(QUESTION)
    honest_id = store.latest("answer", QUESTION).id

    say("4. A counterfactual attack: rewire Metal under Temperature, flood in a look-alike")
    v.apply_chain(
        [
            remove_relation("Metal", SUB, "Material"),
            inject_relation("Metal", SUB, "Temperature"),
            inject_concept("Metall"),
            inject_relation("Metall", SUB, "Material"),
        ]
    )
    poisoned_version = versions.ref()
    ask(QUESTION)
    ask(REWORDED)
    print("   the poisoned answer is now in MOIRA's cache")

    say("5. Aegis's detector alarms and rolls the ontology back")
    print(f"   workload harm {detector.harm:.3f} > {detector.threshold}")
    lsn = detector.respond(v)
    print(f"   rolled back to lsn {lsn} (the honest edit was lsn {honest_lsn}); version {versions.current().key}")
    stale = [m for m in store.find(kind="answer", status="stale") if poisoned_version in m.derived_from]
    print(f"   poisoned answers now stale: {len(stale)}; MOIRA cache rebuilds: {cache.rebuilds}")

    say("6. Ask again: the agents redo the work, and the result is the honest memory, restored")
    again = ask(QUESTION)
    same_id = store.latest("answer", QUESTION).id == honest_id
    print(f"   same answer as before the attack: {again == honest}; same memory id: {same_id}")

    say("7. A slow agent tries to commit work built on the poisoned version")
    try:
        store.put("moira.answerer", "answer", "late", {"answer": "Polymer"}, derived_from=[poisoned_version])
    except Rejected as err:
        print(f"   rejected: {err}")

    say("8. The record")
    ops: dict[str, int] = {}
    for r in store.history():
        ops[r.op] = ops.get(r.op, 0) + 1
    print(f"   memory log: {store.lsn} records {ops}; MOIRA cache {desk.hits} hits, {desk.misses} misses")
    if use_ontodb:
        derived = memory_engine.derived_from(poisoned_version)
        print(f"   SPARQL in OntoDB: {len(derived)} memories derived from the poisoned version")
        print(f"   OntoDB memory database: {memory_engine.shell.verify().splitlines()[-1]}")
        print(f"   OntoDB ontology database: {ontology_store.verify().splitlines()[-1]}")
        print(f"   OntoDB's ontology equals Aegis's: {ontology_store.read_state() == v.state}")
    cache.close()


if __name__ == "__main__":
    main()
