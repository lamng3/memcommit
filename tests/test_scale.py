"""Many processes, an event loop, many vectors, ordered agents, and the command line."""

import asyncio
import random

import pytest

import mneme
from mneme import (
    STALE,
    AnnIndex,
    AsyncMemoryStore,
    Blackboard,
    MemoryStore,
    Recall,
    Rejected,
    VectorIndex,
    provenance_guard,
)
from mneme.__main__ import main
from mneme.server import MemoryServer, RemoteStore


@pytest.fixture
def server():
    s = MemoryServer(MemoryStore(guards=[provenance_guard()]), port=0).start()
    yield s
    s.stop()


def test_two_clients_share_one_store_through_the_server(server):
    a = RemoteStore(server.url)
    b = RemoteStore(server.url)
    seen = []
    b.subscribe(lambda commit, state: seen.append(commit.txn))
    fact = a.put("researcher", "fact", "gate", {"gate": 4})
    with a.transaction("writer") as tx:
        brief = tx.put("brief", "gate", {"text": "gate 4"}, derived_from=[fact.id])
    assert tx.committed is not None and server.store.lsn == 2
    b.refresh()
    assert b.get(brief.id) == brief and seen == [1, 2]
    b.invalidate([fact.id], "gate changed")  # LSNs are assigned on the server
    a.refresh()
    assert a.status(brief.id) == STALE and a.lsn == b.lsn == server.store.lsn


def test_the_servers_guards_hold_for_every_client(server):
    client = RemoteStore(server.url)
    with pytest.raises(Rejected, match="does not exist"):
        client.put("x", "plan", "p", derived_from=["m:0000000000000000"])
    assert server.store.lsn == 0 and client.lsn == 0


def test_the_async_store_commits_one_at_a_time():
    async def run():
        store = AsyncMemoryStore(MemoryStore())
        await asyncio.gather(*(store.put("agent", "note", str(i)) for i in range(20)))
        async with store.transaction("agent") as tx:
            tx.put("note", "in a block")
        notes = await store.find(kind="note")
        return store, notes, tx

    store, notes, tx = asyncio.run(run())
    assert len(notes) == 21 and store.lsn == 21 and tx.committed is not None
    assert sorted(r.txn for r in store.store.history()) == list(range(1, 22))


def test_the_ann_index_finds_what_the_exact_index_finds():
    rng = random.Random(3)
    vocabulary = [f"w{i}" for i in range(16)]

    def embed(texts):
        return [[t.split().count(w) + 0.001 for w in vocabulary] for t in texts]

    exact, approx = MemoryStore(), MemoryStore()
    for i in range(200):
        text = " ".join(rng.choices(vocabulary, k=6))
        exact.put("a", "note", str(i), text)
        approx.put("a", "note", str(i), text)
    e = Recall(exact, index=VectorIndex(embed), recency=0, importance=0)
    a = Recall(approx, index=AnnIndex(embed, dimensions=len(vocabulary), tables=6, bits=8), recency=0, importance=0)
    overlap = 0
    for _ in range(10):
        query = " ".join(rng.choices(vocabulary, k=3))
        overlap += len({h.memory.key for h in e.search(query, k=5)} & {h.memory.key for h in a.search(query, k=5)})
    assert overlap >= 35  # at least 70% of the exact top 5, across 10 queries
    approx.invalidate([approx.find(kind="note")[0].id], "gone")
    assert approx.find(kind="note", status=STALE)[0].id not in a.index.vectors


class Producer:
    name = "producer"
    consumes = frozenset({"input"})
    produces = frozenset({"middle"})

    def __init__(self, log):
        self.log = log

    def handle(self, memory, tx, session):
        self.log.append(("producer", memory.key))
        tx.put("middle", memory.key, derived_from=[memory.id])


class Consumer:
    name = "consumer"
    consumes = frozenset({"middle", "input"})

    def __init__(self, log):
        self.log = log

    def handle(self, memory, tx, session):
        self.log.append(("consumer", memory.key))


def test_the_blackboard_runs_upstream_agents_first():
    log = []
    store = MemoryStore()
    board = Blackboard(store, [Consumer(log), Producer(log)])
    assert board.rank == {"consumer": 1, "producer": 0}
    store.put("user", "input", "a")
    store.put("user", "input", "b")
    board.run()
    assert log[:2] == [("producer", "a"), ("producer", "b")]  # both producers before any consumer


def test_the_command_line(tmp_path, capsys):
    path = tmp_path / "memory.db"
    store = mneme.open(path)
    fact = store.put("a", "fact", "f", tier="archive")
    store.invalidate([fact.id], "old")
    assert main(["verify", str(path)]) == 0 and capsys.readouterr().out.strip() == "ok"
    assert main(["stats", str(path)]) == 0 and '"stale": 1' in capsys.readouterr().out
    assert main(["compact", str(path)]) == 0 and "records 2 ->" in capsys.readouterr().out
