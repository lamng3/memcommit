"""What the store keeps: memories, and the log records that change their status and tier.

A `Memory` is an immutable JSON value: what kind of thing it is (a thought,
an action, an observation, a fact, a summary), what it is about (`key`), its
`body`, the agent that wrote it (`source`), who may read it (`scope`), how much
it matters (`importance`), and what it was derived from (`derived_from`). Its
id is a hash of all of that, so writing the same memory twice names the same
memory, and redoing work that comes out the same restores what was there.

`derived_from` holds references. A reference is a memory id (`m:...`) or any
outside reference a client chooses (`doc:handbook@3`, `file:sha256...`).
Provenance is what makes invalidation work: when a reference goes bad, every
memory derived from it, directly or through other memories, goes stale.

A reference can also name a read: `query_ref("fact", subject="Ceramic")` is
`find:` plus the query, and a memory derived from it was built on what that
query returned, including what it did not return. When a memory that matches
the query goes live, everything derived from the query goes stale, so work
that read "nothing known about Ceramic" is redone once something is known.

Neither status nor tier is stored on a memory. They come from the log: a
`Record` puts, restores, retracts, or marks stale one memory, or moves it
between tiers, and replaying the log in order gives each memory's status and
tier. The store's state is a fold over the log.

Status says whether a memory still holds:

- `live`: it holds.
- `stale`: something it was built on no longer holds; redone work may bring it back.
- `retracted`: it was wrong.

Tier says where a memory sits, which is what retrieval and policies act on:

- `short`: working memory, bounded, where every new memory starts: a cache.
- `mid`: optional, between short and long: memories evicted from the cache,
  grouped into segments (by trace or topic) until a segment is hot enough to
  promote or cold enough to consolidate.
- `long`: consolidated memory, promoted from short- or mid-term by a policy.
- `archive`: evicted from working memory; kept, and searched only on request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

ID_LENGTH = 16
MEMORY_PREFIX = "m:"
QUERY_PREFIX = "find:"

# Statuses a memory can have.
LIVE, STALE, RETRACTED = "live", "stale", "retracted"
STATUSES = (LIVE, STALE, RETRACTED)

# Tiers a memory can sit in.
SHORT, MID, LONG, ARCHIVE = "short", "mid", "long", "archive"
TIERS = (SHORT, MID, LONG, ARCHIVE)

# Record operations, and the status each leaves its memory in. `move` changes only the tier.
OPS: dict[str, str | None] = {"put": LIVE, "restore": LIVE, "stale": STALE, "retract": RETRACTED, "move": None}

SHARED = "shared"


def canonical(value: Any) -> str:
    """Byte-stable JSON, the form ids are hashed from."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_memory_ref(ref: str) -> bool:
    return ref.startswith(MEMORY_PREFIX)


def query_ref(kind: str, key: str | None = None, scopes: Iterable[str] | None = None, **where: Any) -> str:
    """A reference to a read: memories of `kind` (and `key`, in `scopes`) whose body has every field in `where`.

    Pass the reader's `scopes` so a memory it could not see does not invalidate its work.
    """
    query: dict[str, Any] = {"kind": kind}
    if key is not None:
        query["key"] = key
    if scopes is not None:
        query["scopes"] = sorted(set(scopes))
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
        and ("scopes" not in query or memory.scope in query["scopes"])
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
    importance: float = 0.0
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("a memory needs a kind")
        object.__setattr__(self, "derived_from", tuple(sorted(set(self.derived_from))))
        object.__setattr__(self, "importance", float(self.importance))
        canonical(self.body)  # fail now, not at commit, if the body is not JSON
        object.__setattr__(self, "id", MEMORY_PREFIX + self._hash())

    def _hash(self) -> str:
        content = [self.kind, self.key, self.body, self.source, self.scope, list(self.derived_from)]
        if self.importance:
            content.append(self.importance)
        return hashlib.sha256(canonical(content).encode()).hexdigest()[:ID_LENGTH]

    def memory_refs(self) -> list[str]:
        """The memories this one was derived from, without outside references or reads."""
        return [r for r in self.derived_from if is_memory_ref(r)]

    def to_json(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "body": self.body,
            "source": self.source,
            "scope": self.scope,
            "derived_from": list(self.derived_from),
        }
        if self.importance:
            data["importance"] = self.importance
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Memory:
        memory = cls(
            kind=data["kind"],
            key=data["key"],
            body=data.get("body"),
            source=data.get("source", ""),
            scope=data.get("scope", SHARED),
            derived_from=tuple(data.get("derived_from", ())),
            importance=data.get("importance", 0.0),
        )
        if data.get("id") and data["id"] != memory.id:
            raise ValueError(f"memory {data['id']} does not hash to its id ({memory.id}): the body was altered")
        return memory


@dataclass(frozen=True)
class Record:
    """One log entry: an operation on one memory, inside transaction `txn`, by `agent`, at `time`.

    `tier` is the tier a `put` places a new memory in, or the tier a `move` sends it to.
    """

    lsn: int
    txn: int
    op: str
    memory: Memory
    agent: str
    reason: str = ""
    tier: str = ""
    time: float = 0.0

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(f"unknown op {self.op!r}; expected one of {', '.join(OPS)}")
        if self.tier and self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}; expected one of {', '.join(TIERS)}")
        if self.op == "move" and not self.tier:
            raise ValueError("a move needs a tier")

    @property
    def status(self) -> str | None:
        """The status this record leaves its memory in, or None for a move."""
        return OPS[self.op]

    def to_json(self) -> dict[str, Any]:
        data = {
            "lsn": self.lsn,
            "txn": self.txn,
            "op": self.op,
            "agent": self.agent,
            "reason": self.reason,
            "memory": self.memory.to_json(),
        }
        if self.tier:
            data["tier"] = self.tier
        if self.time:
            data["time"] = self.time
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Record:
        return cls(
            lsn=data["lsn"],
            txn=data["txn"],
            op=data["op"],
            memory=Memory.from_json(data["memory"]),
            agent=data.get("agent", ""),
            reason=data.get("reason", ""),
            tier=data.get("tier", ""),
            time=data.get("time", 0.0),
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
