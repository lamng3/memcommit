"""Tiers, policies, and compaction: bounded working memory whose every move is in the log."""

from mneme import (
    ARCHIVE,
    FIFO,
    LFU,
    LONG,
    LRU,
    RETRACTED,
    SHORT,
    STALE,
    TTL,
    AccessCount,
    AccessLog,
    AllOf,
    AnyOf,
    Candidate,
    Compactor,
    FinishedTraces,
    Forgetting,
    ImportanceAtLeast,
    JsonlEngine,
    KindIn,
    Memory,
    MemoryStore,
    Scored,
    TieredMemory,
    Trace,
    trace,
)


def test_moves_are_records_and_rollback_undoes_them():
    store = MemoryStore()
    note = store.put("a", "note", "n1")
    assert store.tier(note.id) == SHORT
    fact = store.put("a", "fact", "f1", tier=LONG)
    assert store.tier(fact.id) == LONG
    assert store.move([note.id], ARCHIVE, "old") == [note.id]
    assert store.move([note.id], ARCHIVE) == []  # already there: nothing written
    assert store.history(note.id)[-1].op == "move" and store.status(note.id) == "live"
    store.rollback_to(2)
    assert store.tier(note.id) == SHORT


def test_a_stale_memory_keeps_its_tier_and_a_restore_keeps_it_too():
    store = MemoryStore()
    fact = store.put("a", "fact", "f", tier=LONG)
    store.invalidate([fact.id], "outdated")
    assert store.status(fact.id) == STALE and store.tier(fact.id) == LONG
    store.put("a", "fact", "f")  # redone the same: restored where it was
    assert store.tier(fact.id) == LONG


def candidates():
    def c(name, written, last, count, importance=0.0):
        return Candidate(Memory("note", name, importance=importance), SHORT, "live", written, last, count)

    return [c("old", 1, 9, 5), c("cold", 2, 3, 1), c("hot", 3, 10, 9, importance=0.9), c("new", 4, 4, 1)]


def test_eviction_policies_choose_their_victims():
    cs = candidates()
    names = lambda policy, n=1: [c.memory.key for c in policy.victims(cs, n, now=10)]  # noqa: E731
    assert names(FIFO()) == ["old"]
    assert names(LRU()) == ["cold"]
    assert names(LFU(), 2) == ["cold", "new"]
    assert names(Scored()) == ["cold"]
    assert names(Scored(recency=0, frequency=0)) == ["old"]  # importance only; ties by age


def test_promotion_policies_combine():
    cs = candidates()
    keys = lambda policy: {c.memory.key for c in policy.select(cs, now=10)}  # noqa: E731
    assert keys(AccessCount(5)) == {"old", "hot"}
    assert keys(ImportanceAtLeast(0.5)) == {"hot"}
    assert keys(KindIn(["note"])) == {"old", "cold", "hot", "new"}
    assert keys(AllOf(AccessCount(5), ImportanceAtLeast(0.5))) == {"hot"}
    assert keys(AnyOf(ImportanceAtLeast(0.5), AccessCount(5))) == {"old", "hot"}


def test_working_memory_stays_within_capacity_per_scope():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=3, eviction=LRU())
    notes = [store.put("planner", "note", f"n{i}", scope="planner") for i in range(3)]
    tiers.touch([notes[0].id])  # n0 is used again, so n1 is the least recent
    store.put("planner", "note", "n3", scope="planner")
    assert store.tier(notes[1].id) == ARCHIVE
    assert [m.key for m in tiers.working(scopes=["planner"])] == ["n2", "n0", "n3"]
    store.put("booker", "note", "b0", scope="booker")  # another scope has its own room
    assert tiers.sizes() == {SHORT: 4, ARCHIVE: 1}
    assert store.history(notes[1].id)[-1].reason == "evicted from short-term memory"


def test_memories_that_no_longer_hold_are_evicted_first():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=2, eviction=FIFO(), auto=False)
    a, b, c = (store.put("x", "note", k) for k in "abc")
    store.invalidate([c.id], "wrong")
    assert tiers.maintain().evicted == [c.id]
    assert store.tier(a.id) == SHORT and store.tier(b.id) == SHORT


def test_used_memories_are_promoted_and_long_term_can_be_bounded():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=10, promotion=AccessCount(3), long_capacity=1, auto=False)
    a = store.put("x", "fact", "a")
    b = store.put("x", "fact", "b")
    tiers.touch([a.id, a.id, b.id, b.id, b.id])
    done = tiers.maintain()
    assert set(done.promoted) == {a.id, b.id}
    assert done.evicted == [a.id]  # long-term holds one: the least recently used goes
    assert store.tier(b.id) == LONG and store.tier(a.id) == ARCHIVE


def test_the_access_log_survives_a_restart(tmp_path):
    log = AccessLog()
    log.touch(["m:1", "m:1", "m:2"])
    log.save(tmp_path / "access.json")
    again = AccessLog.load(tmp_path / "access.json")
    assert again.now == 3 and again.get("m:1").count == 2
    assert AccessLog.load(tmp_path / "missing.json").now == 0


def test_a_finished_trace_compacts_into_a_long_term_summary():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=100, compactor=Compactor(store))
    with trace(store, "planner", "book a trip") as t:
        t.action("search_flights", {"to": "LYO"})
        obs = t.observation({"flights": 3})
        t.result("AF123")
    summary = tiers.compactor.summary_of(t.id)
    assert summary is not None and store.tier(summary.id) == LONG
    assert summary.body["tools"] == ["search_flights"] and summary.body["results"] == ["AF123"]
    assert summary.body["status"] == "ok" and summary.body["steps"]["action"] == 1
    assert all(store.tier(m.id) == ARCHIVE for m in Trace.open(store, t.id).steps)

    # The summary is built on the steps: a step found wrong takes the summary with it.
    with store.transaction("auditor") as tx:
        tx.retract(obs.id, "the tool lied")
    store.invalidate([obs.id], "retracted")
    assert store.status(obs.id) == RETRACTED and store.status(summary.id) == STALE


def test_a_compaction_policy_can_keep_recent_traces_whole():
    store = MemoryStore()
    compactor = Compactor(
        store, policy=FinishedTraces(keep_recent=1), summarizer=lambda t: f"did {t.root.body['task']}"
    )
    for task in ("one", "two"):
        with trace(store, "planner", task):
            pass
    summaries = compactor.run()
    assert [s.body["content"] for s in summaries] == ["did one"]
    assert compactor.run() == []  # "two" is the newest, kept whole


def test_tier_decisions_replay_from_the_log(tmp_path):
    store = MemoryStore(JsonlEngine(tmp_path / "log.jsonl"))
    TieredMemory(store, capacity=1, eviction=FIFO())
    first = store.put("x", "note", "a")
    store.put("x", "note", "b")
    again = MemoryStore(JsonlEngine(tmp_path / "log.jsonl"))
    assert again.tier(first.id) == ARCHIVE


def test_forgetting_and_ttl_policies():
    cs = candidates()
    keys = lambda policy, n=1: [c.memory.key for c in policy.victims(cs, n, now=10)]  # noqa: E731
    assert keys(Forgetting()) == ["cold"]  # used once, long ago
    assert Forgetting().retention(cs[2], 10) > Forgetting().retention(cs[1], 10)
    assert keys(TTL({"note": 7}), 2) == ["old", "cold"]  # written at ticks 1 and 2: expired
    assert keys(TTL({"other": 1}, fallback=FIFO())) == ["old"]  # nothing expired: the fallback decides


def test_the_benchmark_replays_a_workload_through_the_real_store():
    from mneme.bench import Use, Write, compare, replay, synthetic

    events = [Write("a"), Write("b"), Use("a"), Write("c"), Use("a"), Use("b")]
    lru = replay(events, LRU(), capacity=2)
    fifo = replay(events, FIFO(), capacity=2)
    assert (lru.uses, lru.hits) == (3, 2)  # b was least recently used when c arrived
    assert (fifo.uses, fifo.hits) == (3, 2)  # a was oldest, so its second use missed instead
    assert lru.evicted == 1 and lru.peak_working == 2
    assert synthetic(seed=1) == synthetic(seed=1)
    reports = compare(synthetic(tasks=8, seed=1), {"LRU": lambda: (LRU(), None)}, [4, 8])
    assert [r.capacity for r in reports] == [4, 8] and reports[0].hit_rate <= reports[1].hit_rate
