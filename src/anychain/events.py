"""Run log: one event per answer, so problems in production can be found and counted.

Every `explain` (and every acceptance/canary check) records what happened: which network, how long
it took, and every gap with its cause (models.GapCause). Gaps caused by a failing source or by our
own code are what an operator needs to see; gaps that are expected limits (no ABI, pending tx) are
kept too, so their rate can be watched.

Sinks are classes behind one interface, chosen by config (storage.event_sink):
  - SqliteEventLog: local, queryable with `anychain log` (this case).
  - In production the same RunEvent would be published to a queue (e.g. SQS) and a worker would
    aggregate it and alert. That sink is a design note only (docs/DECISIONS.md D24), not built.
"""
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from anychain.models import EvidenceBundle

# Causes that mean something is wrong (as opposed to an expected limit).
WRITER_PROBLEMS = ("withheld", "unavailable")  # the user got the evidence only
PROBLEM_CAUSES = ("processing_error", "config_error", "source_error", "source_unavailable", "source_behind")


@dataclass(frozen=True)
class GapEvent:
    topic: str
    cause: str
    why: str
    retryable: bool


@dataclass(frozen=True)
class CheckEvent:
    """One independent check of the answer against the node (acceptance run / daily canary)."""
    name: str
    status: str  # pass | fail | skip
    detail: str = ""


@dataclass(frozen=True)
class RunEvent:
    network: str
    tx_hash: str
    source: str  # cli | api | canary
    outcome: str  # ok | degraded | crash
    duration_ms: int
    status: str | None = None  # the transaction's status as answered
    facts: int = 0
    error: str | None = None  # crash message: the tool could not answer at all
    writer: str | None = None  # written answer: ok | retried | withheld | unavailable | skipped (None: not asked)
    diagnosis: str | None = None  # the failure's conclusion label: CONFIRMED | LIKELY | UNKNOWN (None: no failure)
    cache: str | None = None  # hit | stored | off | "not kept: <reason>" (PHASE3 T0)
    # PHASE3 T1 (R6, D45): what the metrics need
    mode: str | None = None  # support | developer | auditor
    rule: str | None = None  # the failure's own diagnosis rule (e.g. insufficient_balance)
    abi_source: str | None = None  # where the main call's ABI came from: explorer | repo_artifacts | repo_source |
    #                                signature_db | raw (None: no contract call)
    input_tokens: int | None = None  # the written answer's, all attempts, cache reads excluded (None: no model call,
    output_tokens: int | None = None  #   or not reported)
    cache_read_tokens: int | None = None  # input read from the provider's prompt cache
    cache_write_tokens: int | None = None  # input written to it
    cost_usd: float | None = None  # as Claude Code reports it: list price (costBasis "list"), not what a subscription
    #                                pays; the APIs report tokens only
    feedback: str | None = None  # up | down, from the page (set later with set_feedback)
    gaps: tuple[GapEvent, ...] = ()
    checks: tuple[CheckEvent, ...] = ()
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_bundle(cls, bundle: EvidenceBundle, source: str, duration_ms: int,
                    checks: tuple[CheckEvent, ...] = (), ts: float | None = None,
                    writer: str | None = None, cache: str | None = None, mode: str | None = None,
                    usage: dict | None = None) -> "RunEvent":
        gaps = tuple(GapEvent(g.what, g.cause, g.why, g.retryable) for g in bundle.gaps)
        problem = (any(g.cause in PROBLEM_CAUSES for g in gaps) or any(c.status == "fail" for c in checks)
                   or writer in WRITER_PROBLEMS)
        return cls(network=bundle.network, tx_hash=bundle.tx_hash, source=source,
                   outcome="degraded" if problem else "ok", duration_ms=duration_ms, status=bundle.status,
                   facts=len(bundle.items), gaps=gaps, checks=checks, writer=writer,
                   diagnosis=_diagnosis_label(bundle), cache=cache, mode=mode, rule=_diagnosis_rule(bundle),
                   abi_source=abi_source_of(bundle), input_tokens=(usage or {}).get("input_tokens"),
                   output_tokens=(usage or {}).get("output_tokens"),
                   cache_read_tokens=(usage or {}).get("cache_read_tokens"),
                   cache_write_tokens=(usage or {}).get("cache_write_tokens"), cost_usd=(usage or {}).get("cost_usd"),
                   **({"ts": ts} if ts else {}))

    @classmethod
    def crash(cls, network: str, tx_hash: str, source: str, duration_ms: int, error: str,
              ts: float | None = None) -> "RunEvent":
        return cls(network=network, tx_hash=tx_hash, source=source, outcome="crash",
                   duration_ms=duration_ms, error=error, **({"ts": ts} if ts else {}))


class EventLog(Protocol):
    def record(self, event: RunEvent, replace: bool = False) -> int | None: ...


class NullEventLog:
    """Logging switched off (storage.event_sink: none)."""

    def record(self, event: RunEvent, replace: bool = False) -> None:
        return None

    def set_feedback(self, run_id: int, feedback: str) -> bool:
        return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, network TEXT NOT NULL, tx_hash TEXT NOT NULL,
    source TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL,
    status TEXT, facts INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS gaps (
    run_id INTEGER NOT NULL REFERENCES runs(id), topic TEXT NOT NULL, cause TEXT NOT NULL,
    why TEXT NOT NULL, retryable INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS checks (
    run_id INTEGER NOT NULL REFERENCES runs(id), name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS runs_ts ON runs(ts);
CREATE INDEX IF NOT EXISTS gaps_run ON gaps(run_id);
CREATE INDEX IF NOT EXISTS checks_run ON checks(run_id);
"""


def _diagnosis_rule(bundle: EvidenceBundle) -> str | None:
    """The failure's own conclusion's rule; a failure answered from the node only has the replay's (as the label
    does, `_diagnosis_label`)."""
    rules = [(e.data.get("from_replay", False), e.data.get("rule")) for e in bundle.items if e.kind == "diagnosis"]
    own = [rule for from_replay, rule in rules if not from_replay]
    return own[0] if own else rules[0][1] if rules else None


ABI_BUCKETS = {"explorer": "explorer", "repo_artifact": "repo_artifacts", "repo_pinned": "repo_source",
               "repo_match": "repo_candidate"}


def abi_source_of(bundle: EvidenceBundle) -> str | None:
    """Where the ABI of the transaction's own call came from, in the original plan's buckets: explorer;
    repo_artifacts or repo_source (the contract pinned in address_map); repo_candidate (an unpinned selector match,
    not confirmed); signature_db (not decoded, signature-database candidates only); raw (not decoded, nothing).
    None: no contract function was called (a plain transfer, data sent to an account without code or to a
    precompile, a creation)."""
    call = next((e for e in bundle.items if e.kind == "call"), None)
    if call is None:
        return None
    if call.data.get("function"):
        return ABI_BUCKETS.get(call.data.get("abi_origin") or "explorer", "explorer")
    selector = call.data.get("selector")
    if not selector:
        return None
    candidates = [e for e in bundle.items if e.kind == "candidate" and e.data.get("selector") == selector]
    return "signature_db" if candidates else "raw"


def _diagnosis_label(bundle: EvidenceBundle) -> str | None:
    """The label of the failure's own conclusion; a failure answered from the node only has the replay's
    conclusion, or none (UNKNOWN). None: the transaction did not fail."""
    labels = [(e.data.get("from_replay", False), e.data.get("label")) for e in bundle.items if e.kind == "diagnosis"]
    own = [label for from_replay, label in labels if not from_replay]
    if own:
        return own[0]
    if labels:
        return labels[0][1]
    return "UNKNOWN" if bundle.status == "failed" else None


RUN_COLUMNS = ["ts", "network", "tx_hash", "source", "outcome", "duration_ms", "status", "facts", "error"]
# Columns added after the first version, in order; a log without them gets them on open (written ones only).
ADDED_COLUMNS = [("writer", "TEXT"), ("diagnosis", "TEXT"), ("cache", "TEXT"), ("mode", "TEXT"), ("rule", "TEXT"),
                 ("abi_source", "TEXT"), ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
                 ("cost_usd", "REAL"), ("feedback", "TEXT"), ("cache_read_tokens", "INTEGER"),
                 ("cache_write_tokens", "INTEGER")]


class SqliteEventLog:
    """Local event log in one SQLite file (storage.sqlite_path)."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(f"no event log at {self.path.resolve()}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            for name, kind in ADDED_COLUMNS:  # logs created before these columns
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")

    @contextmanager
    def _connect(self):
        """One short-lived connection per call (safe across threads); commits, then always closes."""
        target = f"{self.path.resolve().as_uri()}?mode=ro" if self.read_only else str(self.path)
        with closing(sqlite3.connect(target, timeout=30, uri=self.read_only)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def has(self, network: str, source: str, tx_hash: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM runs WHERE network = ? AND source = ? AND tx_hash = ? LIMIT 1",
                              (network, source, tx_hash)).fetchone() is not None

    def record(self, event: RunEvent, replace: bool = False) -> int:
        """Add one answer; returns its id. `replace`: drop earlier rows for the same (network, source, tx), so a re-run or
        re-check of a canary answer supersedes it instead of being counted twice."""
        with self._connect() as db:
            if replace:
                old = [r[0] for r in db.execute("SELECT id FROM runs WHERE network = ? AND source = ? AND tx_hash = ?",
                                                (event.network, event.source, event.tx_hash))]
                for table in ("gaps", "checks"):
                    db.executemany(f"DELETE FROM {table} WHERE run_id = ?", [(i,) for i in old])
                db.executemany("DELETE FROM runs WHERE id = ?", [(i,) for i in old])
            names = RUN_COLUMNS + [name for name, _kind in ADDED_COLUMNS]
            run_id = db.execute(f"INSERT INTO runs ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                                [getattr(event, n) for n in names]).lastrowid
            db.executemany("INSERT INTO gaps (run_id, topic, cause, why, retryable) VALUES (?, ?, ?, ?, ?)",
                           [(run_id, g.topic, g.cause, g.why, int(g.retryable)) for g in event.gaps])
            db.executemany("INSERT INTO checks (run_id, name, status, detail) VALUES (?, ?, ?, ?)",
                           [(run_id, c.name, c.status, c.detail) for c in event.checks])
        return run_id

    def set_feedback(self, run_id: int, feedback: str) -> bool:
        """The reader's thumbs up or down on one answer. False: no such answer."""
        if feedback not in ("up", "down"):
            raise ValueError("feedback is up or down")
        with self._connect() as db:
            return db.execute("UPDATE runs SET feedback = ? WHERE id = ?", (feedback, run_id)).rowcount == 1

    # ---- queries (used by `anychain log`) --------------------------------

    def recent(self, limit: int = 20) -> list[dict]:
        """The most recent runs, newest first."""
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY ts DESC, id DESC LIMIT ?", (limit,))]

    def summary(self, since_ts: float, network: str | None = None) -> list[dict]:
        """Per network: answers, crashes, degraded answers, and each problem cause with its count."""
        where, args = "r.ts >= ?", [since_ts]
        if network:
            where, args = where + " AND r.network = ?", args + [network]
        with self._connect() as db:
            # a log from before PHASE2 T1, opened read-only, has no writer column: read it as empty
            columns = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
            w = "writer" if "writer" in columns else "NULL"
            d = "diagnosis" if "diagnosis" in columns else "NULL"  # a log from before PHASE2_5 T5
            runs = db.execute(
                f"SELECT network, COUNT(*) AS answers, SUM(outcome = 'crash') AS crashes,"
                f" SUM(outcome = 'degraded') AS degraded, CAST(AVG(duration_ms) AS INTEGER) AS avg_ms,"
                f" SUM({w} = 'retried') AS retried, SUM({w} = 'withheld') AS withheld,"
                f" SUM({w} = 'unavailable') AS unavailable, COALESCE(SUM({d} = 'CONFIRMED'), 0) AS confirmed,"
                f" COALESCE(SUM({d} = 'LIKELY'), 0) AS likely, COALESCE(SUM({d} = 'UNKNOWN'), 0) AS unknown,"
                f" COALESCE(SUM(status = 'failed'), 0) AS failed"
                f" FROM runs r WHERE {where} GROUP BY network ORDER BY network", args).fetchall()
            causes = db.execute(
                f"SELECT r.network, g.cause, g.topic, COUNT(DISTINCT r.id) AS answers FROM gaps g"
                f" JOIN runs r ON r.id = g.run_id WHERE {where} GROUP BY r.network, g.cause, g.topic"
                f" ORDER BY r.network, answers DESC", args).fetchall()
            fails = db.execute(
                f"SELECT r.network, c.name, COUNT(*) AS n FROM checks c JOIN runs r ON r.id = c.run_id"
                f" WHERE {where} AND c.status = 'fail' GROUP BY r.network, c.name", args).fetchall()
        out = []
        for row in runs:
            out.append({**dict(row),
                        "gaps": [dict(c) for c in causes if c["network"] == row["network"]],
                        "check_failures": [dict(f) for f in fails if f["network"] == row["network"]]})
        return out

    def problems(self, since_ts: float, network: str | None = None, cause: str | None = None,
                 limit: int = 20) -> list[dict]:
        """Most recent answers that crashed, failed a check, had their written answer withheld or
        unavailable, or have a gap with a problem cause."""
        causes = (cause,) if cause else PROBLEM_CAUSES
        marks = ",".join("?" * len(causes))
        where, args = "r.ts >= ?", [since_ts]
        if network:
            where, args = where + " AND r.network = ?", args + [network]
        with self._connect() as db:
            has_writer = "writer" in {r[1] for r in db.execute("PRAGMA table_info(runs)")}
            writer = "r.writer IN ('withheld', 'unavailable') OR " if has_writer else ""
            rows = db.execute(
                f"SELECT r.* FROM runs r WHERE {where} AND ("
                + ("" if cause else "r.outcome = 'crash' OR " + writer + "EXISTS (SELECT 1 FROM checks c "
                   "WHERE c.run_id = r.id AND c.status = 'fail') OR ")
                + f"EXISTS (SELECT 1 FROM gaps g WHERE g.run_id = r.id AND g.cause IN ({marks})))"
                f" ORDER BY r.ts DESC LIMIT ?", args + list(causes) + [limit]).fetchall()
            result = []
            for r in rows:
                gaps = db.execute("SELECT topic, cause, why FROM gaps WHERE run_id = ? AND cause IN "
                                  f"({marks})", [r["id"], *causes]).fetchall()
                fails = db.execute("SELECT name, detail FROM checks WHERE run_id = ? AND status = 'fail'",
                                   [r["id"]]).fetchall()
                result.append({**dict(r), "gaps": [dict(g) for g in gaps], "check_failures": [dict(f) for f in fails]})
        return result


def event_log_for(sink: str, sqlite_path: str) -> EventLog:
    if sink == "sqlite":
        return SqliteEventLog(sqlite_path)
    return NullEventLog()


def as_json(event: RunEvent) -> str:
    """The wire format a queue sink would publish (same fields as the tables)."""
    return json.dumps(asdict(event))
