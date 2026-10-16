"""MOIRA: its three-tier query cache becomes a cache over the memory store.

MOIRA's `AgentMemory` (a hot question hash, a short-term concept trie, and a
long-term hash of promoted paths) stays MOIRA's: this module uses it as is.
What changes is who owns the truth. Answers are `answer` memories in the
store; `MoiraAnswerCache` is a `Cache`, kept in step by `CacheSync`, so a
MOIRA cache hit can never serve an answer the store has marked stale.

MOIRA's tiers have no delete. Eviction rebuilds them from the answers still
live, which keeps their promotion counts honest about what is still true.

`MoiraFrontDesk` is the read path a user sees: a question goes to the MOIRA
cache first; on a miss it is written as a `question` memory, the blackboard's
agents answer it, and the answer comes back through the cache.

An `answer` memory's body:

    {"question": "...", "answer": "...", "concepts": ["Material", "Metal"],
     "edges": [["Metal", "subClassOf", "Material"]]}
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..blackboard import Blackboard
from ..records import Memory
from ..store import MemoryStore

ANSWER, QUESTION = "answer", "question"
MOIRA_FILES = ("hot.json", "short_term.json", "long_term.json")


def _moira_memory() -> Any:
    try:
        from moira import memory
    except ModuleNotFoundError as err:
        raise ModuleNotFoundError(
            "the MOIRA integration needs moira: pip install moira (or put moira/src on the path)"
        ) from err
    return memory


class MoiraAnswerCache:
    def __init__(self, directory: str | Path | None = None, **sizes: int) -> None:
        moira = _moira_memory()
        self._moira = moira
        self.directory = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="moira-cache-"))
        self.config = moira.MemoryConfig(directory=self.directory, **sizes)
        self.answers: dict[str, Memory] = {}
        self.memory = moira.AgentMemory(self.config)
        self.rebuilds = 0

    def _remember(self, m: Memory) -> None:
        body = m.body
        path = self._moira.QueryPath.from_steps(
            body["question"], body.get("concepts", ()), [tuple(e) for e in body.get("edges", ())]
        )
        self.memory.remember(path, body["answer"])

    # Cache protocol ------------------------------------------------------

    def admit(self, memory: Memory) -> None:
        self.answers[memory.id] = memory
        self._remember(memory)

    def evict(self, memory: Memory) -> None:
        if self.answers.pop(memory.id, None) is None:
            return
        for name in MOIRA_FILES:
            (self.directory / name).unlink(missing_ok=True)
        self.memory = self._moira.AgentMemory(self.config)
        for m in self.answers.values():
            self._remember(m)
        self.rebuilds += 1

    # Reads ---------------------------------------------------------------

    def lookup(self, question: str) -> str | None:
        """MOIRA's hot tier: the cached answer to this question, or a rewording of it."""
        return self.memory.consult_question(question).answer

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


class MoiraFrontDesk:
    """Ask a question: from MOIRA's cache if it holds a live answer, else through the agents."""

    def __init__(self, store: MemoryStore, blackboard: Blackboard, cache: MoiraAnswerCache, user: str = "user") -> None:
        self.store = store
        self.blackboard = blackboard
        self.cache = cache
        self.user = user
        self.hits = 0
        self.misses = 0

    def ask(self, question: str) -> tuple[str | None, str]:
        """The answer, and where it came from: "cache" or "agents"."""
        cached = self.cache.lookup(question)
        if cached is not None:
            self.hits += 1
            return cached, "cache"
        self.misses += 1
        asked = self.store.put(self.user, QUESTION, question, {"question": question})
        self.blackboard.run()
        answers = self.store.find(kind=ANSWER, derived_from=asked.id)
        return (answers[-1].body["answer"] if answers else None), "agents"
