"""An async face on the store, for agent frameworks that run in an event loop.

    store = AsyncMemoryStore(memcommit.open("memory.db"))
    async with store.transaction("planner") as tx:
        tx.put("plan", "trip", {"steps": ["book"]})
    facts = await store.find(kind="fact")

Commits run in a worker thread, one at a time behind an `asyncio.Lock`, so a
slow disk never blocks the loop and two coroutines never commit at once.
Reads of the in-memory state are cheap and run inline. The wrapped store is
still there (`.store`) for anything synchronous: guards, observers, tiers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from .records import Memory, Record
from .store import SYSTEM, MemoryStore, Transaction


class AsyncMemoryStore:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()

    @property
    def lsn(self) -> int:
        return self.store.lsn

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # Reads ---------------------------------------------------------------

    async def get(self, memory_id: str) -> Memory | None:
        return self.store.get(memory_id)

    async def find(self, **filters: Any) -> list[Memory]:
        return self.store.find(**filters)

    async def latest(self, kind: str, key: str) -> Memory | None:
        return self.store.latest(kind, key)

    async def history(self, memory_id: str | None = None) -> list[Record]:
        return await asyncio.to_thread(self.store.history, memory_id)

    # Writes --------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self, agent: str) -> AsyncIterator[Transaction]:
        """Collect writes in the block; commit them in a worker thread when it ends."""
        tx = Transaction(self.store, agent)
        yield tx
        tx.committed = await self._run(self.store._commit, tx)

    async def put(self, agent: str, kind: str, key: str, body: Any = None, **kwargs: Any) -> Memory:
        return await self._run(self.store.put, agent, kind, key, body, **kwargs)

    async def invalidate(self, refs: Iterable[str], reason: str, agent: str = SYSTEM) -> list[str]:
        return await self._run(self.store.invalidate, list(refs), reason, agent)

    async def move(self, memory_ids: Iterable[str], tier: str, reason: str = "", agent: str = SYSTEM) -> list[str]:
        return await self._run(self.store.move, list(memory_ids), tier, reason, agent)

    async def rollback_to(self, lsn: int, reason: str = "", agent: str = SYSTEM) -> list[Record]:
        return await self._run(self.store.rollback_to, lsn, reason, agent)
