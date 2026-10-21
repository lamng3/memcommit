"""Storage engines: where the log lives. The store talks only to `Engine`.

An engine has two jobs: append one transaction's records atomically, and
return every record in log order. Two more have defaults an engine may speed
up: `records_after(lsn)`, the tail a snapshot or a remote reader needs, and
`rewrite(records)`, which atomically replaces the whole log with a compacted
one (`memcommit.compact`). It knows nothing about statuses, scopes,
or agents; the store rebuilds those by replay.

- `MemoryEngine`: a list, for tests and short runs.
- `JsonlEngine`: one JSON line per transaction, flushed and fsynced before
  `append` returns. A torn final line (a crash mid-write) is dropped on read,
  the way a database drops a torn final log record.
- `SqliteEngine`: one SQLite transaction per commit, in a single file, with
  WAL journaling. Standard library only.

A new engine (a document store, a graph database, a service) subclasses
`Engine` and registers itself in `ENGINES`; `open` picks one by file suffix.
"""

from __future__ import annotations

import json
import os
import sqlite3
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

    def records_after(self, lsn: int) -> list[Record]:
        """Records with an LSN after `lsn`, in log order."""
        return [r for r in self.records() if r.lsn > lsn]

    def rewrite(self, records: Sequence[Record]) -> None:
        """Replace the whole log with `records`, atomically: a reader sees the old log or the new one."""
        raise NotImplementedError(f"{type(self).__name__} cannot rewrite its log")


@register("memory")
class MemoryEngine(Engine):
    def __init__(self) -> None:
        self._records: list[Record] = []

    def append(self, records: Sequence[Record]) -> None:
        self._records.extend(records)

    def records(self) -> list[Record]:
        return list(self._records)

    def rewrite(self, records: Sequence[Record]) -> None:
        self._records = list(records)


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

    def rewrite(self, records: Sequence[Record]) -> None:
        """Write the new log beside the old one, fsync it, and rename it over: atomic on POSIX."""
        batches: dict[int, list[Record]] = {}
        for r in records:
            batches.setdefault(r.txn, []).append(r)
        temp = self.path.with_suffix(self.path.suffix + ".compact")
        with temp.open("w", encoding="utf-8") as log:
            for batch in sorted(batches.values(), key=lambda b: b[0].lsn):
                log.write(canonical([r.to_json() for r in batch]) + "\n")
            log.flush()
            os.fsync(log.fileno())
        os.replace(temp, self.path)


@register("sqlite")
class SqliteEngine(Engine):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS records (lsn INTEGER PRIMARY KEY, txn INTEGER, record TEXT)")
        self._db.commit()

    def append(self, records: Sequence[Record]) -> None:
        with self._db:  # one SQLite transaction: all rows or none
            self._db.executemany(
                "INSERT INTO records (lsn, txn, record) VALUES (?, ?, ?)",
                [(r.lsn, r.txn, canonical(r.to_json())) for r in records],
            )

    def records(self) -> list[Record]:
        return self.records_after(0)

    def records_after(self, lsn: int) -> list[Record]:
        rows = self._db.execute("SELECT record FROM records WHERE lsn > ? ORDER BY lsn", (lsn,))
        return [Record.from_json(json.loads(row[0])) for row in rows]

    def rewrite(self, records: Sequence[Record]) -> None:
        with self._db:  # one SQLite transaction: the old log or the new one
            self._db.execute("DELETE FROM records")
            self._db.executemany(
                "INSERT INTO records (lsn, txn, record) VALUES (?, ?, ?)",
                [(r.lsn, r.txn, canonical(r.to_json())) for r in records],
            )
        self._db.execute("VACUUM")

    def close(self) -> None:
        self._db.close()


def open_engine(path: str | Path) -> Engine:
    """An engine for `path` by suffix: .jsonl is JSONL, .db/.sqlite/.sqlite3 is SQLite."""
    suffix = Path(path).suffix.lower()
    if suffix == ".jsonl":
        return JsonlEngine(path)
    if suffix in (".db", ".sqlite", ".sqlite3"):
        return SqliteEngine(path)
    raise ValueError(f"no engine for {str(path)!r}; use a .jsonl or .db path, or pass an Engine")
