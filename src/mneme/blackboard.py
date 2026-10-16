"""A blackboard: agents that coordinate only through the memory store.

Each agent names the kinds of memory it consumes. When a memory of such a
kind goes live, the blackboard queues it for that agent, and `run` hands it
over: the agent reads what it needs through its session and writes its
results in one transaction. No agent calls another. A multi-agent workflow is
the set of kinds the agents produce and consume.

Exactly once, durably. With an agent's results the blackboard writes a
`receipt` in the same transaction, in the agent's scope, derived from the
input and from every result. The receipt is the record that the work is
done, so a restarted blackboard re-queues only inputs without a live receipt.

Redo on invalidation. Because the receipt is derived from the results, any
invalidation that makes a result stale makes the receipt stale too, and the
blackboard queues the input again. When an upstream fact is rolled back, the
agents that built on it redo their work against what holds now.

Upstream first. An agent may also name the kinds it `produces`. Then the
queue runs agents in dependency order (a producer before its consumers, FIFO
within a level), so a downstream agent does not do work that an upstream
agent is about to make stale. Agents that name nothing run in FIFO order.

A failing agent writes nothing: its transaction is dropped, and the failure
is written as an `error` memory in the agent's scope. The input is not
retried until something new happens to it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .records import ARCHIVE, LIVE, Commit, Memory
from .session import AgentSession
from .state import State
from .store import MemoryStore, Transaction

RECEIPT = "receipt"
ERROR = "error"


class Agent(Protocol):
    name: str
    consumes: frozenset[str]

    def handle(self, memory: Memory, tx: Transaction, session: AgentSession) -> None:
        """Do the work for one input memory, writing results through `tx`."""


@dataclass
class Step:
    agent: str
    input: str
    outputs: list[str]
    error: str = ""


def receipt_key(agent: str, memory_id: str) -> str:
    return f"{agent}:{memory_id}"


class Blackboard:
    def __init__(self, store: MemoryStore, agents: Iterable[Agent]) -> None:
        self.store = store
        self.agents = {a.name: a for a in agents}
        self.sessions = {name: AgentSession(store, name) for name in self.agents}
        self.queue: deque[tuple[str, str]] = deque()
        self.rank = self._ranks()
        self._queued: set[tuple[str, str]] = set()
        store.subscribe(self._observe)
        self._backfill()

    def _ranks(self) -> dict[str, int]:
        """Each agent's depth in the produces -> consumes graph: producers 0, their consumers 1, and so on."""
        produces = {name: frozenset(getattr(a, "produces", ())) for name, a in self.agents.items()}
        rank = dict.fromkeys(self.agents, 0)
        for _ in range(len(self.agents)):  # longest path, at most n rounds; a cycle stops growing it
            changed = False
            for name, agent in self.agents.items():
                for upstream, kinds in produces.items():
                    if upstream != name and kinds & agent.consumes and rank[name] < rank[upstream] + 1:
                        rank[name] = min(rank[upstream] + 1, len(self.agents))
                        changed = True
            if not changed:
                break
        return rank

    def _next(self) -> tuple[str, str]:
        """The queued item whose agent is furthest upstream, oldest first."""
        best = min(range(len(self.queue)), key=lambda i: (self.rank[self.queue[i][0]], i))
        item = self.queue[best]
        del self.queue[best]
        return item

    def _consumers(self, kind: str) -> list[str]:
        return [name for name, a in self.agents.items() if kind in a.consumes]

    def _enqueue(self, agent: str, memory_id: str) -> None:
        item = (agent, memory_id)
        if item not in self._queued:
            self._queued.add(item)
            self.queue.append(item)

    def _done(self, agent: str, memory_id: str) -> bool:
        return self.store.latest(RECEIPT, receipt_key(agent, memory_id)) is not None

    def _backfill(self) -> None:
        """Queue every live input without a live receipt, oldest first: resume after a restart."""
        for memory in self.store.find():
            for agent in self._consumers(memory.kind):
                if not self._done(agent, memory.id):
                    self._enqueue(agent, memory.id)

    def _observe(self, commit: Commit, state: State) -> None:
        for record in commit.records:
            m = record.memory
            if record.op == "move":
                continue
            if record.status == LIVE:
                for agent in self._consumers(m.kind):
                    self._enqueue(agent, m.id)
            elif m.kind == RECEIPT and isinstance(m.body, dict):
                agent, memory_id = m.body["agent"], m.body["input"]
                if state.is_live(memory_id):  # the work went stale while the input still holds: redo it
                    self._enqueue(agent, memory_id)

    def pending(self) -> int:
        return len(self.queue)

    def run(self, max_steps: int = 1000) -> list[Step]:
        """Hand queued inputs to their agents until the queue is empty or `max_steps` is reached."""
        steps: list[Step] = []
        while self.queue and len(steps) < max_steps:
            item = self._next()
            self._queued.discard(item)
            agent_name, memory_id = item
            if not self.store.state.is_live(memory_id) or self._done(agent_name, memory_id):
                continue
            steps.append(self._step(self.agents[agent_name], self.store.get(memory_id)))
        return steps

    def _step(self, agent: Agent, memory: Memory) -> Step:
        session = self.sessions[agent.name]
        try:
            with session.transaction() as tx:
                agent.handle(memory, tx, session)
                outputs = [m.id for op, m, _, _ in tx.ops if op == "put"]
                tx.put(
                    RECEIPT,
                    receipt_key(agent.name, memory.id),
                    {"agent": agent.name, "input": memory.id},
                    scope=agent.name,
                    derived_from=[memory.id, *outputs],
                    tier=ARCHIVE,  # bookkeeping: never takes room in working memory
                )
        except Exception as err:  # an agent's failure must not stop the others
            session.put(ERROR, receipt_key(agent.name, memory.id), {"error": repr(err)}, scope=agent.name)
            return Step(agent.name, memory.id, [], repr(err))
        return Step(agent.name, memory.id, outputs)
