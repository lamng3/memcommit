"""One store, many processes: a small HTTP server that owns the log, and a client store that mirrors it.

    python -m memcommit serve memory.db --port 7340        # the process that owns the store
    store = RemoteStore("http://127.0.0.1:7340")       # in each agent's process

The server holds the only `MemoryStore` that writes. It runs every commit, so
LSNs are assigned in one place and guards (scopes, provenance, a defender's
checks) are enforced for every process. Clients send a transaction's ops and
get back the records it wrote; everything else they read from their own copy
of the state, which follows the server's log (`refresh`, also done before
every commit and on demand).

`RemoteStore` is a `MemoryStore`: sessions, traces, the blackboard, tiers,
recall, and audit all work on it unchanged. Its observers see every commit,
from any process, when the records arrive.

Endpoints (JSON over HTTP, standard library only):

- `GET /records?after=N`: records after LSN N.
- `POST /transaction` with `{"agent", "ops": [{"op", "memory", "reason", "tier"}]}`:
  commit, and return the records; 409 with `{"problems"}` if a guard refuses.
- `GET /health`: the server's LSN.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .engines import Engine
from .records import Commit, Memory, Record
from .store import MemoryStore, Rejected, Transaction


class MemoryServer:
    """Serve `store` over HTTP. `start()` runs it in a background thread; `serve_forever()` in this one."""

    def __init__(self, store: MemoryStore, host: str = "127.0.0.1", port: int = 7340) -> None:
        self.store = store
        self.lock = threading.Lock()
        handler = _handler(self)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> MemoryServer:
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # Handlers ------------------------------------------------------------

    def records_after(self, lsn: int) -> list[dict[str, Any]]:
        with self.lock:
            return [r.to_json() for r in self.store.engine.records_after(lsn)]

    def commit(self, agent: str, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tx = Transaction(self.store, agent)
        tx.ops = [(o["op"], Memory.from_json(o["memory"]), o.get("reason", ""), o.get("tier", "")) for o in ops]
        with self.lock:
            commit = self.store._commit(tx)
        return [r.to_json() for r in commit.records] if commit else []


def _handler(server: MemoryServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # quiet by default
            return

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/records":
                after = int(parse_qs(url.query).get("after", ["0"])[0])
                self._send(200, server.records_after(after))
            elif url.path == "/health":
                self._send(200, {"lsn": server.store.lsn})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/transaction":
                self._send(404, {"error": "not found"})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            try:
                self._send(200, server.commit(body["agent"], body["ops"]))
            except Rejected as err:
                self._send(409, {"problems": err.problems})
            except (KeyError, ValueError) as err:
                self._send(400, {"error": str(err)})

    return Handler


class RemoteEngine(Engine):
    """Reads a server's log. Writes go through `RemoteStore`, which sends whole transactions."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            body = json.loads(err.read() or b"{}")
            if err.code == 409:
                raise Rejected(body.get("problems", [])) from None
            raise RuntimeError(f"memcommit server: {err.code} {body}") from None

    def append(self, records: Sequence[Record]) -> None:
        raise RuntimeError("a remote store commits whole transactions; use RemoteStore")

    def records(self) -> list[Record]:
        return self.records_after(0)

    def records_after(self, lsn: int) -> list[Record]:
        return [Record.from_json(r) for r in self.request("GET", f"/records?after={lsn}")]


class RemoteStore(MemoryStore):
    """A `MemoryStore` whose log lives on a `MemoryServer`. Commits run on the server."""

    def __init__(self, url: str, **options: Any) -> None:
        super().__init__(RemoteEngine(url), **options)

    def refresh(self) -> list[Record]:
        """Apply new records from the server, and tell observers about each transaction among them."""
        new = self.engine.records_after(self.lsn)
        for record in new:
            self.state.apply(record)
            self._txn = max(self._txn, record.txn)
        self._notify(new)
        return new

    def _notify(self, records: list[Record]) -> None:
        by_txn: dict[int, list[Record]] = {}
        for r in records:
            by_txn.setdefault(r.txn, []).append(r)
        self._committing = True
        try:
            for txn, group in by_txn.items():
                commit = Commit(txn, group[0].agent, tuple(group))
                for observer in self.observers:
                    observer(commit, self.state)
        finally:
            self._committing = False
        self._drain()

    def _commit(self, tx: Transaction) -> Commit | None:
        if self._committing:
            raise RuntimeError("a guard or observer tried to commit during a commit; call store.defer(work) instead")
        if not tx.ops:
            return None
        ops = [{"op": op, "memory": m.to_json(), "reason": reason, "tier": tier} for op, m, reason, tier in tx.ops]
        written = [
            Record.from_json(r) for r in self.engine.request("POST", "/transaction", {"agent": tx.agent, "ops": ops})
        ]
        self.refresh()
        if not written:
            return None
        return Commit(written[0].txn, tx.agent, tuple(written))
