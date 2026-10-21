import json

from memcommit import Knowledge, Session
from memcommit import open as open_store
from memcommit.__main__ import main
from memcommit.demo import run


def test_the_demo_runs_end_to_end_and_the_record_verifies():
    lines: list[str] = []
    assert run(out=lines.append) == 0
    text = "\n".join(lines)
    assert "city note: alice lives in Paris." in text
    assert "the trip plan built on the old note is now stale" in text
    assert "is now stale" in text and "verify: ok" in text
    assert "I do not know yet." in text  # a question memory cannot answer is not answered with a wrong note


def test_the_cli_inspects_and_consolidates_a_store(tmp_path, capsys):
    path = tmp_path / "memory.db"
    store = open_store(path)
    with Session.start(store, "assistant", user="alice") as session:
        session.user_says("I live in Lyon.")
    assert main(["dream", str(path), "--dry-run"]) == 0
    assert "would change 1 notes" in capsys.readouterr().out
    assert Knowledge(open_store(path), "assistant").read("user/alice/city") is None
    assert main(["dream", str(path)]) == 0
    assert main(["index", str(path)]) == 0
    assert "- city: alice lives in Lyon." in capsys.readouterr().out
    assert main(["search", str(path), "where does alice live"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["path"] == "user/alice/city"
    assert main(["demo", "--path", str(tmp_path / "demo.db")]) == 0
