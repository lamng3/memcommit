"""Short-term memory as a cache, the mid-term tier, stateful policies, compaction policies, and recorded workloads."""

from mneme import (
    ARC,
    ARCHIVE,
    LONG,
    LRU,
    MID,
    SHORT,
    STALE,
    AccessLog,
    Candidate,
    Compactor,
    KeepFailed,
    Learned,
    Memory,
    MemoryStore,
    OlderThan,
    TieredMemory,
    UnderPressure,
    query_ref,
    trace,
)
from mneme.bench import Use, Write, from_access_log, replay


def test_touch_is_a_cache_access():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=1)
    a = store.put("x", "note", "a")
    b = store.put("x", "note", "b")  # a is evicted
    long = store.put("x", "fact", "f", tier=LONG)
    tiers.touch([b.id, long.id, a.id])
    assert (tiers.stats.hits, tiers.stats.warm, tiers.stats.misses) == (1, 1, 1)
    assert tiers.stats.page_ins == 1 and store.tier(a.id) == SHORT and store.tier(b.id) == ARCHIVE
    assert tiers.stats.evictions == 2 and round(tiers.stats.hit_rate, 2) == 0.33


def test_evicted_memories_wait_in_mid_term_segments():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=2, mid_capacity=10, auto=False)
    with trace(store, "planner", "one") as t1:
        t1.thought("a")
        t1.thought("b")
    tiers.maintain()
    assert tiers.sizes()[MID] == 2 and tiers.sizes()[SHORT] == 2
    assert set(tiers.segments()) == {t1.id}  # grouped by trace


def test_a_hot_segment_is_promoted_whole():
    store = MemoryStore()
    tiers = TieredMemory(store, capacity=1, mid_capacity=10, heat_threshold=5.0, auto=False)
    with trace(store, "planner", "hot") as t:
        steps = [t.thought(str(i)) for i in range(3)]
    tiers.maintain()
    mid = [c.id for members in tiers.segments().values() for c in members]
    tiers.access.touch(mid * 3)  # used again and again, without paging back in
    done = tiers.maintain()
    assert set(mid) <= set(done.promoted) and all(store.tier(m) == LONG for m in mid)
    assert steps


def test_a_cold_segment_is_consolidated_into_a_summary():
    store = MemoryStore()

    def summarize(name, members):
        return {"count": len(members), "kinds": sorted({m.kind for m in members})}

    tiers = TieredMemory(store, capacity=1, mid_capacity=2, segment_summarizer=summarize, auto=False)
    for task in ("old", "new"):
        with trace(store, "planner", task) as t:
            t.thought("x")
            t.action("tool")
    done = tiers.maintain()
    assert done.consolidated
    summary = store.find(kind="summary")[0]
    assert store.tier(summary.id) == LONG and summary.body["count"] >= 1 and summary.key.startswith("segment:")
    assert all(store.tier(m) == ARCHIVE for m in done.consolidated)
    assert sum(len(v) for v in tiers.segments().values()) <= 2


def cand(key, written, last, count):
    return Candidate(Memory("note", key), SHORT, "live", written, last, count)


def test_arc_learns_from_regretted_evictions():
    arc = ARC()
    once, again = cand("once", 1, 1, 1), cand("again", 2, 2, 3)
    assert arc.victims([once, again], 1, now=5) == [once]  # target 0: once-used goes first
    arc.on_evict([once])
    arc.on_page_in([once])  # it was needed after all: favour recency
    assert arc.target >= 1.0
    assert arc.victims([cand("x", 1, 1, 1), cand("y", 2, 2, 3)], 1, now=5)[0].memory.key == "y"


def test_the_learned_policy_learns_what_gets_reused():
    policy = Learned(horizon=5)
    hot, cold = cand("hot", 1, 9, 5), cand("cold", 1, 1, 1)
    for _ in range(30):
        policy.on_use([hot])
        policy.victims([hot, cold], 1, now=10)
    assert policy.predict(hot, 10) > policy.predict(cold, 10)
    assert policy.victims([hot, cold], 1, now=10) == [cold] and policy.examples > 30


def test_compaction_policies_choose_which_traces_fold():
    store = MemoryStore()
    with trace(store, "planner", "ok run"):
        pass
    try:
        with trace(store, "planner", "failed run"):
            raise RuntimeError("tool down")
    except RuntimeError:
        pass
    compactor = Compactor(store, policy=KeepFailed(records=100))
    assert [s.body["task"] for s in compactor.run()] == ["ok run"]
    assert Compactor(store, policy=OlderThan(records=100)).run() == []
    tiers = TieredMemory(store, capacity=100, auto=False)
    assert Compactor(store, policy=UnderPressure(tiers, fraction=0.9)).run() == []
    tiers.capacity = 2
    assert [s.body["task"] for s in Compactor(store, policy=UnderPressure(tiers)).run()] == ["failed run"]


def test_summaries_of_the_same_task_merge():
    store = MemoryStore()
    compactor = Compactor(store)
    for result in ("AF1", "AF2", "AF3"):
        with trace(store, "planner", "book a flight") as t:
            t.action("search")
            t.result(result)
    compactor.run()
    (merged,) = compactor.merge()
    assert merged.body["runs"] == 3 and merged.body["results"] == ["AF1", "AF2", "AF3"]
    assert merged.body["tools"] == ["search"] and store.tier(merged.id) == LONG
    assert compactor.merge() == []  # merged summaries are not merged again


def test_a_recorded_access_log_replays_as_a_workload():
    store = MemoryStore()
    access = AccessLog(record=True)
    tiers = TieredMemory(store, capacity=2, access=access)
    a = store.put("x", "note", "a")
    b = store.put("x", "note", "b")
    tiers.touch([a.id])
    store.put("x", "note", "c")
    tiers.touch([b.id])
    events = from_access_log(store, access)
    assert events[:4] == [Write(a.id, "note"), Write(b.id, "note"), Use(a.id), Write(events[3].key, "note")]
    assert replay(events, LRU(), capacity=2).uses == 2


def test_a_scoped_read_ignores_memories_the_reader_cannot_see():
    store = MemoryStore()
    read = query_ref("note", topic="t", scopes=["shared", "planner"])
    answer = store.put("planner", "answer", "q", derived_from=[read])
    store.put("booker", "note", "private", {"topic": "t"}, scope="booker")
    assert store.status(answer.id) == "live"
    store.put("someone", "note", "shared", {"topic": "t"})
    assert store.status(answer.id) == STALE
