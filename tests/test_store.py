"""The store: ids, transactions, guards, observers, invalidation, rollback, and replay from every engine."""

import pytest

from memcommit import (
    LIVE,
    RETRACTED,
    STALE,
    JsonlEngine,
    Memory,
    MemoryStore,
    Rejected,
    query_ref,
    scope_guard,
)


def test_a_memory_is_named_by_its_content():
    a = Memory("fact", "Metal", {"is": "Material"}, source="x", derived_from=("b", "a"))
    b = Memory("fact", "Metal", {"is": "Material"}, source="x", derived_from=("a", "b"))
    assert a.id == b.id and a.id.startswith("m:")
    assert a.id != Memory("fact", "Metal", {"is": "Temperature"}, source="x").id
    with pytest.raises(ValueError, match="altered"):
        Memory.from_json({**a.to_json(), "body": {"is": "Temperature"}})


def test_a_transaction_commits_all_or_nothing():
    store = MemoryStore()
    with pytest.raises(RuntimeError), store.transaction("a") as tx:
        tx.put("fact", "one")
        raise RuntimeError("agent crashed")
    assert store.lsn == 0
    with store.transaction("a") as tx:
        one = tx.put("fact", "one")
        two = tx.put("fact", "two", derived_from=[one.id])
    assert store.lsn == 2 and tx.committed.txn == 1
    assert [m.key for m in store.find(kind="fact")] == ["one", "two"]
    assert store.find(derived_from=one.id) == [two]


def test_writing_a_live_memory_again_writes_nothing():
    store = MemoryStore()
    store.put("a", "fact", "x")
    store.put("a", "fact", "x")
    assert store.lsn == 1


def test_invalidation_follows_provenance_transitively():
    store = MemoryStore()
    version = store.put("aegis", "version", "v1")
    answer = store.put("moira", "answer", "q", derived_from=[version.id])
    summary = store.put("moira", "summary", "s", derived_from=[answer.id])
    unrelated = store.put("moira", "answer", "other")
    marked = store.invalidate([version.id], "rolled back")
    assert marked == [version.id, answer.id, summary.id]
    assert store.status(unrelated.id) == LIVE
    assert store.history(summary.id)[-1].reason == "rolled back"


def test_an_outside_reference_invalidates_what_was_built_on_it():
    store = MemoryStore()
    built = store.put("a", "fact", "x", derived_from=["file:sha256:abc"])
    assert store.invalidate(["file:sha256:abc"], "file changed") == [built.id]
    assert store.status(built.id) == STALE


def test_rollback_is_compensation_and_keeps_history():
    store = MemoryStore()
    kept = store.put("a", "fact", "kept")
    mark = store.lsn
    added = store.put("a", "fact", "added")
    with store.transaction("a") as tx:
        tx.retract(kept.id, "wrong")
    records = store.rollback_to(mark)
    assert {(r.op, r.memory.key) for r in records} == {("restore", "kept"), ("retract", "added")}
    assert store.status(kept.id) == LIVE and store.status(added.id) == RETRACTED
    assert len(store.history()) == 5  # nothing erased
    assert store.as_of(mark).status == {kept.id: LIVE}


def test_a_guard_rejects_and_nothing_is_written():
    store = MemoryStore(guards=[scope_guard])
    store.put("kroma", "draft", "x", scope="kroma/drafts")
    with pytest.raises(Rejected, match="moira may not write"):
        store.put("moira", "draft", "y", scope="kroma")
    assert store.lsn == 1


def test_observers_see_each_commit_and_may_not_commit_inside_it():
    store = MemoryStore()
    seen = []
    store.subscribe(lambda commit, state: seen.append([r.op for r in commit.records]))
    m = store.put("a", "fact", "x")
    store.invalidate([m.id], "why")
    assert seen == [["put"], ["stale"]]

    store.subscribe(lambda commit, state: store.put("b", "echo", "loop"))
    with pytest.raises(RuntimeError, match="during a commit"):
        store.put("a", "fact", "y")


def test_a_restarted_store_replays_to_the_same_state(tmp_path):
    engine = JsonlEngine(tmp_path / "log.jsonl")
    store = MemoryStore(engine)
    v = store.put("a", "version", "v1")
    store.put("b", "answer", "q", derived_from=[v.id])
    store.invalidate([v.id], "gone")
    again = MemoryStore(JsonlEngine(tmp_path / "log.jsonl"))
    assert again.state.status == store.state.status
    assert again.put("c", "fact", "z") and again.history()[-1].txn == 4


def test_a_torn_final_write_is_dropped_on_restart(tmp_path):
    path = tmp_path / "log.jsonl"
    store = MemoryStore(JsonlEngine(path))
    store.put("a", "fact", "x")
    with path.open("a") as log:
        log.write('[{"lsn": 2, "txn"')  # the process died mid-write
    again = MemoryStore(JsonlEngine(path))
    assert again.lsn == 1


def test_a_memory_that_changes_a_read_makes_what_was_built_on_it_stale():
    store = MemoryStore()
    read = query_ref("match", source="Ceramic")
    answer = store.put("moira", "answer", "q", {"answer": "Ceramic (no alignment)"}, derived_from=[read])
    summary = store.put("moira", "summary", "s", derived_from=[answer.id])
    other = store.put("kroma", "match", "Metal|MetallicMaterial", {"source": "Metal"})
    assert store.status(answer.id) == LIVE  # a match for another concept does not change the read

    with store.transaction("kroma") as tx:
        match = tx.put("match", "Ceramic|Ceramics", {"source": "Ceramic"})
    assert [r.op for r in tx.committed.records] == ["put", "stale", "stale"]  # one transaction
    assert store.status(answer.id) == STALE and store.status(summary.id) == STALE
    assert store.find(kind="match", where={"source": "Ceramic"}) == [match]
    assert store.status(other.id) == LIVE

    # A memory written in the same transaction as the read is not undone by it.
    with store.transaction("moira") as tx:
        again = tx.put("answer", "q", {"answer": "Ceramic (Ceramics)"}, derived_from=[read, match.id])
        tx.put("match", "Ceramic|Pottery", {"source": "Ceramic"})
    assert store.status(again.id) == LIVE


def test_retracting_a_premise_stales_what_was_built_on_it():
    store = MemoryStore()
    fact = store.put("a", "fact", "x", {"v": 1})
    direct = store.put("b", "answer", "direct", {}, derived_from=[fact.id])
    by_read = store.put("b", "answer", "read", {}, derived_from=[query_ref("fact", "x")])
    downstream = store.put("c", "report", "r", {}, derived_from=[direct.id])
    unrelated = store.put("b", "answer", "other", {})
    before = store.lsn
    with store.transaction("a") as tx:
        tx.retract(fact.id, "wrong")
    assert [store.status(m.id) for m in (direct, by_read, downstream)] == ["stale"] * 3
    assert store.status(unrelated.id) == "live"
    store.rollback_to(before)
    assert all(store.status(m.id) == "live" for m in (fact, direct, by_read, downstream))
