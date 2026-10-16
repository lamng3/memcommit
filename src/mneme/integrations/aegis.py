"""Aegis: every ontology version is a memory, and an Aegis rollback invalidates what was built on undone versions.

`OntologyVersions` is a listener on an Aegis `VersionedOntology` (it follows
Aegis's `Listener` protocol, `on_apply` and `on_rollback`, so this module
imports nothing from Aegis). Each commit Aegis logs becomes an
`ontology_version` memory:

    kind "ontology_version", key "materials@4", body {"lsn": 4, "digest": "9c1e...", "action": "..."}

Agents derive their results from `versions.ref()`, the current version's id.
Then:

- **Supersede.** A new commit marks the previous version stale, and with it
  everything derived from it, so the blackboard redoes that work against the
  new version. Answers always describe the ontology as it is now.
- **Roll back.** When Aegis rolls back (its detector found an attack), every
  version after the target goes stale, which carries every poisoned answer and
  every cache entry built on it. The target version is restored, and redone
  work that comes out identical restores the same memory ids it had before.

`version_guard` is the write-time half: it rejects a memory derived from a
version that is no longer current, so a slow agent cannot commit work done
against an ontology that has since changed.
"""

from __future__ import annotations

from typing import Any

from ..records import Commit, Memory
from ..state import State
from ..store import MemoryStore

VERSION = "ontology_version"


class OntologyVersions:
    def __init__(self, store: MemoryStore, name: str, agent: str = "aegis", supersede: bool = True) -> None:
        self.store = store
        self.name = name
        self.agent = agent
        self.supersede = supersede

    def watch(self, versioned: Any) -> Memory:
        """Record the current state as a version, then follow every later commit and rollback."""
        memory = self._put(versioned.lsn, versioned.state.digest(), "base")
        versioned.subscribe(self)
        return memory

    def key(self, lsn: int) -> str:
        return f"{self.name}@{lsn}"

    def versions(self, status: str | None = "live") -> list[Memory]:
        found = [m for m in self.store.find(kind=VERSION, status=status) if m.body.get("ontology") == self.name]
        return sorted(found, key=lambda m: m.body["lsn"])

    def current(self) -> Memory:
        live = self.versions()
        if not live:
            raise LookupError(f"no live version of {self.name}; call watch() first")
        return live[-1]

    def ref(self) -> str:
        """What an agent's result derives from: the current version's memory id."""
        return self.current().id

    def _put(self, lsn: int, digest: str, action: str) -> Memory:
        body = {"ontology": self.name, "lsn": lsn, "digest": digest, "action": action}
        return self.store.put(self.agent, VERSION, self.key(lsn), body)

    # Aegis Listener protocol --------------------------------------------

    def on_apply(self, record: Any, state: Any) -> None:
        previous = self.versions()
        self._put(record.lsn, record.after, str(record.action))
        if self.supersede and previous:
            self.store.invalidate([m.id for m in previous], f"superseded by {self.key(record.lsn)}", self.agent)

    def on_rollback(self, lsn: int, undone: list[Any], state: Any) -> None:
        later = [m.id for m in self.versions(status=None) if m.body["lsn"] > lsn]
        self.store.invalidate(later, f"aegis rolled {self.name} back to lsn {lsn}", self.agent)
        digest = state.digest()
        target = [m for m in self.versions(status=None) if m.body["lsn"] == lsn and m.body["digest"] == digest]
        if target:
            with self.store.transaction(self.agent) as tx:
                tx.restore(target[-1].id, f"aegis rolled {self.name} back to lsn {lsn}")
        else:
            self._put(lsn, digest, "rollback")


def version_guard(versions: OntologyVersions):
    """A guard: reject memories derived from a version of this ontology that is not the current one."""

    def guard(commit: Commit, state: State) -> list[str]:
        if commit.agent == versions.agent:
            return []
        problems = []
        for memory in commit.puts():
            for ref in memory.derived_from:
                source = state.get(ref)
                ours = source is not None and source.kind == VERSION and source.body.get("ontology") == versions.name
                if ours and not state.is_live(ref):
                    problems.append(f"{memory.kind} {memory.key!r} is derived from {source.key}, which is stale")
        return problems

    return guard
