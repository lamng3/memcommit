"""An end-to-end sample agent on MemCommit, with no model and no network: `memcommit demo`.

A scripted assistant talks to a user over several sessions. It declares its memory
structure, reads the index at the start of each session, answers from notes it
searches, and saves what it is asked to remember. Between sessions an offline pass
(dreaming) turns what was said into notes. Then a fact changes, and an episode
turns out to be spoofed, and the notes built on each go stale.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from . import open as open_store
from .audit import verify
from .dream import Dreamer, slug
from .episodes import Session
from .evaluation import evaluate, make_workload, table
from .knowledge import Knowledge
from .schema import Kind, Schema
from .store import MemoryStore

SCHEMA = Schema(
    Kind(
        "knowledge",
        description="a note the assistant keeps about a user",
        body={
            "type": "object",
            "required": ["path", "text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        tier="long",
    ),
    name="assistant",
)


def reply(notes: Knowledge, session: Session, turn: str) -> str:
    """The scripted assistant: questions are answered from memory, everything else is acknowledged."""
    session.user_says(turn)
    if turn.rstrip().endswith("?"):
        hits = notes.call("search_memory", {"query": turn, "where": "derived", "k": 2}, session=session)
        text = hits[0]["snippet"] if hits else "I do not know yet."
    else:
        request = turn.lower().split("remember that ", 1)
        if len(request) == 2:
            path = f"user/{session.user}/remembered/{slug(request[1])}"
            notes.call("add_memory", {"path": path, "text": request[1].strip().capitalize()}, session=session)
        text = "Noted."
    session.agent_says(text, used=notes.take_reads())
    return text


def talk(store: MemoryStore, notes: Knowledge, user: str, turns: list[str], out: Callable[[str], None]) -> Session:
    with Session.start(store, "assistant", user=user) as session:
        for turn in turns:
            out(f"   {user}: {turn}\n   assistant: {reply(notes, session, turn)}")
    return session


def run(path: str | Path | None = None, out: Callable[[str], None] = print) -> int:
    with tempfile.TemporaryDirectory() as scratch:
        store = open_store(path or Path(scratch) / "demo.db")
        SCHEMA.attach(store)
        notes = Knowledge(store, "assistant")
        dreamer = Dreamer(store)

        out("1. The structure is declared once, and every write is checked against it")
        out("   kinds: " + ", ".join(SCHEMA.kinds) + f"   (version {SCHEMA.version})")

        out("2. Two users talk to the assistant; it keeps the full record of each session")
        talk(store, notes, "alice", ["I live in Lyon and my dog is named Rex.", "I prefer oat milk."], out)
        talk(store, notes, "bob", ["Remember that the wifi password is plum42.", "How is the weather?"], out)

        out("3. Dreaming: an offline pass extracts what was implied, with provenance back to the turns")
        preview = dreamer.run(dry_run=True)
        out(f"   preview: {len(preview.changes)} changes, nothing written")
        report = dreamer.run()
        out(f"   pass: {report.count('add')} notes added from {len(report.sessions)} sessions")

        out("4. The next session starts with the index, then answers from notes")
        out("   " + notes.index().strip().replace("\n", "\n   "))
        talk(store, notes, "alice", ["What is the name of alice's dog?"], out)

        out("5. A fact changes; the pass supersedes the note, and what was built on the old one goes stale")
        lyon = notes.read("user/alice/city")
        plan = store.put("assistant", "plan", "alice-trip", {"destination": "Lyon"}, derived_from=[lyon.id])
        talk(store, notes, "alice", ["Big news: I moved to Paris."], out)
        report = dreamer.run()
        out(f"   city note: {notes.read('user/alice/city').body['text']}")
        out(f"   the trip plan built on the old note is now {store.status(plan.id)}")

        out("6. An episode turns out to be spoofed; the notes learned from it go stale")
        spoof = talk(store, notes, "mallory", ["I live in Atlantis."], out)
        dreamer.run()
        atlantis = notes.read("user/mallory/city")
        store.invalidate([spoof.id], reason="the session was spoofed")
        out(f"   note {atlantis.body['text']!r} is now {store.status(atlantis.id)}")

        out("7. Does it help? A synthetic benchmark, no model (more in results/memory/)")
        results = evaluate(make_workload(10, 16, seed=0))
        out(
            "   "
            + table(
                [r for r in results if r.name in ("none", "full context", "episodic", "episodic + derived")]
            ).replace("\n", "\n   ")
        )

        problems = verify(store)
        out(f"8. The record\n   {store.lsn} log records, verify: {problems or 'ok'}")
        return 1 if problems else 0
