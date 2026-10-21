import pytest

from memcommit import Episodes, MemoryStore, Session


def test_a_session_records_every_turn_and_tool_call_as_it_happens():
    store = MemoryStore()
    session = Session.start(store, "assistant", user="alice")
    session.user_says("I live in Lyon.")
    lookup = session.wrap(lambda topic: f"about {topic}", "lookup")
    assert lookup(topic="Lyon") == "about Lyon"
    reply = session.agent_says("Noted.")
    session.end()
    roles = [t["role"] for t in session.transcript()]
    assert roles == ["user", "tool_call", "tool_result", "assistant"]
    assert store.get(reply.id).body["role"] == "assistant"
    assert "user: I live in Lyon." in session.text()


def test_a_failing_tool_is_recorded_and_still_raises():
    store = MemoryStore()
    session = Session.start(store, "assistant")

    def broken(**_):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        session.call("lookup", {"q": 1}, broken)
    assert any(m.kind == "error" for m in session.trace.steps)


def test_a_reply_derives_from_what_it_read_so_a_retraction_stales_it():
    store = MemoryStore()
    fact = store.put("a", "fact", "x", {"v": 1})
    session = Session.start(store, "assistant")
    reply = session.agent_says("It is 1.", used=[fact.id])
    store.invalidate([fact.id], reason="wrong")
    assert store.status(reply.id) == "stale"


def test_the_block_ends_the_session_ok_or_error():
    store = MemoryStore()
    with Session.start(store, "a", user="u") as s:
        s.user_says("hi")
    assert s.trace.status() == "ok"
    with pytest.raises(ValueError), Session.start(store, "a") as bad:
        raise ValueError("boom")
    assert bad.trace.status() == "error"


def test_episodes_find_sessions_by_user_and_what_ended_since_a_point():
    store = MemoryStore()
    one = Session.start(store, "a", user="alice")
    one.end()
    mark = store.lsn
    two = Session.start(store, "a", user="bob")
    open_one = Session.start(store, "a", user="bob")
    two.end()
    episodes = Episodes(store)
    assert [s.id for s in episodes.sessions(user="bob")] == [two.id, open_one.id]
    assert [s.id for s in episodes.sessions(ended=False)] == [open_one.id]
    assert [s.id for s in episodes.new_since(mark)] == [two.id]
    assert Session.open(store, two.id).user == "bob"
