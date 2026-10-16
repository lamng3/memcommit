"""The store as a database: log compaction, retention, snapshots, seals, and readers that follow a writer."""

import pytest

from mneme import ARCHIVE, LONG, STALE, JsonlEngine, MemoryStore, SqliteEngine, verify
from mneme.storage import ArchivedOlderThan, AutoSeal, KindsAfter, check_seal, compact, compactions, fold


def busy_store(engine=None, clock=None):
    """A store whose memories have long histories: puts, stale, restore, moves, a retraction."""
    times = iter(range(1, 10_000))
    store = MemoryStore(engine, clock=clock or (lambda: float(next(times))))
    a = store.put("x", "fact", "a", {"v": 1})
    b = store.put("x", "fact", "b", {"v": 2}, derived_from=[a.id])
    c = store.put("x", "note", "c")
    for _ in range(3):
        store.invalidate([a.id], "re-check")
        store.put("x", "fact", "a", {"v": 1})  # restored, and b with it? no: b stays stale until redone
        store.put("x", "fact", "b", {"v": 2}, derived_from=[a.id])
    store.move([a.id], LONG)
    store.move([c.id], ARCHIVE)
    with store.transaction("x") as tx:
        tx.retract(c.id, "wrong")
    return store, a, b, c


@pytest.mark.parametrize("engine", ["memory", "jsonl", "sqlite"])
def test_compaction_folds_the_log_and_replays_to_the_same_state(tmp_path, engine):
    make = {
        "memory": lambda: None,
        "jsonl": lambda: JsonlEngine(tmp_path / "log.jsonl"),
        "sqlite": lambda: SqliteEngine(tmp_path / "log.db"),
    }[engine]
    store, a, _b, _c = busy_store(make())
    before = (dict(store.state.status), dict(store.state.tier))
    report = compact(store)
    assert report.after < report.before and report.purged == []
    assert (store.state.status, store.state.tier) == (
        {**before[0], report.manifest.id: "live", report.seal.id: "live"},
        {**before[1], report.manifest.id: ARCHIVE, report.seal.id: ARCHIVE},
    )
    assert verify(store) == [] and check_seal(store)
    if engine != "memory":  # a fresh process reads the compacted log
        again = MemoryStore(make())
        assert again.state.status == store.state.status and again.tier(a.id) == LONG
    assert compactions(store)[0].body["before"] == report.before


def test_fold_keeps_only_what_replay_needs():
    store, a, b, c = busy_store()
    records = store.engine.records()
    kept = fold(records, store.lsn)
    assert {r.memory.id for r in kept} == {a.id, b.id, c.id}
    by_memory = {}
    for r in kept:
        by_memory.setdefault(r.memory.id, []).append(r.op)
    assert by_memory == {a.id: ["put", "move"], b.id: ["put"], c.id: ["put", "move", "retract"]}


def test_records_after_the_horizon_are_kept_verbatim():
    store, a, _b, _c = busy_store()
    horizon = store.lsn
    store.invalidate([a.id], "after the horizon")
    tail = [r.to_json() for r in store.engine.records() if r.lsn > horizon]
    compact(store, horizon=horizon)
    assert [r.to_json() for r in store.engine.records() if horizon < r.lsn <= horizon + len(tail)] == tail
    assert store.status(a.id) == STALE


def test_retention_purges_old_archived_memories():
    now = {"t": 0.0}
    store = MemoryStore(clock=lambda: now["t"])
    old = store.put("x", "observation", "old", tier=ARCHIVE)
    kept = store.put("x", "summary", "s", derived_from=[old.id], tier=LONG)
    now["t"] = 100.0
    young = store.put("x", "observation", "young", tier=ARCHIVE)
    now["t"] = 150.0
    report = compact(store, retention=ArchivedOlderThan(seconds=120))
    assert report.purged == [old.id]
    assert store.get(old.id) is None and store.get(young.id) is not None and store.get(kept.id) is not None
    assert verify(store) == []  # the summary's premise was purged on purpose, not forged
    assert KindsAfter(["observation"], 10).purge(young, store.state, 150.0)


def test_a_snapshot_opens_without_replaying_the_whole_log(tmp_path):
    engine = JsonlEngine(tmp_path / "log.jsonl")
    store, a, _b, _c = busy_store(engine)
    path = store.save_snapshot(tmp_path / "state.json")
    store.put("x", "fact", "after the snapshot")
    calls = []
    original = engine.records_after
    engine.records_after = lambda lsn: calls.append(lsn) or original(lsn)
    again = MemoryStore(engine, snapshot=path)
    assert calls == [store.lsn - 1]  # only the tail was read
    assert again.state.status == store.state.status and again.state.tier == store.state.tier
    assert again.find(kind="fact", key="b") == store.find(kind="fact", key="b")
    assert again.state.dependents([a.id]) == store.state.dependents([a.id])  # indexes rebuilt


def test_a_reader_follows_another_writer_with_refresh(tmp_path):
    writer = MemoryStore(SqliteEngine(tmp_path / "log.db"))
    reader = MemoryStore(SqliteEngine(tmp_path / "log.db"))
    fact = writer.put("x", "fact", "f")
    assert reader.get(fact.id) is None
    assert [r.memory.id for r in reader.refresh()] == [fact.id] and reader.get(fact.id) == fact


def test_auto_seal_and_check_seal():
    store = MemoryStore()
    AutoSeal(store, every=3)
    for i in range(7):
        store.put("x", "note", str(i))
    seals = store.find(kind="seal")
    assert len(seals) == 3 and check_seal(store)  # seals are records too: 1-3, 5-6, 8-9
    del store.engine._records[0]
    assert not check_seal(store)
