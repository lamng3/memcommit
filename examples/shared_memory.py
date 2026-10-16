"""Two agents share one memory: traces, a blackboard, tiers, recall, compaction, and recovery from a poisoned tool.

A researcher answers questions by calling a lookup tool and writes a `finding`;
a writer turns each finding into a `brief`. They never call each other: the
blackboard hands each one the memories it consumes. Every run is a trace.
Then the lookup tool is compromised, a poisoned finding spreads into a brief,
and a defender quarantines it; the agents redo the work from what still holds.

    python examples/shared_memory.py [--store memory.db]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a clone without installing

import mneme  # noqa: E402
from mneme import (  # noqa: E402
    LRU,
    AccessCount,
    Blackboard,
    Compactor,
    Recall,
    TieredMemory,
    Trace,
    blast_radius,
    provenance_guard,
    query_ref,
    quarantine,
    scope_guard,
    verify,
)

FACTS = {
    "boiling point of water": "100 C at sea level",
    "speed of light": "299,792 km/s",
    "tallest mountain": "Everest, 8,849 m",
}
TOOL = {"compromised": False}


def lookup(topic: str) -> str:
    """The researcher's tool. When compromised, it lies about one topic."""
    if TOOL["compromised"] and topic == "speed of light":
        return "about 30 km/s"
    return FACTS.get(topic, "unknown")


class Researcher:
    name = "researcher"
    consumes = frozenset({"question"})

    def handle(self, question, tx, session):
        topic = question.body["topic"]
        with session.trace(f"answer: {topic}", derived_from=[question.id]) as t:
            t.thought(f"I should look up {topic}")
            call = t.action("lookup", {"topic": topic})
            answer = lookup(topic)
            seen = t.observation(answer, parent=call, importance=0.5)
        tx.put("finding", topic, {"topic": topic, "answer": answer}, derived_from=[question.id, seen.id])


class Writer:
    name = "writer"
    consumes = frozenset({"finding"})

    def handle(self, finding, tx, session):
        topic = finding.body["topic"]
        notes = session.find(kind="note", where={"topic": topic})  # a read: a later note redoes this brief
        extra = "".join(f" ({n.body['text']})" for n in notes)
        text = f"{topic.capitalize()}: {finding.body['answer']}{extra}."
        refs = [finding.id, query_ref("note", topic=topic), *(n.id for n in notes)]
        tx.put("brief", topic, {"topic": topic, "text": text}, derived_from=refs, importance=0.8)


def show_briefs(store) -> None:
    for brief in store.find(kind="brief"):
        print(f"   {brief.body['text']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, help="a .db or .jsonl file; default: a temporary one")
    args = parser.parse_args()
    path = args.store or Path(tempfile.mkdtemp(prefix="mneme-")) / "memory.db"

    store = mneme.open(path, guards=[scope_guard, provenance_guard()])
    tiers = TieredMemory(store, capacity=12, eviction=LRU(), promotion=AccessCount(3), compactor=Compactor(store))
    recall = Recall(store, tiers=tiers)
    board = Blackboard(store, [Researcher(), Writer()])
    print(f"memory: {path}")

    print("\n1. Ask three questions; the agents answer through the shared memory")
    for topic in FACTS:
        store.put("user", "question", topic, {"topic": topic})
    steps = board.run()
    print(f"   {len(steps)} agent steps, {len(store.find(kind='trace'))} traces")
    show_briefs(store)

    print("\n2. Working memory stays bounded; finished traces become long-term summaries")
    print(f"   tiers: {tiers.sizes()}")
    summary = store.find(kind="summary")[0]
    print(f"   a summary: {summary.body['task']} -> tools {summary.body['tools']}, status {summary.body['status']}")

    print("\n3. Recall: what do we know about light?")
    for hit in recall.search("how fast is light", k=2):
        print(f"   {hit.score:.2f} {hit.memory.kind:8} {hit.memory.key}")

    print("\n4. A note arrives later; the brief that read 'no notes' is redone")
    store.put("user", "note", "light-note", {"topic": "speed of light", "text": "in a vacuum"})
    board.run()
    print(f"   {store.latest('brief', 'speed of light').body['text']}")

    print("\n5. The lookup tool is compromised, and a re-asked question picks up the lie")
    TOOL["compromised"] = True
    store.invalidate([store.latest("finding", "speed of light").id], "re-check")
    board.run()
    poisoned = store.latest("finding", "speed of light")
    print(f"   {store.latest('brief', 'speed of light').body['text']}")

    print("\n6. A defender traces the damage and quarantines the poisoned observation")
    observation = next(r for r in poisoned.derived_from if store.get(r) and store.get(r).kind == "observation")
    radius = blast_radius(store, [observation])
    print(f"   built on the poisoned observation: {[store.get(m).kind for m in radius]}")
    TOOL["compromised"] = False
    quarantine(store, [observation], "the lookup tool was compromised")
    board.run()
    print(f"   {store.latest('brief', 'speed of light').body['text']}")

    print("\n7. The record")
    ops: dict[str, int] = {}
    for r in store.history():
        ops[r.op] = ops.get(r.op, 0) + 1
    print(f"   {store.lsn} log records {ops}")
    print(f"   verify: {verify(store) or 'ok'}")
    last = store.find(kind="trace")[-1]
    print(f"   last trace: {Trace.open(store, last.id).to_json()['task']}")


if __name__ == "__main__":
    main()
