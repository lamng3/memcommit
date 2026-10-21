import pytest

from memcommit import Kind, MemoryStore, Rejected, Schema, check_body


def schema(strict: bool = False) -> Schema:
    return Schema(
        Kind("question", body={"type": "object", "required": ["text"]}),
        Kind(
            "answer",
            description="what an agent concluded",
            body={
                "type": "object",
                "required": ["text", "confidence"],
                "properties": {
                    "text": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "additionalProperties": False,
            },
            scopes=("shared", "agent"),
            requires=("question",),
            tier="long",
            importance=0.5,
        ),
        strict=strict,
    )


def test_a_conforming_write_passes_and_gets_the_declared_defaults():
    store = MemoryStore()
    s = schema()
    s.attach(store)
    q = s.put(store, "asker", "question", "q1", {"text": "why?"})
    a = s.put(store, "answerer", "answer", "q1", {"text": "because", "confidence": 0.9}, derived_from=[q.id])
    assert store.tier(a.id) == "long" and store.tier(q.id) == "short"
    assert store.get(a.id).importance == 0.5


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ({"text": "x"}, "confidence is required"),
        ({"text": "x", "confidence": 2}, "at most 1"),
        ({"text": "x", "confidence": "high"}, "must be number"),
        ({"text": "x", "confidence": 0.5, "extra": 1}, "extra is not allowed"),
        ("not an object", "must be object"),
    ],
)
def test_a_body_out_of_line_with_its_kind_is_rejected(body, problem):
    store = MemoryStore()
    s = schema()
    s.attach(store)
    q = s.put(store, "asker", "question", "q1", {"text": "why?"})
    with pytest.raises(Rejected, match=problem):
        store.put("answerer", "answer", "q1", body, derived_from=[q.id])
    assert store.find(kind="answer") == []


def test_requires_names_the_kinds_a_memory_must_derive_from():
    store = MemoryStore()
    s = schema()
    s.attach(store)
    with pytest.raises(Rejected, match="derived from a question"):
        store.put("answerer", "answer", "q1", {"text": "x", "confidence": 1})
    with store.transaction("both") as tx:  # a premise written in the same commit counts
        q = tx.put("question", "q2", {"text": "?"})
        tx.put("answer", "q2", {"text": "x", "confidence": 1}, derived_from=[q.id])


def test_scopes_limit_where_a_kind_is_written():
    store = MemoryStore()
    s = Schema(Kind("note", scopes=("agent",)))
    s.attach(store)
    store.put("planner", "note", "n", "x", scope="planner/drafts")
    with pytest.raises(Rejected, match="scope 'shared'"):
        store.put("planner", "note", "m", "x")


def test_strict_rejects_undeclared_kinds_but_not_the_ones_memcommit_writes():
    store = MemoryStore()
    schema(strict=True).attach(store)
    with pytest.raises(Rejected, match="not a declared kind"):
        store.put("a", "gossip", "g", "x")
    import memcommit

    with memcommit.trace(store, "a", "task") as t:
        t.thought("fine")


def test_the_schema_is_recorded_and_a_new_version_supersedes_the_old():
    store = MemoryStore()
    first = schema()
    first.attach(store)
    assert Schema.load(store).version == first.version
    second = Schema(*first.kinds.values(), Kind("fact"))
    second.record(store)
    assert Schema.load(store).version == second.version
    assert len(store.find(kind="schema")) == 1
    assert first.record(store) is not None  # reverting writes the old one back live
    assert Schema.load(store).version == first.version


def test_audit_finds_data_written_before_the_schema():
    store = MemoryStore()
    bad = store.put("a", "question", "q", {"no": "text"})
    assert schema().audit(store) == {bad.id: ["body.text is required"]}


def test_a_schema_must_not_require_an_undeclared_kind():
    with pytest.raises(ValueError, match="undeclared"):
        Schema(Kind("answer", requires=("question",)))


def test_describe_lists_every_kind():
    text = schema().describe()
    assert "## answer: what an agent concluded" in text and "derived from `question`" in text


def test_check_body_covers_the_subset():
    assert check_body([1, 2], {"type": "array", "items": {"type": "integer"}, "maxItems": 1}) == [
        "body allows at most 1 items"
    ]
    assert check_body(True, {"type": "integer"}) == ["body must be integer, not boolean"]
    assert check_body("a", {"enum": ["b"]}) == ["body must be one of ['b']"]
