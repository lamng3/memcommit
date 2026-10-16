"""Each integration against the real system. A test skips when its system is not on this machine."""

import os
from pathlib import Path

import pytest

from mneme import LIVE, STALE, Blackboard, CacheSync, MemoryStore, Rejected

SOURCE = Path(__file__).resolve().parents[2]
ONTODB = next(
    (p for p in [os.environ.get("ONTODB_BIN", ""), str(SOURCE / "ontodb/build/dev/ontodb")] if p and Path(p).is_file()),
    None,
)
SUB = "subClassOf"


@pytest.mark.skipif(ONTODB is None, reason="no ontodb binary")
def test_ontodb_holds_the_log_across_processes(tmp_path):
    from mneme.integrations.ontodb import OntoDBEngine

    store = MemoryStore(OntoDBEngine(ONTODB, tmp_path / "db"))
    version = store.put("aegis", "version", "v1", {"note": 'quotes " and } braces\nand é'})
    answer = store.put("moira", "answer", "q", {"answer": "Metal, Polymer"}, derived_from=[version.id, "file:x y"])
    store.invalidate([version.id], "rolled back")

    again = MemoryStore(OntoDBEngine(ONTODB, tmp_path / "db"))  # a fresh OntoDB process reads it back
    assert again.state.status == {version.id: STALE, answer.id: STALE}
    assert again.get(version.id).body["note"] == 'quotes " and } braces\nand é'
    assert again.engine.derived_from(version.id) == [answer.id]
    assert again.engine.derived_from("file:x y") == [answer.id]
    assert again.engine.shell.verify().endswith("verify: ok")


def materials():
    from aegis import Ontology

    return Ontology.of(
        ["Material", "Metal", "Polymer", "Temperature"],
        [("Metal", SUB, "Material"), ("Polymer", SUB, "Material")],
    )


def test_aegis_versions_supersede_and_rollback():
    pytest.importorskip("aegis")
    from aegis import VersionedOntology, inject_concept, inject_relation, remove_relation

    from mneme.integrations.aegis import OntologyVersions, version_guard

    store = MemoryStore()
    v = VersionedOntology(materials())
    versions = OntologyVersions(store, "materials")
    store.guard(version_guard(versions))
    versions.watch(v)
    base = versions.ref()
    answer = store.put("moira", "answer", "q", "Metal, Polymer", derived_from=[base])

    v.apply(inject_concept("Ceramic"))
    assert store.status(base) == STALE and store.status(answer.id) == STALE  # superseded
    honest = versions.ref()
    with pytest.raises(Rejected, match="stale"):
        store.put("moira", "answer", "late", "built on the old version", derived_from=[base])

    v.apply_chain([remove_relation("Metal", SUB, "Material"), inject_relation("Metal", SUB, "Temperature")])
    poisoned = store.put("moira", "answer", "q", "Polymer", derived_from=[versions.ref()])
    v.rollback_to(1)
    assert versions.ref() == honest and store.status(honest) == LIVE
    assert store.status(poisoned.id) == STALE


def test_moira_cache_serves_only_live_answers():
    pytest.importorskip("moira.memory")
    from mneme.integrations.moira import MoiraAnswerCache

    store = MemoryStore()
    cache = MoiraAnswerCache()
    sync = CacheSync(store, cache, kinds=["answer"])
    v = store.put("aegis", "version", "v1")
    body = {"question": "What kinds of material are there?", "answer": "Metal, Polymer", "concepts": ["Material"]}
    store.put("moira", "answer", body["question"], body, derived_from=[v.id])
    assert cache.lookup("what kinds of material are there") == "Metal, Polymer"  # MOIRA's normalization
    store.invalidate([v.id], "rolled back")
    assert cache.lookup("What kinds of material are there?") is None
    assert sync.evicted == 1 and cache.rebuilds == 1
    cache.close()


def test_kroma_matcher_refines_and_resumes_from_memory():
    pytest.importorskip("kroma.algorithms.refinement")
    from mneme.integrations.kroma import KromaMatcher, accepted_matches

    graph = {"Material": {"Metal", "Polymer"}, "Substance": {"MetallicMaterial", "Plastic"}}
    verdicts = {("Metal", "MetallicMaterial"): (1, 0.93, True), ("Metal", "Plastic"): (0, 0.88, True)}

    def judge(s, t):
        return verdicts.get((s, t), (0, 0.5, False))

    store = MemoryStore()
    matcher = KromaMatcher(graph, judge)
    board = Blackboard(store, [matcher])
    for s, t in [("Metal", "MetallicMaterial"), ("Metal", "Plastic"), ("Polymer", "Plastic")]:
        store.put("user", "match_request", f"{s}|{t}", {"source": s, "target": t})
    board.run()
    session = board.sessions["kroma.matcher"]
    assert [m.body["target"] for m in accepted_matches(session)] == ["MetallicMaterial"]
    assert store.latest("match", "Polymer|Plastic").body["accepted"] is False
    # Whatever KROMA's refinement decided is recorded with the match...
    same = matcher.block("Metal") == matcher.block("MetallicMaterial")
    assert store.latest("match", "Metal|MetallicMaterial").body["same_class"] is same

    # ...and a restarted matcher rebuilds exactly that state from memory alone.
    restarted = KromaMatcher(graph, judge)
    restarted._replay(session)
    assert restarted.equiv == matcher.equiv
