"""Recall finds what matters and never what went stale; audit finds what a tamperer left behind."""

import pytest

from mneme import (
    ARCHIVE,
    LONG,
    RETRACTED,
    STALE,
    Memory,
    MemoryStore,
    Recall,
    Rejected,
    SqliteEngine,
    TieredMemory,
    VectorIndex,
    blast_radius,
    diff,
    lineage,
    open,
    provenance_guard,
    quarantine,
    seal,
    verify,
)


def test_recall_ranks_by_relevance_recency_and_importance():
    store = MemoryStore()
    tiers = TieredMemory(store, auto=False)
    recall = Recall(store, tiers=tiers)
    lyon = store.put("planner", "fact", "lyon", {"text": "flights to Lyon leave from gate 4"})
    store.put("planner", "fact", "paris", {"text": "flights to Paris are delayed"})
    key = store.put("planner", "fact", "visa", {"text": "a visa is needed for Lyon"}, importance=1.0)
    hits = recall.search("Lyon flights", k=2)
    assert [h.memory for h in hits] == [lyon, key]
    assert tiers.candidate(lyon.id).count == 2  # written, then recalled
    assert recall.search("Lyon", k=5, kinds=["nothing"]) == []


def test_recall_never_returns_a_stale_memory_and_skips_the_archive_by_default():
    store = MemoryStore()
    recall = Recall(store)
    old = store.put("a", "fact", "gate", {"text": "gate 4"})
    store.invalidate([old.id], "gate changed")
    assert recall.search("gate") == []
    new = store.put("a", "fact", "gate", {"text": "gate 7"})
    store.move([new.id], ARCHIVE)
    assert recall.search("gate") == []
    assert [h.memory for h in recall.search("gate", tiers=None)] == [new]


def test_a_vector_index_takes_any_embedder():
    def embed(texts):  # a toy embedder: counts of two words
        return [[t.lower().count("cat") + 0.01, t.lower().count("dog") + 0.01] for t in texts]

    store = MemoryStore()
    recall = Recall(store, index=VectorIndex(embed), recency=0, importance=0)
    store.put("a", "note", "1", "a dog barked")
    cat = store.put("a", "note", "2", "the cat sat")
    assert recall.search("cat", k=1)[0].memory == cat


def test_verify_finds_a_tampered_body_and_forged_provenance():
    store = MemoryStore()
    fact = store.put("planner", "fact", "gate", {"gate": 4})
    assert verify(store) == []
    object.__setattr__(store.engine._records[0].memory, "body", {"gate": 13})  # edit the log in place
    assert any("does not hash" in p for p in verify(store))

    forged = MemoryStore()
    forged.put("planner", "note", "x", derived_from=["m:doesnotexist"])
    assert any("unknown memories" in p for p in verify(forged))
    assert fact.id.startswith("m:")


def test_lineage_blast_radius_and_quarantine():
    store = MemoryStore()
    obs = store.put("tool", "observation", "o", {"price": 1})
    plan = store.put("planner", "plan", "p", derived_from=[obs.id])
    answer = store.put("planner", "answer", "a", derived_from=[plan.id])
    store.invalidate([answer.id], "re-asked")
    assert lineage(store, answer.id) == [obs.id, plan.id]
    assert blast_radius(store, [obs.id]) == [plan.id, answer.id]  # stale ones too
    lsn = store.lsn
    records = quarantine(store, [obs.id], "the tool was compromised")
    assert {r.op for r in records} == {"retract", "stale"}
    assert store.status(obs.id) == RETRACTED and store.status(plan.id) == STALE
    changed = {c.memory.id: c.after for c in diff(store, lsn)}
    assert changed == {obs.id: (RETRACTED, "short"), plan.id: (STALE, "short")}


def test_the_provenance_guard_rejects_forged_and_stale_premises():
    store = MemoryStore(guards=[provenance_guard()])
    real = store.put("a", "fact", "f")
    with pytest.raises(Rejected, match="does not exist"):
        store.put("b", "plan", "p", derived_from=["m:forged0000000000"])
    store.invalidate([real.id], "outdated")
    with pytest.raises(Rejected, match="stale"):
        store.put("b", "plan", "p", derived_from=[real.id])
    with store.transaction("b") as tx:  # premises written in the same transaction are fine
        new = tx.put("fact", "g")
        tx.put("plan", "q", derived_from=[new.id])
    assert store.lsn == 4


def test_sqlite_and_open_pick_engines_by_suffix(tmp_path):
    store = open(tmp_path / "memory.db")
    assert isinstance(store.engine, SqliteEngine)
    fact = store.put("a", "fact", "f", {"x": 1}, tier=LONG)
    store.invalidate([fact.id], "outdated")
    again = open(tmp_path / "memory.db")
    assert again.status(fact.id) == STALE and again.tier(fact.id) == LONG
    assert again.get(fact.id) == Memory("fact", "f", {"x": 1}, source="a")
    with pytest.raises(ValueError, match="no engine"):
        open(tmp_path / "memory.txt")


def test_a_seal_catches_a_deleted_record():
    store = MemoryStore()
    store.put("a", "fact", "one")
    store.put("a", "fact", "two")
    sealed = seal(store)
    assert verify(store) == [] and sealed.body["lsn"] == 2 and store.tier(sealed.id) == ARCHIVE
    del store.engine._records[1]  # an attacker drops a record from the log
    assert any("no longer matches its seal" in p for p in verify(store))
