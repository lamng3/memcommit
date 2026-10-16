"""What the store keeps: memories, and the log records that change their status.

A `Memory` is an immutable value: what kind of thing it is, what it is about
(`key`), its JSON `body`, the agent that wrote it (`source`), who may read it
(`scope`), and what it was derived from (`derived_from`). Its id is a hash of
all of that, so writing the same memory twice names the same memory.

`derived_from` holds references. A reference is a memory id (`m:...`) or any
outside reference a client chooses (`ontology:materials@3`, `file:sha256...`).
Provenance is what makes invalidation work: when a reference goes bad, every
memory derived from it, directly or through other memories, goes stale.

A reference can also name a read: `query_ref("match", source="Ceramic")` is
`find:` plus the query, and a memory derived from it was built on what that
query returned, including what it did not return. When a memory that matches
the query goes live, everything derived from the query goes stale, so work
that read "no match for Ceramic" is redone once there is one.

Status is never stored on a memory. It comes from the log: a `Record` puts,
restores, retracts, or marks stale one memory, and replaying the log in order
gives each memory's status. That is the event-sourcing half of the design;
the store's state is a fold over the log.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ID_LENGTH = 16
MEMORY_PREFIX = "m:"
QUERY_PREFIX = "find:"

# Statuses a memory can have.
LIVE, STALE, RETRACTED = "live", "stale", "retracted"
STATUSES = (LIVE, STALE, RETRACTED)

# Record operations, and the status each leaves its memory in.
OPS = {"put": LIVE, "restore": LIVE, "stale": STALE, "retract": RETRACTED}

SHARED = "shared"


def canonical(value: Any) -> str:
    """Byte-stable JSON, the form ids are hashed from."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_memory_ref(ref: str) -> bool:
    return ref.startswith(MEMORY_PREFIX)


def query_ref(kind: str, key: str | None = None, **where: Any) -> str:
    """A reference to a read: memories of `kind` (and `key`) whose body has every field in `where`."""
    query: dict[str, Any] = {"kind": kind}
    if key is not None:
        query["key"] = key
    if where:
        query["where"] = where
    return QUERY_PREFIX + canonical(query)


def parse_query(ref: str) -> dict[str, Any] | None:
    """The query a `query_ref` names, or None for any other reference."""
    return json.loads(ref[len(QUERY_PREFIX) :]) if ref.startswith(QUERY_PREFIX) else None


def body_has(body: Any, where: dict[str, Any] | None) -> bool:
    """True if `body` is a dict with every field in `where`, or `where` is empty."""
    return not where or (isinstance(body, dict) and all(body.get(f) == v for f, v in where.items()))


def matches(query: dict[str, Any], memory: Memory) -> bool:
    """True if `memory` is in the result of the read `query` names."""
    return (
        memory.kind == query["kind"]
        and query.get("key", memory.key) == memory.key
        and body_has(memory.body, query.get("where"))
    )


@dataclass(frozen=True)
class Memory:
    kind: str
    key: str
    body: Any = None
    source: str = ""
    scope: str = SHARED
    derived_from: tuple[str, ...] = ()
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("a memory needs a kind")
        object.__setattr__(self, "derived_from", tuple(sorted(set(self.derived_from))))
        canonical(self.body)  # fail now, not at commit, if the body is not JSON
        object.__setattr__(self, "id", MEMORY_PREFIX + self._hash())

    def _hash(self) -> str:
        content = [self.kind, self.key, self.body, self.source, self.scope, list(self.derived_from)]
        return hashlib.sha256(canonical(content).encode()).hexdigest()[:ID_LENGTH]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "body": self.body,
            "source": self.source,
            "scope": self.scope,
            "derived_from": list(self.derived_from),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Memory:
        memory = cls(
            kind=data["kind"],
            key=data["key"],
            body=data.get("body"),
            source=data.get("source", ""),
            scope=data.get("scope", SHARED),
            derived_from=tuple(data.get("derived_from", ())),
        )
        if data.get("id") and data["id"] != memory.id:
            raise ValueError(f"memory {data['id']} does not hash to its id ({memory.id}): the body was altered")
        return memory


@dataclass(frozen=True)
class Record:
    """One log entry: an operation on one memory, inside transaction `txn`, by `agent`."""

    lsn: int
    txn: int
    op: str
    memory: Memory
    agent: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(f"unknown op {self.op!r}; expected one of {', '.join(OPS)}")

    @property
    def status(self) -> str:
        return OPS[self.op]

    def to_json(self) -> dict[str, Any]:
        return {
            "lsn": self.lsn,
            "txn": self.txn,
            "op": self.op,
            "agent": self.agent,
            "reason": self.reason,
            "memory": self.memory.to_json(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Record:
        return cls(
            lsn=data["lsn"],
            txn=data["txn"],
            op=data["op"],
            memory=Memory.from_json(data["memory"]),
            agent=data.get("agent", ""),
            reason=data.get("reason", ""),
        )


@dataclass(frozen=True)
class Commit:
    """The records one transaction wrote, as observers and guards see them."""

    txn: int
    agent: str
    records: tuple[Record, ...]

    @property
    def lsn(self) -> int:
        return self.records[-1].lsn if self.records else 0

    def puts(self) -> list[Memory]:
        return [r.memory for r in self.records if r.op == "put"]
