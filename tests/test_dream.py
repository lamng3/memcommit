import pytest

from memcommit import Dreamer, Knowledge, Learning, MemoryStore, Session, verify


def chat(store, user, *turns, tools=()):
    session = Session.start(store, "assistant", user=user)
    for turn in turns:
        session.user_says(turn)
    for tool, args in tools:
        session.call(tool, args, lambda **_: "ok")
    session.end()
    return session


def test_dreaming_extracts_implied_facts_with_provenance_back_to_the_turn():
    store = MemoryStore()
    session = chat(store, "alice", "Hi! I live in Lyon and my dog is named Rex.", "I prefer oat milk.")
    report = Dreamer(store).run()
    notes = Knowledge(store, "assistant")
    assert notes.read("user/alice/city").body["text"] == "alice lives in Lyon."
    assert notes.read("user/alice/dog").body["text"] == "alice's dog is Rex."
    assert notes.read("user/alice/prefers/oat-milk")
    assert report.count("add") == 3 and report.sessions == [session.id]
    note = notes.read("user/alice/city")
    assert session.id in note.derived_from and session.trace.steps[0].id in note.derived_from


def test_a_pass_covers_only_new_sessions_and_a_second_run_does_nothing():
    store = MemoryStore()
    chat(store, "alice", "I live in Lyon.")
    dreamer = Dreamer(store)
    assert dreamer.due()
    assert dreamer.run().count("add") == 1
    assert not dreamer.due() and dreamer.run().changes == []
    chat(store, "alice", "I prefer tea.")
    assert [c.path for c in dreamer.run().changes] == ["user/alice/prefers/tea"]


def test_a_newer_statement_replaces_the_note_and_stales_what_was_built_on_it():
    store = MemoryStore()
    chat(store, "alice", "I live in Lyon.")
    dreamer = Dreamer(store)
    dreamer.run()
    notes = Knowledge(store, "assistant")
    old = notes.read("user/alice/city")
    plan = store.put("assistant", "plan", "visit", {"go": "Lyon"}, derived_from=[old.id])
    chat(store, "alice", "Big news: I moved to Paris.")
    report = dreamer.run()
    assert [c.action for c in report.changes] == ["update"]
    assert notes.read("user/alice/city").body["text"] == "alice lives in Paris."
    assert store.status(old.id) == "stale" and store.status(plan.id) == "stale"


def test_a_tool_sequence_that_worked_becomes_a_playbook():
    store = MemoryStore()
    chat(store, "bob", "Refund order 12", tools=[("get_order", {"id": 12}), ("refund", {"id": 12})])
    Dreamer(store).run()
    playbook = Knowledge(store, "assistant").ls("playbooks")[0]
    assert playbook.body["text"] == "To Refund order 12: get_order(id=12) then refund(id=12)."


def test_dry_run_reports_without_writing():
    store = MemoryStore()
    chat(store, "alice", "I live in Lyon.")
    dreamer = Dreamer(store)
    lsn = store.lsn
    report = dreamer.run(dry_run=True)
    assert report.dry_run and report.count("add") == 1 and store.lsn == lsn
    assert dreamer.due()


def test_retracting_an_episode_stales_the_notes_learned_from_it():
    store = MemoryStore()
    session = chat(store, "alice", "I live in Lyon.")
    Dreamer(store).run()
    note = Knowledge(store, "assistant").read("user/alice/city")
    store.invalidate([session.id], reason="the session was spoofed")
    assert store.status(note.id) == "stale"
    assert not Dreamer(store).due()  # a stale pass record still counts: nothing is dreamed twice


def test_duplicates_are_archived_and_oversized_notes_are_split():
    store = MemoryStore()
    notes = Knowledge(store, "assistant")
    notes.add("faq/a", "Refunds take five business days to arrive.", importance=0.9)
    notes.add("faq/b", "Refunds take five business days to arrive!", importance=0.2)
    notes.add("faq/long", "\n\n".join(f"Paragraph {i} " + "word " * 20 for i in range(8)))
    dreamer = Dreamer(store, max_chars=300)
    report = dreamer.run()
    assert {(c.action, c.path) for c in report.changes} == {("archive", "faq/b"), ("split", "faq/long")}
    keys = [m.key for m in notes.ls("faq")]
    assert "faq/a" in keys and "faq/b" not in keys and "faq/long" not in keys
    parts = [m for m in notes.ls("faq") if "-part-" in m.key]
    assert len(parts) > 1 and all(len(m.body["text"]) <= 300 for m in parts)
    assert all(
        store.get(m.id).derived_from
        == (store.find(kind="knowledge", key="faq/long", status=None, tiers=["archive"])[0].id,)
        for m in parts
    )


def test_a_custom_extractor_replaces_the_heuristic_one():
    store = MemoryStore()
    chat(store, "alice", "anything at all")
    dreamer = Dreamer(store, extractor=lambda s: [Learning("topics/x", "A model said so.", sources=(s.id,))])
    dreamer.run()
    assert Knowledge(store, "assistant").read("topics/x").body["text"] == "A model said so."


def test_undo_takes_a_pass_back_unless_someone_else_wrote():
    store = MemoryStore()
    chat(store, "alice", "I live in Lyon.")
    dreamer = Dreamer(store)
    report = dreamer.run()
    dreamer.undo(report)
    assert Knowledge(store, "assistant").read("user/alice/city") is None
    chat(store, "bob", "I live in Oslo.")
    second = dreamer.run()
    store.put("someone", "fact", "x", {})
    with pytest.raises(RuntimeError, match="someone"):
        dreamer.undo(second)
    assert verify(store) == []


def test_attach_runs_a_pass_when_enough_sessions_have_ended():
    store = MemoryStore()
    dreamer = Dreamer(store)
    dreamer.attach(min_sessions=2)
    chat(store, "alice", "I live in Lyon.")
    assert Knowledge(store, "assistant").read("user/alice/city") is None
    chat(store, "bob", "I live in Oslo.")
    notes = Knowledge(store, "assistant")
    assert notes.read("user/alice/city") and notes.read("user/bob/city")
