"""Does memory help? Score every memory configuration on a synthetic multi-session workload.

    python examples/memory_benchmark.py

Writes results/memory/<date>.json and prints the tables that results/memory/README.md reports. The workload is
synthetic and no model is involved: see `memcommit.evaluation`.
"""

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run from a clone without installing

from memcommit.evaluation import evaluate, make_workload, table  # noqa: E402

USERS = 20
SESSIONS = [8, 32, 64]  # sessions per user: the history an agent has to remember
SEEDS = [0, 1, 2]


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def main() -> None:
    report: dict[str, object] = {"users": USERS, "seeds": SEEDS, "sessions": {}}
    for sessions in SESSIONS:
        runs = [evaluate(make_workload(USERS, sessions, seed)) for seed in SEEDS]
        merged = []
        for position, first in enumerate(runs[0]):
            same = [run[position] for run in runs]
            first.accuracy = mean([r.accuracy for r in same])
            first.by_attribute = {a: mean([r.by_attribute[a] for r in same]) for a in first.by_attribute}
            first.context_words = mean([r.context_words for r in same])
            first.records = round(mean([r.records for r in same]))
            merged.append(first)
        print(f"\n### {sessions} sessions per user ({USERS * sessions} sessions, mean of {len(SEEDS)} seeds)\n")
        print(table(merged))
        report["sessions"][str(sessions)] = [  # type: ignore[index]
            {
                "name": r.name,
                "accuracy": round(r.accuracy, 4),
                "by_attribute": {a: round(v, 4) for a, v in r.by_attribute.items()},
                "context_words": round(r.context_words, 1),
                "log_records": r.records,
            }
            for r in merged
        ]
    out = ROOT / "results" / "memory" / f"{date.today().isoformat()}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
