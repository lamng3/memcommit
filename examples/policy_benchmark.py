"""Compare tier policies on an agent-shaped workload: how often is a memory an agent needs still in working memory?

python examples/policy_benchmark.py [--json results/policies.json] [--seed 0]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run from a clone without installing

from mneme import FIFO, LFU, LRU, AccessCount, Forgetting, ImportanceAtLeast, Scored, TwoQueue
from mneme.bench import compare, synthetic

POLICIES = {
    "FIFO": lambda: (FIFO(), None),
    "LRU": lambda: (LRU(), None),
    "LFU": lambda: (LFU(), None),
    "Scored": lambda: (Scored(), None),
    "Forgetting": lambda: (Forgetting(), None),
    "2Q": lambda: (TwoQueue(), None),
    "LRU + promote used": lambda: (LRU(), AccessCount(3)),
    "Scored + promote important": lambda: (Scored(), ImportanceAtLeast(0.8)),
}
CAPACITIES = (16, 32, 64)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="also write the reports to this file")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    events = synthetic(seed=args.seed)
    writes = sum(type(e).__name__ == "Write" for e in events)
    print(f"workload: {writes} writes, {len(events) - writes} uses (seed {args.seed})\n")
    reports = compare(events, POLICIES, CAPACITIES)
    print(f"{'policy':28} " + " ".join(f"{'cap ' + str(c):>9}" for c in CAPACITIES) + "   peak working (cap 32)")
    for name in POLICIES:
        rows = [r for r in reports if r.policy == name]
        peak = next(r.peak_working for r in rows if r.capacity == 32)
        print(f"{name:28} " + " ".join(f"{r.hit_rate:9.3f}" for r in rows) + f"   {peak}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        data = {"seed": args.seed, "workload": "synthetic", "reports": [r.to_json() for r in reports]}
        args.json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
