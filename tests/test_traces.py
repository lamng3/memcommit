"""Traces: runs as trees of steps, written step by step, read back whole."""

import pytest

from mneme import LIVE, STALE, AgentSession, MemoryStore, Trace, trace


def test_a_run_is_a_tree_of_steps_built_on_its_root():
    store = MemoryStore()
    with trace(store, "planner", "book a trip", inputs={"city": "Lyon"}) as t:
        t.thought("dates first")
        call = t.action("search_flights", {"to": "LYO"})
        t.observation({"flights": 3}, parent=call)
        t.result({"status": "booked"}, importance=0.9)

    back = Trace.open(store, t.id)
    assert [m.kind for m in back.steps] == ["thought", "action", "observation", "result", "trace_end"]
    assert back.status() == "ok" and back.ended
    assert back.children(call) == [back.steps[2]]
    assert back.steps[3].importance == 0.9
    doc = back.to_json()
    assert doc["task"] == "book a trip" and doc["inputs"] == {"city": "Lyon"}
    assert doc["steps"][1]["tool"] == "search_flights" and doc["steps"][1]["memory"] == LIVE


def test_each_step_commits_on_its_own_and_a_failing_run_ends_in_error():
    store = MemoryStore()
    with pytest.raises(TimeoutError), trace(store, "planner", "flaky") as t:
        t.action("search_flights")
        raise TimeoutError("tool timed out")
    back = Trace.open(store, t.id)
    assert back.status() == "error" and "tool timed out" in back.steps[-1].body["error"]
    with pytest.raises(RuntimeError, match="ended"):
        t.thought("too late")


def test_invalidating_a_step_invalidates_every_step_after_it():
    store = MemoryStore()
    with trace(store, "planner", "plan", derived_from=["doc:itinerary@1"]) as t:
        first = t.observation("the hotel is open")
        later = t.thought("so book it")
    store.invalidate([first.id], "the hotel closed")
    assert store.status(later.id) == STALE
    store.invalidate(["doc:itinerary@1"], "new itinerary")
    assert store.status(t.id) == STALE


def test_a_handoff_starts_another_agents_trace():
    store = MemoryStore()
    session = AgentSession(store, "planner")
    with session.trace("book a trip") as t:
        helper = t.handoff("booker", "book the cheapest flight", inputs={"max": 200})
        helper.result({"booked": "AF123"})
        helper.end()
    assert Trace.open(store, t.id).subtraces() == [helper.root]
    assert helper.agent == "booker" and helper.root.body["run"].startswith(t.root.body["run"] + "/")
    assert Trace.open(store, helper.id).status() == "ok"


def test_two_runs_of_the_same_task_are_different_traces():
    store = MemoryStore()
    a = Trace.start(store, "planner", "same task")
    b = Trace.start(store, "planner", "same task")
    assert a.id != b.id
    same = Trace.start(store, "planner", "same task", run=a.root.body["run"])
    assert same.id == a.id  # naming the run reopens it
