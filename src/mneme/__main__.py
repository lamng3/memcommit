"""The mneme command: serve a store, or check one.

python -m mneme serve memory.db [--host 127.0.0.1] [--port 7340] [--snapshot state.json]
python -m mneme verify memory.db
python -m mneme compact memory.db [--retain-archive-seconds N]
python -m mneme stats memory.db
"""

from __future__ import annotations

import argparse
import json
import sys

from . import open as open_store
from .audit import verify
from .storage import ArchivedOlderThan, compact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mneme", description="A transactional memory layer for agents.")
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
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import MemoryServer

        server = MemoryServer(open_store(args.path, snapshot=args.snapshot), args.host, args.port)
        print(f"mneme serving {args.path} at {server.url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.stop()
        return 0
    store = open_store(args.path)
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
