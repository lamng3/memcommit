"""OntoDB as a storage engine: the memory log as RDF in a durable OntoDB database.

Each mneme transaction is one `INSERT DATA`, which a persistent OntoDB
database runs as one transaction, forced to its write-ahead log before the
shell returns. Nothing is ever deleted: the log is append-only, and status
comes from replay, as with every engine.

Two kinds of node are written:

    <urn:mneme:lsn:7>     mn:lsn 7 ; mn:txn 3 ; mn:op "stale" ; mn:agent "mneme" ;
                          mn:memory <urn:mneme:m:...> ; mn:record "<the record as JSON>"
    <urn:mneme:m:...>     mn:kind "answer" ; mn:key "..." ; mn:source "moira.answerer" ;
                          mn:scope "shared" ; mn:body "<JSON>" ; mn:derivedFrom <urn:mneme:m:...>

The record nodes are what the engine reads back. The memory nodes are for
people and other tools: provenance is a SPARQL query, such as every memory
derived from one ontology version.

OntoDB is driven through its shell (`ontodb <dir>`), the interface OntoDB
promises to keep. This module depends on no OntoDB code.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote, unquote

from ..engines import Engine, register
from ..records import Record, canonical, is_memory_ref

PROMPT = re.compile(r"^OntoDB> ", re.MULTILINE)
NS = "urn:mneme:"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
TERM = re.compile(r'<[^>]*>|"(?:[^"\\]|\\.)*"(?:\^\^<[^>]*>|@[A-Za-z-]+)?|_:\S+')
TIMEOUT_SECONDS = 60


class OntoDBError(RuntimeError):
    pass


def predicate(name: str) -> str:
    return f"<{NS}{name}>"


def ref_iri(ref: str) -> str:
    if is_memory_ref(ref):
        return f"<{NS}{ref}>"
    return f"<{NS}ref:{quote(ref, safe='')}>"


def iri_ref(iri: str) -> str:
    """Inverse of `ref_iri`, for an IRI without angle brackets."""
    body = iri[len(NS) :]
    return unquote(body[len("ref:") :]) if body.startswith("ref:") else body


def literal(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def integer(n: int) -> str:
    return f'"{n}"^^<{XSD_INTEGER}>'


def term_value(term: str) -> str:
    """A literal's text, or an IRI without its brackets."""
    if term.startswith("<"):
        return term[1:-1]
    if term.startswith('"'):
        end = term.rindex('"')
        return json.loads(term[: end + 1])
    return term


class OntoDBShell:
    """Runs scripts through `ontodb <dir>` and parses what it prints."""

    def __init__(self, binary: str | Path, directory: str | Path, timeout: float = TIMEOUT_SECONDS) -> None:
        self.binary = Path(binary)
        self.directory = Path(directory)
        self.timeout = timeout

    def run(self, script: str) -> str:
        proc = subprocess.run(
            [str(self.binary), str(self.directory)],
            input=script + "\n\\quit\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=self.timeout,
        )
        _, _, rest = proc.stdout.partition("OntoDB> ")  # everything before the first prompt is the open report
        body = PROMPT.sub("", rest).strip()
        if proc.returncode != 0 or re.search(r"^(error|not built yet)", body, re.MULTILINE):
            raise OntoDBError(f"ontodb failed ({proc.returncode}): {body} {proc.stderr.strip()}")
        return body

    def select(self, sparql: str) -> list[list[str]]:
        """Rows of a SELECT, each a list of terms as OntoDB prints them."""
        rows: list[list[str]] = []
        in_rows = False
        for line in self.run(sparql).splitlines():
            if not in_rows:
                in_rows = line.startswith("?")
                continue
            if re.fullmatch(r"\(\d+ rows?\)", line.strip()):
                break
            rows.append(TERM.findall(line))
        return rows

    def verify(self) -> str:
        return self.run("\\verify")


@register("ontodb")
class OntoDBEngine(Engine):
    def __init__(self, binary: str | Path, directory: str | Path) -> None:
        self.shell = OntoDBShell(binary, directory)

    @staticmethod
    def triples(record: Record) -> list[str]:
        m = record.memory
        node = f"<{NS}lsn:{record.lsn}>"
        out = [
            f"{node} {predicate('lsn')} {integer(record.lsn)} .",
            f"{node} {predicate('txn')} {integer(record.txn)} .",
            f"{node} {predicate('op')} {literal(record.op)} .",
            f"{node} {predicate('agent')} {literal(record.agent)} .",
            f"{node} {predicate('memory')} {ref_iri(m.id)} .",
            f"{node} {predicate('record')} {literal(canonical(record.to_json()))} .",
        ]
        memory = ref_iri(m.id)
        out += [
            f"{memory} {predicate('kind')} {literal(m.kind)} .",
            f"{memory} {predicate('key')} {literal(m.key)} .",
            f"{memory} {predicate('source')} {literal(m.source)} .",
            f"{memory} {predicate('scope')} {literal(m.scope)} .",
            f"{memory} {predicate('body')} {literal(canonical(m.body))} .",
        ]
        out += [f"{memory} {predicate('derivedFrom')} {ref_iri(ref)} ." for ref in m.derived_from]
        return out

    def append(self, records: Sequence[Record]) -> None:
        body = "\n".join(f"  {t}" for r in records for t in self.triples(r))
        self.shell.run(f"INSERT DATA {{\n{body}\n}}")

    def records(self) -> list[Record]:
        if not self.shell.directory.exists():
            return []
        rows = self.shell.select(f"SELECT ?r ?j WHERE {{ ?r {predicate('record')} ?j . }}")
        records = [Record.from_json(json.loads(term_value(j))) for _, j in rows]
        return sorted(records, key=lambda r: r.lsn)

    def derived_from(self, ref: str) -> list[str]:
        """Memory ids derived directly from `ref`, by SPARQL over the stored memory nodes."""
        rows = self.shell.select(f"SELECT ?m WHERE {{ ?m {predicate('derivedFrom')} {ref_iri(ref)} . }}")
        return sorted(iri_ref(term_value(m)) for (m,) in rows)
