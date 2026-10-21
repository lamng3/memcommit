from memcommit.evaluation import Context, Question, answer, evaluate, make_workload


def test_the_workload_is_deterministic_and_asks_for_current_values():
    a, b = make_workload(5, 6, seed=3), make_workload(5, 6, seed=3)
    assert a.sessions == b.sessions and a.questions == b.questions
    assert len(a.questions) == 5 * 4
    moved = [q for q in a.questions if q.attribute == "city" and len(q.values) == 2]
    assert all(q.gold == q.values[1] for q in moved)


def test_the_reader_wants_the_latest_value_mentioned():
    question = Question("u", "city", "Where?", "Paris", ("Lyon", "Paris"))
    assert answer(Context([("episodic", "I live in Lyon.", 1), ("episodic", "I moved to Paris.", 2)]), question)
    assert not answer(Context([("episodic", "I moved to Paris.", 1), ("episodic", "I live in Lyon.", 2)]), question)
    assert not answer(Context(), question)


def test_memory_helps_and_costs_less_than_the_full_history():
    results = {r.name: r for r in evaluate(make_workload(8, 8, seed=0))}
    assert results["none"].accuracy == 0
    assert results["episodic"].accuracy > 0.8
    assert results["derived, dreaming"].accuracy > results["derived, online tools"].accuracy
    assert results["episodic + derived"].context_words < results["full context"].context_words / 3


def test_with_perfect_extraction_the_notes_answer_everything_cleanly():
    results = {r.name: r for r in evaluate(make_workload(8, 8, seed=0))}
    assert results["derived, dreaming, oracle extractor"].accuracy == 1.0
    assert results["derived, dreaming, oracle extractor"].accuracy > results["derived, dreaming"].accuracy
