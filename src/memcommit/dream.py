"""Dreaming: an offline pass that turns finished sessions into notes, and keeps the notes tidy.

    dreamer = Dreamer(store)                    # or Dreamer(store, extractor=my_model_extractor)
    report = dreamer.run()                      # extract learnings from sessions that ended since the last run
    dreamer.run(dry_run=True).changes           # what it would do, without writing
    dreamer.undo(report)                        # take a run back, if nobody else has written since

An agent that saves notes by tool call (`knowledge`) only remembers what it
thought to save. Most of what matters is implied: a preference stated in
passing, a sequence of tool calls that worked. A dreaming pass reads the
sessions that ended since the last pass, asks an extractor for the learnings in
each, and writes them as notes, each derived from the session and the turns it
came from. Then it maintains the store:

- **Conflicts.** A learning whose path already holds a different note replaces
  it. The old version goes stale, with what was built on it, and the new note
  says in its history which session changed it.
- **Duplicates.** Live notes that say nearly the same thing are merged: the
  most important is kept, the rest go to the archive.
- **Oversized notes.** A note past `max_chars` is split at paragraph breaks into
  parts derived from it, and the original goes to the archive.

The extractor is a function from a session to learnings. `heuristic_extractor`
needs no model: it finds statements of fact and preference in what the user
said, and records a tool sequence that ended well as a playbook. A language
model is a function with the same signature.

Everything a pass does is a transaction with provenance, so it can be audited
(`lineage` from a note back to its episodes), repaired (retract a bad episode
and its notes go stale), or reversed (`undo`). A pass records what it covered
as a `dream` memory, which is how the next one knows where to start.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .episodes import USER, Episodes, Session
from .knowledge import KNOWLEDGE, Knowledge
from .records import ARCHIVE, LONG
from .store import SYSTEM, MemoryStore
from .traces import TRACE_END

DREAM = "dream"
DREAMER = "dreamer"
MEMORY_TOOLS = frozenset({"add_memory", "update_memory", "delete_memory", "search_memory", "fetch_memory"})


@dataclass(frozen=True)
class Learning:
    """One thing worth keeping, and where in the session it came from."""

    path: str
    text: str
    title: str = ""
    tags: tuple[str, ...] = ()
    importance: float = 0.5
    sources: tuple[str, ...] = ()


Extractor = Callable[[Session], list[Learning]]


@dataclass(frozen=True)
class Change:
    """One edit a pass makes (or would make): `add`, `update`, `archive`, or `split`."""

    action: str
    path: str
    text: str = ""
    reason: str = ""
    sources: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    importance: float = 0.5


@dataclass
class DreamReport:
    start_lsn: int
    end_lsn: int
    sessions: list[str] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    dry_run: bool = False
    memory: str = ""

    def count(self, action: str) -> int:
        return sum(1 for c in self.changes if c.action == action)


def slug(text: str, limit: int = 40) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words)[:limit].strip("-") or "note"


_VALUE = r"([^.!?,;]+?)(?= and | but | so |[.!?,;]|$)"  # a value ends at punctuation or a conjunction
_CITY = re.compile(r"\bI (?:live in|am living in|moved to|am moving to|relocated to) " + _VALUE, re.I)
_ATTRIBUTE = re.compile(r"\bmy ([a-z]+(?: [a-z]+)?) (?:is|are) (?:named |called )?" + _VALUE, re.I)
_PREFERENCE = re.compile(r"\bI (?:prefer|like|love|always use|usually use) " + _VALUE, re.I)
_REMEMBER = re.compile(r"\bremember (?:that )?([^.!?;]+)", re.I)


def heuristic_extractor(session: Session) -> list[Learning]:
    """Facts and preferences the user stated, and a playbook from a tool sequence that ended well. No model."""
    who = session.user or "user"
    out: list[Learning] = []
    turns = [t for t in session.transcript() if t["role"] == USER]
    for turn in turns:
        text, source = turn["text"], (turn["id"],)
        for value in _CITY.findall(text):
            out.append(
                Learning(f"user/{who}/city", f"{who} lives in {value.strip()}.", tags=("profile",), sources=source)
            )
        for attribute, value in _ATTRIBUTE.findall(text):
            path = f"user/{who}/{slug(attribute)}"
            out.append(Learning(path, f"{who}'s {attribute} is {value.strip()}.", tags=("profile",), sources=source))
        for value in _PREFERENCE.findall(text):
            path = f"user/{who}/prefers/{slug(value)}"
            out.append(Learning(path, f"{who} prefers {value.strip()}.", tags=("preference",), sources=source))
        for value in _REMEMBER.findall(text):
            out.append(
                Learning(
                    f"user/{who}/remembered/{slug(value)}",
                    value.strip().capitalize() + ".",
                    sources=source,
                    importance=0.7,
                )
            )
    calls = [t for t in session.transcript() if t["role"] == "tool_call" and t["tool"] not in MEMORY_TOOLS]
    if turns and len(calls) >= 2 and session.trace.status() == "ok":
        task = turns[0]["text"]
        steps = " then ".join(f"{c['tool']}({_short(c['arguments'])})" for c in calls)
        sources = (turns[0]["id"], *(c["id"] for c in calls))
        out.append(
            Learning(
                f"playbooks/{slug(task)}", f"To {task.rstrip('.?!')}: {steps}.", tags=("playbook",), sources=sources
            )
        )
    return out


def _short(arguments: Any, limit: int = 40) -> str:
    text = ", ".join(f"{k}={v}" for k, v in arguments.items()) if isinstance(arguments, dict) else str(arguments)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _words(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


class Dreamer:
    def __init__(
        self,
        store: MemoryStore,
        extractor: Extractor = heuristic_extractor,
        *,
        agent: str = DREAMER,
        max_chars: int = 1200,
        duplicate: float = 0.85,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.agent = agent
        self.max_chars = max_chars
        self.duplicate = duplicate
        self.notes = Knowledge(store, agent)
        self.episodes = Episodes(store)

    # What is due ---------------------------------------------------------

    def watermark(self) -> int:
        """The log position the last pass covered up to."""
        return max((m.body["covers_lsn"] for m in self.store.find(kind=DREAM, status=None)), default=0)

    def pending(self) -> list[Session]:
        return self.episodes.new_since(self.watermark())

    def due(self, min_sessions: int = 1) -> bool:
        return len(self.pending()) >= min_sessions

    def attach(self, min_sessions: int = 3) -> None:
        """Run a pass whenever `min_sessions` sessions have ended since the last one."""

        def observe(commit: Any, _state: Any) -> None:
            ended = any(r.memory.kind == TRACE_END for r in commit.records)
            if ended and commit.agent != self.agent and self.due(min_sessions):
                self.store.defer(self.run)

        self.store.subscribe(observe)

    # A pass --------------------------------------------------------------

    def run(self, dry_run: bool = False) -> DreamReport:
        start = self.store.lsn
        sessions = self.pending()
        report = DreamReport(start, start, [s.id for s in sessions], dry_run=dry_run)
        for session in sessions:
            for change in self._learn(session):
                report.changes.append(change)
                if not dry_run:
                    self._apply(change)
        for change in self._maintain():
            report.changes.append(change)
            if not dry_run:
                self._apply(change)
        if not dry_run and (sessions or report.changes):
            report.memory = self._record(report, start).id
            report.end_lsn = self.store.lsn
        return report

    def _learn(self, session: Session) -> list[Change]:
        latest = {learning.path: learning for learning in self.extractor(session)}  # the last statement wins
        changes = []
        for path, learning in latest.items():
            sources = (session.id, *learning.sources)
            existing = self.notes.read(path)
            if existing is None:
                action, reason = "add", f"from session {session.id}"
            elif existing.body["text"] != learning.text:
                action, reason = "update", f"session {session.id} says differently"
            else:
                continue
            changes.append(Change(action, path, learning.text, reason, sources, learning.tags, learning.importance))
        return changes

    def _maintain(self) -> list[Change]:
        changes: list[Change] = []
        notes = self.notes.ls()
        state = self.store.state
        dropped: set[str] = set()
        ranked = sorted(notes, key=lambda m: (-m.importance, state.first[m.id].lsn))
        for i, kept in enumerate(ranked):
            if kept.key in dropped:
                continue
            for other in ranked[i + 1 :]:
                if other.key in dropped or kept.key.rsplit("/", 1)[0] != other.key.rsplit("/", 1)[0]:
                    continue
                a, b = _words(kept.body["text"]), _words(other.body["text"])
                if a and b and len(a & b) / len(a | b) >= self.duplicate:
                    dropped.add(other.key)
                    changes.append(Change("archive", other.key, reason=f"duplicate of {kept.key}", sources=(kept.id,)))
        for note in notes:
            if note.key not in dropped and len(note.body["text"]) > self.max_chars:
                changes.append(
                    Change("split", note.key, reason=f"over {self.max_chars} characters", sources=(note.id,))
                )
        return changes

    def _apply(self, change: Change) -> None:
        options: dict[str, Any] = {"tags": change.tags, "derived_from": change.sources, "importance": change.importance}
        match change.action:
            case "add":
                self.notes.add(change.path, change.text, **options)
            case "update":
                self.notes.update(change.path, change.text, reason=change.reason, **options)
            case "archive":
                note = self.notes.read(change.path)
                if note is not None:
                    self.store.move([note.id], ARCHIVE, change.reason, agent=self.agent)
            case "split":
                self._split(change)

    def _split(self, change: Change) -> None:
        note = self.notes.read(change.path)
        if note is None:
            return
        parts: list[str] = []
        for paragraph in re.split(r"\n\s*\n", note.body["text"]):
            if parts and len(parts[-1]) + len(paragraph) + 2 <= self.max_chars:
                parts[-1] += "\n\n" + paragraph
            else:
                parts.append(paragraph)
        with self.store.transaction(self.agent) as tx:
            for n, part in enumerate(parts, 1):
                body = {"path": f"{note.key}-part-{n}", "title": "", "text": part, "tags": note.body.get("tags", [])}
                tx.put(KNOWLEDGE, body["path"], body, derived_from=[note.id], importance=note.importance, tier=LONG)
            tx.move(note.id, ARCHIVE, f"split into {len(parts)} parts")

    def _record(self, report: DreamReport, start: int) -> Any:
        body = {
            "covers_lsn": start,
            "start_lsn": start,
            "sessions": report.sessions,
            "changes": [{"action": c.action, "path": c.path, "reason": c.reason} for c in report.changes],
        }
        key = f"pass@{start}"
        return self.store.put(self.agent, DREAM, key, body, derived_from=report.sessions, tier=LONG)

    # Taking a pass back --------------------------------------------------

    def undo(self, report: DreamReport) -> list[Any]:
        """Roll the log back to before `report`. Refuses if any other agent has written since."""
        others = {r.agent for r in self.store.history() if r.lsn > report.start_lsn} - {self.agent, SYSTEM}
        if others:
            raise RuntimeError(f"cannot undo: {', '.join(sorted(others))} wrote after the pass")
        return self.store.rollback_to(report.start_lsn, f"undo dream pass at lsn {report.start_lsn}")
