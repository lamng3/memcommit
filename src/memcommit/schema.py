"""A declared memory structure: which kinds of memory exist, and what each must look like.

    schema = Schema(
        Kind("fact", body={"type": "object", "required": ["subject", "value"]}, tier="long"),
        Kind("answer", requires=("question",), scopes=("agent",)),
        strict=True,
    )
    schema.attach(store)                       # a guard rejects writes that do not conform
    schema.put(store, "researcher", "fact", "light", {"subject": "light", "value": "299,792 km/s"})

A `Kind` says, for one kind of memory:

- `body`: a JSON schema for the body (a small subset: `type`, `properties`,
  `required`, `additionalProperties`, `items`, `enum`, `minimum`, `maximum`,
  `minLength`, `maxLength`, `minItems`, `maxItems`).
- `scopes`: where it may be written: `shared`, `agent` (the writer's own
  scope or one under it), or any scope name. None allows any scope.
- `requires`: kinds it must be derived from. A memory of this kind must list
  at least one memory of each required kind in `derived_from`, so structure
  and provenance are declared together.
- `tier` and `importance`: defaults that `Schema.put` fills in.

`strict=True` rejects kinds the schema does not declare. The kinds MemCommit
writes itself (traces and their steps, summaries, receipts, seals, the
compaction manifest, the schema) are always allowed.

The schema is itself a memory: `attach` writes it to the log as a `schema`
memory, and a changed schema marks the previous one stale, so `as_of(lsn)`
shows which structure held when. `Schema.load(store)` reads the live one back.
The core knows no domain: a project names its own kinds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .audit import SEAL
from .blackboard import RECEIPT
from .compaction import SUMMARY
from .records import SHARED, SHORT, TIERS, Commit, Memory, canonical
from .session import owns
from .state import State
from .storage import COMPACTION
from .store import SYSTEM, MemoryStore
from .traces import STEP_KINDS, TRACE

SCHEMA = "schema"
BUILTIN = frozenset({SCHEMA, SUMMARY, RECEIPT, SEAL, COMPACTION, TRACE, *STEP_KINDS})
AGENT = "agent"

_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def check_body(value: Any, schema: dict[str, Any], path: str = "body") -> list[str]:
    """Problems with `value` against the JSON-schema subset above. Empty means it conforms."""
    problems: list[str] = []
    wanted = schema.get("type")
    if wanted is not None:
        names = [wanted] if isinstance(wanted, str) else list(wanted)
        if not any(_is(value, n) for n in names):
            return [f"{path} must be {' or '.join(names)}, not {_name(value)}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        for field in schema.get("required", ()):
            if field not in value:
                problems.append(f"{path}.{field} is required")
        properties = schema.get("properties", {})
        for field, item in value.items():
            if field in properties:
                problems += check_body(item, properties[field], f"{path}.{field}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}.{field} is not allowed")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{path} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path} allows at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                problems += check_body(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{path} needs at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path} allows at most {schema['maxLength']} characters")
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path} must be at most {schema['maximum']}")
    return problems


def _is(value: Any, name: str) -> bool:
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name]) and not (name != "boolean" and isinstance(value, bool))


def _name(value: Any) -> str:
    for name in (*_TYPES, "integer", "number"):
        if _is(value, name):
            return name
    return type(value).__name__


@dataclass(frozen=True)
class Kind:
    name: str
    description: str = ""
    body: dict[str, Any] | None = None
    scopes: tuple[str, ...] | None = None
    requires: tuple[str, ...] = ()
    tier: str = SHORT
    importance: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a kind needs a name")
        if self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}; expected one of {', '.join(TIERS)}")
        object.__setattr__(self, "requires", tuple(self.requires))
        if self.scopes is not None:
            object.__setattr__(self, "scopes", tuple(self.scopes))

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        if self.description:
            data["description"] = self.description
        if self.body is not None:
            data["body"] = self.body
        if self.scopes is not None:
            data["scopes"] = list(self.scopes)
        if self.requires:
            data["requires"] = list(self.requires)
        if self.tier != SHORT:
            data["tier"] = self.tier
        if self.importance:
            data["importance"] = self.importance
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Kind:
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            body=data.get("body"),
            scopes=tuple(data["scopes"]) if "scopes" in data else None,
            requires=tuple(data.get("requires", ())),
            tier=data.get("tier", SHORT),
            importance=data.get("importance", 0.0),
        )

    def problems(self, memory: Memory, kinds_of: dict[str, str]) -> list[str]:
        """What is wrong with `memory` as a memory of this kind. `kinds_of` maps memory ids to their kinds."""
        out = []
        if self.body is not None:
            out += check_body(memory.body, self.body)
        if self.scopes is not None and not self._allows(memory):
            out.append(f"scope {memory.scope!r} is not one of {list(self.scopes)}")
        derived = {kinds_of.get(r) for r in memory.memory_refs()}
        out += [f"must be derived from a {k}" for k in self.requires if k not in derived]
        return out

    def _allows(self, memory: Memory) -> bool:
        assert self.scopes is not None
        return any(
            (memory.scope == SHARED)
            if s == SHARED
            else owns(memory.source, memory.scope)
            if s == AGENT
            else memory.scope == s
            for s in self.scopes
        )


class Schema:
    def __init__(self, *kinds: Kind, strict: bool = False, name: str = "memory") -> None:
        self.kinds = {k.name: k for k in kinds}
        if len(self.kinds) != len(kinds):
            raise ValueError("two kinds share a name")
        self.strict = strict
        self.name = name
        for kind in kinds:
            missing = [r for r in kind.requires if r not in self.kinds and r not in BUILTIN]
            if missing:
                raise ValueError(f"kind {kind.name!r} requires undeclared kinds: {', '.join(missing)}")

    def __contains__(self, name: str) -> bool:
        return name in self.kinds

    def kind(self, name: str) -> Kind:
        return self.kinds[name]

    @property
    def version(self) -> str:
        return hashlib.sha256(canonical(self.to_json()).encode()).hexdigest()[:8]

    # Checking ------------------------------------------------------------

    def problems(self, memory: Memory, kinds_of: dict[str, str]) -> list[str]:
        kind = self.kinds.get(memory.kind)
        if kind is None:
            if self.strict and memory.kind not in BUILTIN:
                return [f"{memory.kind} is not a declared kind"]
            return []
        return kind.problems(memory, kinds_of)

    def guard(self, commit: Commit, state: State) -> list[str]:
        """Reject a commit that puts a memory out of line with its kind."""
        puts = commit.puts()
        kinds_of = {mid: m.kind for mid, m in state.memories.items()} | {m.id: m.kind for m in puts}
        return [f"{m.kind} {m.key!r}: {p}" for m in puts for p in self.problems(m, kinds_of)]

    def check(self, memory: Memory, store: MemoryStore) -> list[str]:
        """Problems with a memory already in `store`, for auditing data written before a schema existed."""
        return self.problems(memory, {mid: m.kind for mid, m in store.state.memories.items()})

    def audit(self, store: MemoryStore) -> dict[str, list[str]]:
        """Problems with every live memory, by memory id."""
        found = {}
        for memory in store.find():
            problems = self.check(memory, store)
            if problems:
                found[memory.id] = problems
        return found

    # Using it ------------------------------------------------------------

    def attach(self, store: MemoryStore) -> Memory | None:
        """Enforce this schema on `store`, and record it in the log. Returns the schema memory, if one was written."""
        store.guard(self.guard)
        return self.record(store)

    def record(self, store: MemoryStore) -> Memory | None:
        """Write the schema to the log; the previous version of this schema goes stale. A no-op if unchanged."""
        previous = store.find(kind=SCHEMA, key=self.name)
        with store.transaction(SYSTEM) as tx:
            written = tx.put(SCHEMA, self.name, self.to_json(), tier="long")
            for old in previous:
                if old.id != written.id:
                    tx.mark_stale(old.id, f"superseded by schema version {self.version}")
        return written if tx.committed else None

    def put(self, store: MemoryStore, agent: str, kind: str, key: str, body: Any = None, **kwargs: Any) -> Memory:
        """`store.put` with the declared kind's default tier and importance filled in."""
        declared = self.kinds.get(kind)
        if declared is not None:
            kwargs.setdefault("tier", declared.tier)
            if declared.importance:
                kwargs.setdefault("importance", declared.importance)
        return store.put(agent, kind, key, body, **kwargs)

    # Describing it -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "strict": self.strict, "kinds": [k.to_json() for k in self.kinds.values()]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Schema:
        return cls(*(Kind.from_json(k) for k in data["kinds"]), strict=data.get("strict", False), name=data["name"])

    @classmethod
    def load(cls, store: MemoryStore, name: str = "memory") -> Schema | None:
        """The live schema recorded in `store`, if any."""
        found = store.find(kind=SCHEMA, key=name)
        return cls.from_json(found[-1].body) if found else None

    def describe(self) -> str:
        """The structure as Markdown, one section per kind: what an agent reads to know what it may write."""
        lines = [f"# Memory structure: {self.name} (version {self.version})", ""]
        for kind in self.kinds.values():
            lines.append(f"## {kind.name}" + (f": {kind.description}" if kind.description else ""))
            facts = [f"tier `{kind.tier}`"]
            if kind.scopes is not None:
                facts.append("scopes " + ", ".join(f"`{s}`" for s in kind.scopes))
            if kind.requires:
                facts.append("derived from " + ", ".join(f"`{r}`" for r in kind.requires))
            lines.append("- " + "; ".join(facts))
            if kind.body is not None:
                lines.append(f"- body: `{canonical(kind.body)}`")
            lines.append("")
        return "\n".join(lines)
