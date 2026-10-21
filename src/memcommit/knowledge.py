"""Derived memory: what an agent has learned, kept as notes at paths, with an index it reads first.

    notes = Knowledge(store, "assistant")
    notes.add("user/alice/city", "Alice lives in Lyon.", tags=["profile"], derived_from=[turn.id])
    notes.update("user/alice/city", "Alice moved to Paris.")      # the old version goes stale, history kept
    print(notes.index())                                           # the catalog: inject it at session start
    notes.search("where does alice live")                          # derived notes, or episodes, or both
    notes.fetch("user/alice/city")

Derived memory is the distilled tier: short notes that say what matters,
organised as a hierarchy of paths, and cheap enough to put in front of an
agent. Episodic memory (`episodes`) holds everything that happened; a note
records where it came from through `derived_from`, so the detail behind a
claim can always be fetched, and a claim built on a bad episode goes stale.

A path is a note's key, so a note looks mutable though the log is not:
`update` writes the new version and marks the old one stale, and everything
derived from the old version goes stale with it unless `cascade=False`.
`delete` retracts: the note was wrong, and what was built on it falls too.

The agent reads in layers. `index()` is a Markdown catalog of every note's
path, title, and first line, sized to a budget; `search` finds notes (or
episodes, or both) by relevance, recency, and importance; `fetch` returns one
note in full, or an episode as its transcript. `tools()` describes those
operations, with `add`, `update`, and `delete`, as tool specs a model can call,
and `call` runs them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .episodes import Session
from .recall import Recall
from .records import LONG, MID, SHARED, SHORT, Memory
from .store import MemoryStore
from .traces import MESSAGE, OBSERVATION, TRACE

KNOWLEDGE = "knowledge"
WORKING = (SHORT, MID, LONG)
SUMMARY_LENGTH = 100


def normalize(path: str) -> str:
    """A path without leading or trailing slashes. Raises on empty segments and `..`."""
    parts = [p for p in path.strip().strip("/").split("/")]
    if not parts or any(not p or p in (".", "..") for p in parts):
        raise ValueError(f"not a note path: {path!r}")
    return "/".join(parts)


def first_line(text: str, limit: int = SUMMARY_LENGTH) -> str:
    line = next((ln.strip().lstrip("#").strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def snippet(text: str, query: str = "", width: int = 160) -> str:
    """The part of `text` around the first query word it contains, or its start."""
    text = " ".join(text.split())
    for word in re.findall(r"\w+", query.lower()):
        at = text.lower().find(word)
        if at >= 0:
            start = max(0, at - width // 4)
            return ("…" if start else "") + text[start : start + width] + ("…" if start + width < len(text) else "")
    return text[:width] + ("…" if len(text) > width else "")


class Knowledge:
    def __init__(self, store: MemoryStore, agent: str, *, scope: str = SHARED, recall: Recall | None = None) -> None:
        self.store = store
        self.agent = agent
        self.scope = scope
        self.recall = recall or Recall(store)
        self.reads: list[str] = []

    # Reads ---------------------------------------------------------------

    def read(self, path: str) -> Memory | None:
        """The live note at `path`, or None."""
        found = self.store.find(kind=KNOWLEDGE, key=normalize(path), tiers=WORKING, scopes=self._scopes())
        return found[-1] if found else None

    def ls(self, prefix: str = "") -> list[Memory]:
        """Live notes whose path starts with `prefix`, by path."""
        prefix = prefix.strip("/")
        notes = self.store.find(kind=KNOWLEDGE, tiers=WORKING, scopes=self._scopes())
        latest = {m.key: m for m in notes}  # the newest live note per path
        return sorted(
            (m for p, m in latest.items() if not prefix or p == prefix or p.startswith(prefix + "/")),
            key=lambda m: m.key,
        )

    def index(self, max_chars: int = 4000) -> str:
        """The catalog: every note by directory, with its title and first line, within `max_chars`.

        What does not fit is left out, most important and most recent kept first, and the tail says so.
        """
        notes = self.ls()
        state = self.store.state
        ranked = sorted(notes, key=lambda m: (-m.importance, -state.last[m.id].lsn))
        header = f"# Memory index\n\n{len(notes)} notes. Use search_memory to find more, fetch_memory to read one.\n"
        shown: list[Memory] = []
        used = len(header) + 80
        for m in ranked:
            cost = len(self._entry(m)) + len(m.key.rsplit("/", 1)[0]) + 8
            if used + cost > max_chars:
                break
            shown.append(m)
            used += cost
        lines = [header]
        directory = None
        for m in sorted(shown, key=lambda m: m.key):
            parent = m.key.rsplit("/", 1)[0] if "/" in m.key else ""
            if parent != directory:
                lines.append(f"\n## {parent or '/'}")
                directory = parent
            lines.append(self._entry(m))
        if len(shown) < len(notes):
            lines.append(f"\n… {len(notes) - len(shown)} more notes not listed; search_memory finds them.")
        return "\n".join(lines) + "\n"

    def _entry(self, memory: Memory) -> str:
        body = memory.body
        name = memory.key.rsplit("/", 1)[-1]
        tags = f" [{', '.join(body['tags'])}]" if body.get("tags") else ""
        return f"- {name}: {body.get('title') or first_line(body['text'])}{tags}"

    def search(self, query: str, k: int = 5, where: str = "derived") -> list[dict[str, Any]]:
        """Find memories. `where` is `derived` (notes), `episodic` (what was said and done), or `all`."""
        found: list[dict[str, Any]] = []
        if where in ("derived", "all"):
            for hit in self.recall.search(query, k, kinds=[KNOWLEDGE], scopes=self._scopes(), tiers=WORKING):
                body = hit.memory.body
                found.append(
                    {
                        "tier": "derived",
                        "id": hit.memory.id,
                        "path": hit.memory.key,
                        "title": body.get("title", ""),
                        "snippet": snippet(body["text"], query),
                        "score": round(hit.score, 4),
                    }
                )
        if where in ("episodic", "all"):
            for hit in self.recall.search(
                query, k, kinds=[MESSAGE, OBSERVATION], scopes=self._scopes(), tiers=None, touch=False
            ):
                body = hit.memory.body
                text = body.get("content", "")
                found.append(
                    {
                        "tier": "episodic",
                        "id": hit.memory.id,
                        "session": body.get("trace"),
                        "role": body.get("role", hit.memory.kind),
                        "snippet": snippet(text if isinstance(text, str) else str(text), query),
                        "score": round(hit.score, 4),
                    }
                )
        found.sort(key=lambda f: -f["score"])
        self.reads += [f["id"] for f in found[:k]]
        return found[:k]

    def fetch(self, ref: str) -> dict[str, Any] | None:
        """A note by path, or any memory by id. An episode's root returns its whole transcript."""
        if ref.startswith("m:"):
            memory = self.store.get(ref)
            if memory is None:
                return None
            self.reads.append(memory.id)
            if memory.kind == TRACE:
                session = Session.open(self.store, memory.id)
                return {"id": memory.id, "kind": "session", "transcript": session.transcript()}
            if memory.kind == KNOWLEDGE:
                return self._note(memory)
            return {"id": memory.id, "kind": memory.kind, "body": memory.body}
        note = self.read(ref)
        if note is None:
            return None
        self.reads.append(note.id)
        return self._note(note)

    def _note(self, memory: Memory) -> dict[str, Any]:
        state = self.store.state
        return {
            "id": memory.id,
            "path": memory.key,
            "title": memory.body.get("title", ""),
            "text": memory.body["text"],
            "tags": memory.body.get("tags", []),
            "derived_from": list(memory.derived_from),
            "status": state.status[memory.id],
        }

    def take_reads(self) -> list[str]:
        """The ids of the memories read since the last call; pass them as `used` to `Session.agent_says`."""
        out, self.reads = list(dict.fromkeys(self.reads)), []
        return out

    # Writes --------------------------------------------------------------

    def add(
        self,
        path: str,
        text: str,
        *,
        title: str = "",
        tags: Iterable[str] = (),
        derived_from: Iterable[str] = (),
        importance: float = 0.5,
    ) -> Memory:
        """Write a new note. Raises if a live note is already at `path`; use `update`."""
        path = normalize(path)
        if self.read(path) is not None:
            raise FileExistsError(f"{path} exists; update it instead")
        return self._put(path, text, title, tags, derived_from, importance)

    def update(
        self,
        path: str,
        text: str,
        *,
        title: str | None = None,
        tags: Iterable[str] | None = None,
        derived_from: Iterable[str] = (),
        importance: float | None = None,
        cascade: bool = True,
        reason: str = "",
    ) -> Memory:
        """Write a new version of the note at `path`. The old version goes stale, and with `cascade`, so does what
        was derived from it. Writing the same text again changes nothing."""
        path = normalize(path)
        old = self.read(path)
        if old is None:
            raise KeyError(f"no note at {path}; add it first")
        body = old.body
        return self._put(
            path,
            text,
            body.get("title", "") if title is None else title,
            body.get("tags", []) if tags is None else tags,
            derived_from,
            old.importance if importance is None else importance,
            supersedes=old,
            cascade=cascade,
            reason=reason or "superseded by a newer version",
        )

    def delete(self, path: str, reason: str = "") -> Memory:
        """Retract the note at `path`: it was wrong. What was derived from it goes stale."""
        note = self.read(path)
        if note is None:
            raise KeyError(f"no note at {normalize(path)}")
        with self.store.transaction(self.agent) as tx:
            tx.retract(note.id, reason or "deleted")
        return note

    def _put(
        self,
        path: str,
        text: str,
        title: str,
        tags: Iterable[str],
        derived_from: Iterable[str],
        importance: float,
        supersedes: Memory | None = None,
        cascade: bool = True,
        reason: str = "",
    ) -> Memory:
        body = {"path": path, "title": title or first_line(text), "text": text, "tags": sorted(set(tags))}
        with self.store.transaction(self.agent) as tx:
            note = tx.put(
                KNOWLEDGE, path, body, scope=self.scope, derived_from=derived_from, importance=importance, tier=LONG
            )
            if supersedes is not None and supersedes.id != note.id:
                tx.mark_stale(supersedes.id, reason)
                if cascade:
                    for dependent in self.store.state.dependents([supersedes.id]):
                        tx.mark_stale(dependent, f"built on {path}, which changed")
        return note

    def _scopes(self) -> list[str]:
        return sorted({SHARED, self.scope, self.agent})

    # The agent's tools ---------------------------------------------------

    def tools(self) -> list[dict[str, Any]]:
        """Tool specs for a model: name, description, and a JSON schema for the input."""
        path = {"type": "string", "description": "Note path, like user/alice/city"}
        return [
            {
                "name": "add_memory",
                "description": "Save a new note the agent should remember across sessions.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": path,
                        "text": {"type": "string"},
                        "title": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["path", "text"],
                },
            },
            {
                "name": "update_memory",
                "description": "Replace the text of an existing note when it has changed or was wrong.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": path, "text": {"type": "string"}},
                    "required": ["path", "text"],
                },
            },
            {
                "name": "delete_memory",
                "description": "Delete a note that is wrong.",
                "input_schema": {"type": "object", "properties": {"path": path}, "required": ["path"]},
            },
            {
                "name": "search_memory",
                "description": "Search notes (derived), what was said and done (episodic), or both (all).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "where": {"type": "string", "enum": ["derived", "episodic", "all"]},
                        "k": {"type": "integer", "minimum": 1},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_memory",
                "description": "Read one note by path, or any memory or session by id, in full.",
                "input_schema": {
                    "type": "object",
                    "properties": {"ref": {"type": "string", "description": "A note path or a memory id"}},
                    "required": ["ref"],
                },
            },
        ]

    def call(self, name: str, arguments: dict[str, Any], session: Session | None = None) -> Any:
        """Run a tool by name. With a `session`, the call and its result are recorded in it."""
        if session is not None:
            return session.call(name, arguments, lambda **a: self.call(name, a))
        match name:
            case "add_memory":
                note = self.add(
                    arguments["path"],
                    arguments["text"],
                    title=arguments.get("title", ""),
                    tags=arguments.get("tags", ()),
                )
                return {"ok": True, "id": note.id, "path": note.key}
            case "update_memory":
                note = self.update(arguments["path"], arguments["text"])
                return {"ok": True, "id": note.id, "path": note.key}
            case "delete_memory":
                note = self.delete(arguments["path"])
                return {"ok": True, "path": note.key}
            case "search_memory":
                return self.search(arguments["query"], arguments.get("k", 5), arguments.get("where", "derived"))
            case "fetch_memory":
                return self.fetch(arguments["ref"])
        raise ValueError(f"unknown tool {name!r}")
