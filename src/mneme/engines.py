"""Storage engines: where the log lives. The store talks only to `Engine`.

An engine has two jobs: append one transaction's records atomically, and
return every record in log order. It knows nothing about statuses, scopes,
or agents; the store rebuilds those by replay.

- `MemoryEngine`: a list, for tests and short runs.
- `JsonlEngine`: one JSON line per transaction, flushed and fsynced before
  `append` returns. A torn final line (a crash mid-write) is dropped on read,
  the way a database drops a torn final log record.
- `OntoDBEngine` (integrations/ontodb.py): each transaction is one OntoDB
  transaction, and memories are RDF a SPARQL query can read.

A new engine registers itself in `ENGINES`.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .records import Record, canonical

ENGINES: dict[str, Callable[..., Engine]] = {}


def register(name: str) -> Callable[[type], type]:
    def add(cls: type) -> type:
        if name in ENGINES:
            raise ValueError(f"engine {name!r} is already registered")
        ENGINES[name] = cls
        return cls

    return add


def create(name: str, *args: Any, **kwargs: Any) -> Engine:
    try:
        return ENGINES[name](*args, **kwargs)
    except KeyError:
        raise KeyError(f"unknown engine {name!r}; registered: {', '.join(sorted(ENGINES))}") from None


class Engine(ABC):
    @abstractmethod
    def append(self, records: Sequence[Record]) -> None:
        """Write one transaction's records. All of them are durable when this returns, or none are."""

    @abstractmethod
    def records(self) -> list[Record]:
        """Every record, in log order."""


@register("memory")
class MemoryEngine(Engine):
    def __init__(self) -> None:
        self._records: list[Record] = []

    def append(self, records: Sequence[Record]) -> None:
        self._records.extend(records)

    def records(self) -> list[Record]:
        return list(self._records)


@register("jsonl")
class JsonlEngine(Engine):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, records: Sequence[Record]) -> None:
        line = canonical([r.to_json() for r in records]) + "\n"
        with self.path.open("a", encoding="utf-8") as log:
            log.write(line)
            log.flush()
            os.fsync(log.fileno())

    def records(self) -> list[Record]:
        if not self.path.exists():
            return []
        out: list[Record] = []
        lines = self.path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                batch = json.loads(line)
            except json.JSONDecodeError:
                if i == len(lines) - 1:  # a torn final write: the transaction never committed
                    break
                raise
            out.extend(Record.from_json(r) for r in batch)
        return out
