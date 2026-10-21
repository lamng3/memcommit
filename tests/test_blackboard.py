"""The blackboard: agents coordinate through memories, exactly once, and redo work that went stale."""

from memcommit import ERROR, RECEIPT, Blackboard, CacheSync, DictCache, JsonlEngine, MemoryStore, query_ref, scope_guard


class Doubler:
    name = "doubler"
    consumes = frozenset({"number"})

    def __init__(self):
        self.calls = 0

    def handle(self, memory, tx, session):
        self.calls += 1
        base = session.latest("base", "offset")
        refs = [memory.id] + ([base.id] if base else [])
        offset = base.body if base else 0
        tx.put("doubled", memory.key, memory.body * 2 + offset, derived_from=refs)


class Reporter:
    name = "reporter"
    consumes = frozenset({"doubled"})

    def handle(self, memory, tx, session):
        tx.put("report", memory.key, f"{memory.key} -> {memory.body}", derived_from=[memory.id])


class Broken:
    name = "broken"
    consumes = frozenset({"number"})

    def handle(self, memory, tx, session):
        tx.put("half-done", memory.key)
        raise ValueError("model timed out")


def test_a_chain_of_agents_runs_from_one_input():
    store = MemoryStore(guards=[scope_guard])
    board = Blackboard(store, [Doubler(), Reporter()])
    store.put("user", "number", "a", 21)
    steps = board.run()
    assert [(s.agent, s.error) for s in steps] == [("doubler", ""), ("reporter", "")]
    assert store.latest("report", "a").body == "a -> 42"
    assert len(store.find(kind=RECEIPT)) == 2


def test_work_is_done_once_even_across_a_restart(tmp_path):
    engine = JsonlEngine(tmp_path / "log.jsonl")
    store = MemoryStore(engine)
    doubler = Doubler()
    Blackboard(store, [doubler]).run()
    store.put("user", "number", "a", 1)
    store.put("user", "number", "b", 2)
    first = Blackboard(MemoryStore(JsonlEngine(tmp_path / "log.jsonl")), [doubler])
    first.run(max_steps=1)  # "crash" after one input
    restarted = Blackboard(MemoryStore(JsonlEngine(tmp_path / "log.jsonl")), [doubler])
    assert restarted.pending() == 1
    restarted.run()
    assert doubler.calls == 2


def test_invalidating_an_upstream_fact_redoes_the_work_downstream():
    store = MemoryStore()
    doubler = Doubler()
    board = Blackboard(store, [doubler, Reporter()])
    base = store.put("admin", "base", "offset", 100)
    store.put("user", "number", "a", 1)
    board.run()
    assert store.latest("report", "a").body == "a -> 102"

    store.invalidate([base.id], "offset was wrong")
    assert store.latest("report", "a") is None  # the stale report is not served
    store.put("admin", "base", "offset", 0)
    board.run()
    assert store.latest("report", "a").body == "a -> 2"
    assert doubler.calls == 2


class Labeler:
    """Labels a concept with its alignment, or says it has none: a read whose empty result matters."""

    name = "labeler"
    consumes = frozenset({"concept"})

    def handle(self, memory, tx, session):
        matches = session.find(kind="match", where={"source": memory.key})
        label = ", ".join(m.body["target"] for m in matches) or "unaligned"
        refs = [memory.id, query_ref("match", source=memory.key), *(m.id for m in matches)]
        tx.put("label", memory.key, label, derived_from=refs)


def test_a_new_memory_redoes_work_that_read_its_absence():
    store = MemoryStore()
    board = Blackboard(store, [Labeler()])
    store.put("user", "concept", "Ceramic")
    board.run()
    unaligned = store.latest("label", "Ceramic")
    assert unaligned.body == "unaligned"

    store.put("kroma", "match", "Ceramic|Ceramics", {"source": "Ceramic", "target": "Ceramics"})
    assert store.latest("label", "Ceramic") is None  # the old label is not served
    board.run()
    assert store.latest("label", "Ceramic").body == "Ceramics"

    store.put("kroma", "match", "Metal|MetallicMaterial", {"source": "Metal", "target": "MetallicMaterial"})
    assert board.pending() == 0  # a match for another concept changes nothing


def test_a_failing_agent_writes_nothing_but_an_error():
    store = MemoryStore()
    board = Blackboard(store, [Broken()])
    store.put("user", "number", "a", 1)
    steps = board.run()
    assert "model timed out" in steps[0].error
    assert store.find(kind="half-done") == []
    assert store.find(kind=ERROR)[0].scope == "broken"


def test_a_cache_never_holds_a_stale_memory():
    store = MemoryStore()
    v = store.put("aegis", "version", "v1")
    cache = DictCache()
    CacheSync(store, cache, kinds=["answer"])
    answer = store.put("moira", "answer", "q", "A", derived_from=[v.id])
    assert cache.get("answer", "q") == answer
    store.invalidate([v.id], "rolled back")
    assert cache.get("answer", "q") is None
