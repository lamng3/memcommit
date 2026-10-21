"""Episodic memory: sessions recorded as they happen, kept whole, found again later.

    session = Session.start(store, "assistant", user="alice")
    session.user_says("I live in Lyon.")
    lookup = session.wrap(search_web, "search_web")        # every call is recorded, with its result
    session.agent_says("Noted.")
    session.end()

A session is a trace (see `traces`) with conversational helpers: each user
turn, agent turn, tool call, and tool result is its own memory, committed as it
happens, so a crash keeps what was said. `agent_says(used=...)` derives the
reply from the memories it read, so retracting a bad memory stales every
answer that leaned on it.

The episodic tier is the full record: nothing is distilled away, so detail a
summary dropped can still be recovered. `Episodes` finds sessions, and
`new_since(lsn)` lists the ones that ended after a point in the log, which is
what an offline consolidation reads.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from typing import Any

from .records import SHARED, Memory
from .store import MemoryStore
from .traces import ACTION, MESSAGE, OBSERVATION, TRACE, TRACE_END, Trace

SESSION = "session"
USER, ASSISTANT = "user", "assistant"


class Session:
    def __init__(self, trace: Trace, user: str = "") -> None:
        self.trace = trace
        self.user = user

    @classmethod
    def start(
        cls,
        store: MemoryStore,
        agent: str,
        user: str = "",
        *,
        run: str | None = None,
        scope: str = SHARED,
        derived_from: Iterable[str] = (),
    ) -> Session:
        trace = Trace.start(
            store, agent, SESSION, inputs={"user": user}, run=run, scope=scope, derived_from=derived_from
        )
        return cls(trace, user)

    @classmethod
    def open(cls, store: MemoryStore, root_id: str) -> Session:
        trace = Trace.open(store, root_id)
        return cls(trace, (trace.root.body.get("inputs") or {}).get("user", ""))

    def __enter__(self) -> Session:
        return self

    def __exit__(self, kind: Any, error: Any, _tb: Any) -> None:
        if not self.trace.ended:
            self.end("error", error=repr(error)) if error else self.end()

    @property
    def id(self) -> str:
        return self.trace.id

    @property
    def agent(self) -> str:
        return self.trace.agent

    # Turns ---------------------------------------------------------------

    def user_says(self, text: str, **extra: Any) -> Memory:
        return self.trace.message(self.agent, text, role=USER, user=self.user, **extra)

    def agent_says(self, text: str, used: Iterable[str] = (), **extra: Any) -> Memory:
        """The agent's reply. `used` are the memories it read; the reply is derived from them."""
        return self.trace.message(self.user or "user", text, role=ASSISTANT, derived_from=list(used), **extra)

    def call(self, tool: str, arguments: Any, run: Callable[..., Any]) -> Any:
        """Run `run(**arguments)` (or `run(arguments)`), recording the call, its result, or its error."""
        action = self.trace.action(tool, arguments)
        try:
            value = run(**arguments) if isinstance(arguments, dict) else run(arguments)
        except Exception as err:
            self.trace.error(repr(err), parent=action, tool=tool)
            raise
        self.trace.observation(value, parent=action, tool=tool)
        return value

    def wrap(self, function: Callable[..., Any], name: str | None = None) -> Callable[..., Any]:
        """`function` with every call recorded in this session, as a plugin would."""
        tool = name or function.__name__

        @functools.wraps(function)
        def recorded(*args: Any, **kwargs: Any) -> Any:
            arguments = {"args": list(args), **kwargs} if args else dict(kwargs)
            return self.call(tool, arguments, lambda **_: function(*args, **kwargs))

        return recorded

    def end(self, status: str = "ok", **extra: Any) -> Memory:
        return self.trace.end(status, **extra)

    # Reads ---------------------------------------------------------------

    def transcript(self) -> list[dict[str, Any]]:
        """The session in order: user and agent turns, tool calls, and what they returned."""
        out = []
        for m in self.trace.steps:
            if m.kind == MESSAGE:
                out.append({"id": m.id, "role": m.body.get("role", "message"), "text": m.body["content"]})
            elif m.kind == ACTION:
                out.append({"id": m.id, "role": "tool_call", "tool": m.body["tool"], "arguments": m.body["arguments"]})
            elif m.kind == OBSERVATION:
                out.append({"id": m.id, "role": "tool_result", "tool": m.body.get("tool"), "text": m.body["content"]})
        return out

    def text(self) -> str:
        return "\n".join(f"{t['role']}: {t.get('text', t.get('arguments', ''))}" for t in self.transcript())


class Episodes:
    """The sessions in a store."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def sessions(self, user: str | None = None, agent: str | None = None, ended: bool | None = None) -> list[Session]:
        """Sessions oldest first, optionally for one user or agent, or only the ended (or unended) ones."""
        out = []
        for root in self.store.find(kind=TRACE, status=None):
            body = root.body
            if body.get("task") != SESSION or (agent is not None and body["agent"] != agent):
                continue
            session = Session.open(self.store, root.id)
            if user is not None and session.user != user:
                continue
            if ended is not None and session.trace.ended != ended:
                continue
            out.append(session)
        return out

    def new_since(self, lsn: int) -> list[Session]:
        """Sessions that ended after `lsn`, in the order they ended."""
        state = self.store.state
        ends = [m for m in self.store.find(kind=TRACE_END, status=None) if state.first[m.id].lsn > lsn]
        ends.sort(key=lambda m: state.first[m.id].lsn)
        out = []
        for end in ends:
            root = self.store.get(end.body["trace"])
            if root is not None and root.body.get("task") == SESSION:
                out.append(Session.open(self.store, root.id))
        return out
