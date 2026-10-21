"""The memcommit command: serve a store, or check one.

python -m memcommit serve memory.db [--host 127.0.0.1] [--port 7340] [--snapshot state.json]
python -m memcommit verify memory.db
python -m memcommit compact memory.db [--retain-archive-seconds N]
python -m memcommit stats memory.db
python -m memcommit demo [--path demo.db]
python -m memcommit index memory.db [--agent assistant] [--max-chars 4000]
python -m memcommit search memory.db "where does alice live" [--where derived|episodic|all] [-k 5]
python -m memcommit dream memory.db [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import open as open_store
from .audit import verify
from .storage import ArchivedOlderThan, compact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memcommit", description="A transactional memory layer for agents.")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve a store to agents in other processes")
    serve.add_argument("path")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7340)
    serve.add_argument("--snapshot")
    check = sub.add_parser("verify", help="replay the log and report problems")
    check.add_argument("path")
    fold = sub.add_parser("compact", help="fold the log, purging old archived memories if asked")
    fold.add_argument("path")
    fold.add_argument("--retain-archive-seconds", type=float)
    stats = sub.add_parser("stats", help="counts by status, tier, and kind")
    stats.add_argument("path")
    demo = sub.add_parser("demo", help="run an end-to-end sample agent, with no model")
    demo.add_argument("--path", help="keep the demo's store here instead of a temporary file")
    index = sub.add_parser("index", help="print the catalog of notes")
    index.add_argument("path")
    index.add_argument("--agent", default="assistant")
    index.add_argument("--max-chars", type=int, default=4000)
    search = sub.add_parser("search", help="search notes, episodes, or both")
    search.add_argument("path")
    search.add_argument("query")
    search.add_argument("--agent", default="assistant")
    search.add_argument("--where", choices=["derived", "episodic", "all"], default="derived")
    search.add_argument("-k", type=int, default=5)
    dream = sub.add_parser("dream", help="run an offline consolidation pass over new sessions")
    dream.add_argument("path")
    dream.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import MemoryServer

        server = MemoryServer(open_store(args.path, snapshot=args.snapshot), args.host, args.port)
        print(f"memcommit serving {args.path} at {server.url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.stop()
        return 0
    if args.command == "demo":
        from .demo import run

        return run(args.path)
    store = open_store(args.path)
    if args.command == "index":
        from .knowledge import Knowledge

        print(Knowledge(store, args.agent).index(args.max_chars), end="")
        return 0
    if args.command == "search":
        from .knowledge import Knowledge

        print(json.dumps(Knowledge(store, args.agent).search(args.query, args.k, args.where), indent=2))
        return 0
    if args.command == "dream":
        from .dream import Dreamer

        report = Dreamer(store).run(dry_run=args.dry_run)
        verb = "would change" if args.dry_run else "changed"
        print(f"{verb} {len(report.changes)} notes from {len(report.sessions)} sessions")
        for change in report.changes:
            print(f"  {change.action} {change.path}: {change.reason}")
        return 0
    if args.command == "verify":
        problems = verify(store)
        print("\n".join(problems) or "ok")
        return 1 if problems else 0
    if args.command == "compact":
        retention = ArchivedOlderThan(args.retain_archive_seconds) if args.retain_archive_seconds else None
        report = compact(store, retention=retention)
        print(f"records {report.before} -> {report.after}; purged {len(report.purged)}")
        return 0
    counts: dict[str, dict[str, int]] = {"status": {}, "tier": {}, "kind": {}}
    for memory_id, memory in store.state.memories.items():
        for name, value in (
            ("status", store.status(memory_id)),
            ("tier", store.tier(memory_id)),
            ("kind", memory.kind),
        ):
            counts[name][value] = counts[name].get(value, 0) + 1
    print(json.dumps({"lsn": store.lsn, **counts}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
