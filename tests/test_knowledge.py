import pytest

from memcommit import Knowledge, MemoryStore, Session


def notes():
    store = MemoryStore()
    return store, Knowledge(store, "assistant")


def test_add_read_and_ls():
    _store, k = notes()
    k.add("user/alice/city", "Alice lives in Lyon.", tags=["profile"])
    k.add("user/alice/pet", "Alice has a dog named Rex.")
    k.add("user/bob/city", "Bob lives in Oslo.")
    assert k.read("user/alice/city").body["text"] == "Alice lives in Lyon."
    assert [m.key for m in k.ls("user/alice")] == ["user/alice/city", "user/alice/pet"]
    assert [m.key for m in k.ls("user")] == ["user/alice/city", "user/alice/pet", "user/bob/city"]
    with pytest.raises(FileExistsError):
        k.add("user/alice/city", "again")
    with pytest.raises(ValueError):
        k.add("../escape", "x")


def test_update_supersedes_and_stales_what_was_built_on_the_old_version():
    store, k = notes()
    old = k.add("user/alice/city", "Alice lives in Lyon.")
    built = store.put("assistant", "plan", "visit", {"go": "Lyon"}, derived_from=[old.id])
    new = k.update("user/alice/city", "Alice moved to Paris.")
    assert store.status(old.id) == "stale" and store.status(new.id) == "live"
    assert store.status(built.id) == "stale"
    assert k.read("user/alice/city").id == new.id
    k.update("user/alice/city", "Alice moved to Paris.")  # same text again: no change
    assert len(store.history(new.id)) == 1


def test_update_without_cascade_leaves_dependents_alone():
    store, k = notes()
    old = k.add("a", "one")
    built = store.put("assistant", "plan", "p", {}, derived_from=[old.id])
    k.update("a", "two", cascade=False)
    assert store.status(built.id) == "live"


def test_delete_retracts_and_stales_dependents():
    store, k = notes()
    note = k.add("a", "wrong")
    built = store.put("assistant", "plan", "p", {}, derived_from=[note.id])
    k.delete("a", "it was wrong")
    assert store.status(note.id) == "retracted" and store.status(built.id) == "stale"
    assert k.read("a") is None


def test_the_index_lists_notes_by_directory_within_a_budget():
    _store, k = notes()
    k.add("user/alice/city", "Alice lives in Lyon.", importance=0.9)
    k.add("user/alice/pet", "Alice has a dog named Rex.", tags=["pets"])
    k.add("playbooks/refund", "# Refund\nCheck the order, then refund.")
    text = k.index()
    assert "## user/alice" in text and "- city: Alice lives in Lyon." in text and "[pets]" in text
    assert "## playbooks" in text and "- refund: Refund" in text
    for i in range(40):
        k.add(f"bulk/n{i}", f"note number {i} " + "x" * 50)
    short = k.index(max_chars=900)
    assert len(short) < 1100 and "more notes not listed" in short and "city: Alice lives in Lyon." in short


def test_search_spans_derived_and_episodic_memory_and_fetch_returns_detail():
    store, k = notes()
    session = Session.start(store, "assistant", user="alice")
    turn = session.user_says("My cat Miso is allergic to chicken, so only fish.")
    session.end()
    k.add("user/alice/cat", "Alice's cat Miso eats fish only.", derived_from=[turn.id])
    derived = k.search("what does the cat eat", where="derived")
    assert derived[0]["path"] == "user/alice/cat" and derived[0]["tier"] == "derived"
    episodic = k.search("allergic chicken", where="episodic")
    assert episodic[0]["tier"] == "episodic" and "chicken" in episodic[0]["snippet"]
    assert {h["tier"] for h in k.search("cat", where="all")} == {"derived", "episodic"}
    assert k.fetch("user/alice/cat")["derived_from"] == [turn.id]
    assert k.fetch(session.id)["transcript"][0]["text"].startswith("My cat Miso")
    assert k.fetch("missing/path") is None


def test_reads_are_collected_so_a_reply_can_derive_from_them():
    store, k = notes()
    note = k.add("a", "the sky is blue")
    k.search("sky")
    session = Session.start(store, "assistant")
    reply = session.agent_says("Blue.", used=k.take_reads())
    assert note.id in store.get(reply.id).derived_from and k.take_reads() == []


def test_the_tools_are_described_and_callable_and_recorded_in_a_session():
    store, k = notes()
    assert [t["name"] for t in k.tools()] == [
        "add_memory",
        "update_memory",
        "delete_memory",
        "search_memory",
        "fetch_memory",
    ]
    session = Session.start(store, "assistant")
    assert k.call("add_memory", {"path": "x/y", "text": "hello there"}, session=session)["ok"]
    assert k.call("search_memory", {"query": "hello"}, session=session)[0]["path"] == "x/y"
    assert [t["role"] for t in session.transcript()].count("tool_call") == 2
    with pytest.raises(ValueError):
        k.call("nope", {})
