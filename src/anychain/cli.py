"""Command line interface: `anychain explain <hash>`."""
import json
import sys
import time
from enum import Enum

import typer
from dotenv import load_dotenv

from anychain.answer import structured_answer
from anychain.bundle import InvalidHashError, build_bundle
from anychain.cache import BundleCache, NoCache, answer_key, cached_bundle
from anychain.collectors.rpc import RpcClient
from anychain.config import ConfigError, load_config
from anychain.events import PROBLEM_CAUSES, CheckEvent, NullEventLog, RunEvent, SqliteEventLog, event_log_for
from anychain.render import render_markdown
from anychain.writer import CheckedAnswer, WriterError, write_checked

app = typer.Typer(help="Explain and troubleshoot EVM transactions on any configured network.", no_args_is_help=True)


class Mode(str, Enum):
    support = "support"
    developer = "developer"
    auditor = "auditor"


@app.callback()
def _main() -> None:
    load_dotenv()


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def explain(
    tx_hash: str = typer.Argument(..., help="Transaction hash (0x + 64 hex)"),
    mode: Mode = typer.Option(None, help="Who the answer is for (default: from config)"),
    config: str = typer.Option(None, "--config", help="Path to network YAML (or set ANYCHAIN_CONFIG)"),
    as_json: bool = typer.Option(False, "--json", help="Print the structured answer as JSON (the summary included)"),
    as_evidence: bool = typer.Option(False, "--evidence", help="Print the evidence bundle as JSON"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM; print the evidence only"),
    fresh: bool = typer.Option(False, "--fresh", help="Fetch everything again, ignoring the cache"),
) -> None:
    """Explain what a transaction did, with a cited source for each fact."""
    if as_json and as_evidence:
        _fail("Use either --json (the structured answer) or --evidence (the evidence bundle), not both.")
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    log = _open_log(cfg)
    started = time.monotonic()
    store = cache_for(cfg)
    try:
        bundle, cache_state = cached_bundle(tx_hash.strip(), cfg, store, build_bundle, lambda: finality_rpc_for(cfg),
                                            fresh)
    except InvalidHashError as exc:
        _fail(str(exc))
    except Exception as exc:  # last line of defence: never a stack trace for the user
        _record(log, RunEvent.crash(cfg.network.name, tx_hash.strip(), "cli", _ms(started),
                                    f"{type(exc).__name__}: {exc}"))
        _fail(f"Unexpected error while collecting data ({type(exc).__name__}: {exc}). Please report it.")
    mode_name = (mode or Mode(cfg.assistant.default_mode)).value

    def event(**kw) -> RunEvent:
        return RunEvent.from_bundle(bundle, "cli", _ms(started), cache=cache_state, mode=mode_name, **kw)
    if as_evidence:  # (with --json: refused before collecting anything, below)
        _record(log, event())
        print(bundle.model_dump_json(indent=2))
        return

    def as_answer(summary: str | None, status: str) -> None:
        status = {"fail": "withheld"}.get(status, status)  # the writer's outcome names, as the JSON documents them
        print(json.dumps(structured_answer(bundle, summary, status, mode_name), indent=2, ensure_ascii=False))
    evidence_md = render_markdown(bundle)
    if no_llm or not bundle.items:
        _record(log, event(writer="skipped"))
        as_answer(None, "skipped" if no_llm else "no_evidence") if as_json else print(evidence_md)
        return
    final = cache_state in ("hit", "stored")  # a written answer is kept only for evidence that cannot change
    key = answer_key(bundle, cfg, mode_name) if final else None
    kept = None
    if key and not fresh:
        try:
            kept = store.get_answer(key)
        except Exception:  # a cache that cannot be read is skipped
            kept = None
    try:
        checked = CheckedAnswer(kept[0], "cached", []) if kept else write_checked(bundle, cfg, mode_name)
    except WriterError as exc:
        _record(log, event(writer="unavailable", usage=exc.usage))
        print(f"_(LLM unavailable: {exc}. " + ("The answer has no summary.)_" if as_json else
                                                  "Showing the evidence only.)_\n"), file=sys.stderr)
        as_answer(None, "unavailable") if as_json else print(evidence_md)
        return
    # What the model stated outside the evidence, kept for review: "retried" (fixed) or "fail" (withheld).
    check = (CheckEvent("answer_check", "fail" if checked.text is None else "retried", "; ".join(checked.problems)),
             ) if checked.problems else ()
    _record(log, event(checks=check, writer=checked.outcome, usage=checked.usage))
    if key and checked.text is not None and not kept:
        try:
            store.put_answer(key, checked.text, checked.outcome)
        except Exception as exc:
            print(f"(cache unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)
    if as_json:
        as_answer(checked.text, checked.outcome)
        return
    if checked.text is None:
        listed = "; ".join(checked.problems[:3])
        print(f"_(The written explanation was withheld: twice it stated things not in the evidence ({listed}). "
              "Showing the evidence only.)_\n")
        print(evidence_md)
        return
    print(checked.text + "\n\n---\n" + evidence_md)


def cache_for(cfg):
    """The answer cache (PHASE3 T0, D44); off by config, or when it cannot be opened (said, never fatal)."""
    if not cfg.cache.enabled:
        return NoCache()
    try:
        return BundleCache(cfg.cache.path, cfg.cache.max_age_days)
    except Exception as exc:
        print(f"(cache unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)
        return NoCache()


def finality_rpc_for(cfg) -> RpcClient:
    """The node asked whether a block is final before an answer is kept (only when a cache is on), with its own
    small time budget."""
    from anychain.collectors.http import Budget
    return RpcClient(cfg.rpc, budget=Budget(2 * cfg.rpc.timeout_s))


@app.command()
def batch(
    file: str = typer.Argument(..., help="A text file with one transaction hash per line (# starts a comment)"),
    config: str = typer.Option(None, "--config", help="Path to network YAML (or set ANYCHAIN_CONFIG)"),
    out: str = typer.Option("batch.jsonl", "--out", help="JSON Lines file: one structured answer per hash"),
    mode: Mode = typer.Option(None, help="Who the answers are for (default: from config)"),
    workers: int = typer.Option(4, min=1, max=16, help="How many transactions at a time"),
    fresh: bool = typer.Option(False, "--fresh", help="Fetch everything again, ignoring the cache"),
) -> None:
    """Explain many transactions (evidence and structured answer, no written summary). Resumes where it stopped:
    hashes already answered in --out are skipped (PHASE3 T0, R11)."""
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    try:
        lines = Path(file).read_text().splitlines()
    except OSError as exc:
        _fail(f"Cannot read {file}: {exc}")
    unique: dict[str, str] = {}  # the same hash in another letter case is the same transaction
    for line in lines:
        h = line.strip()
        if h and not h.startswith("#"):
            unique.setdefault(h.lower(), h)
    hashes = list(unique.values())
    done: set[str] = set()
    out_path = Path(out)
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "answer" in row:
                done.add(str(row.get("tx_hash")).lower())
    todo = [h for h in hashes if h.lower() not in done]
    log, store = _open_log(cfg), cache_for(cfg)
    rpc = None if isinstance(store, NoCache) else finality_rpc_for(cfg)
    mode_name = (mode or Mode(cfg.assistant.default_mode)).value

    def one(tx: str) -> dict:
        started = time.monotonic()
        try:
            bundle, state = cached_bundle(tx, cfg, store, build_bundle, rpc, fresh)
        except InvalidHashError as exc:
            return {"tx_hash": tx, "error": str(exc)}
        except Exception as exc:  # one transaction never stops the batch
            _record(log, RunEvent.crash(cfg.network.name, tx, "batch", _ms(started), f"{type(exc).__name__}: {exc}"))
            return {"tx_hash": tx, "error": f"{type(exc).__name__}: {exc}"}
        _record(log, RunEvent.from_bundle(bundle, "batch", _ms(started), writer="skipped", cache=state))
        return {"tx_hash": tx, "answer": structured_answer(bundle, None, "skipped", mode_name)}
    answered = failed = 0
    cut = out_path.exists() and out_path.stat().st_size and not out_path.read_bytes().endswith(b"\n")
    with ThreadPoolExecutor(workers) as pool, out_path.open("a") as sink:
        if cut:  # a run killed mid-line: the next line starts on its own (the cut one is skipped on resume)
            sink.write("\n")
        for row in pool.map(one, todo):  # in the file's order; each line written as soon as its turn comes
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            answered, failed = (answered + 1, failed) if "answer" in row else (answered, failed + 1)
    print(f"{_n(len(hashes), 'hash')}: {len(hashes) - len(todo)} already in {out}, {answered} answered now, "
          f"{failed} failed (written with their error; run again to retry them)")


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _open_log(cfg):
    """Opening the log (mkdir, schema) can fail too: a bad path, read-only disk, a locked file."""
    try:
        return event_log_for(cfg.storage.event_sink, cfg.storage.sqlite_path)
    except Exception as exc:
        print(f"(event log unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)
        return NullEventLog()


def _record(log, event: RunEvent) -> None:
    """Logging must never cost the user an answer."""
    try:
        log.record(event)
    except Exception as exc:
        print(f"(event log unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)


@app.command("log")
def show_log(
    config: str = typer.Option(None, "--config", help="Network YAML whose storage.sqlite_path holds the log"),
    hours: float = typer.Option(24, help="Look back this many hours"),
    network: str = typer.Option(None, help="Only this network (its network.name)"),
    problems: bool = typer.Option(False, "--problems", help="List recent answers with a problem instead of totals"),
    cause: str = typer.Option(None, help=f"With --problems: only this cause ({', '.join(PROBLEM_CAUSES)})"),
    limit: int = typer.Option(20, help="With --problems: how many answers"),
) -> None:
    """Query the event log: what went wrong, where, and how often."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    known = PROBLEM_CAUSES
    if cause and cause not in known:
        _fail(f"Unknown cause {cause!r}; problem causes are: {', '.join(known)}")
    if cfg.storage.event_sink != "sqlite":
        _fail(f"storage.event_sink is {cfg.storage.event_sink!r}: there is no local log to read")
    try:  # read-only: a query never creates or changes the log; relative paths are from the current directory
        log = SqliteEventLog(cfg.storage.sqlite_path, read_only=True)
    except FileNotFoundError as exc:
        _fail(f"{exc}. storage.sqlite_path is relative to the directory you run from.")
    since = time.time() - hours * 3600
    if problems:
        rows = log.problems(since, network, cause, limit)
        if not rows:
            print(f"No problems in the last {hours:g} h.")
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            print(f"{when}  {r['network']}  {r['tx_hash']}  {r['source']}  {r['outcome']}  {r['duration_ms']} ms")
            if r["error"]:
                print(f"    crash: {r['error']}")
            if r.get("writer") in ("withheld", "unavailable"):
                print(f"    written answer {r['writer']}")
            for g in r["gaps"]:
                print(f"    {g['cause']}: {g['topic']}: {g['why'][:140]}")
            for f in r["check_failures"]:
                print(f"    check failed: {f['name']}: {f['detail'][:140]}")
        return
    rows = log.summary(since, network)
    if not rows:
        print(f"No answers logged in the last {hours:g} h.")
    for r in rows:
        print(f"{r['network']}: {_n(r['answers'], 'answer')}, {r['degraded']} with a problem, "
              f"{r['crashes']} crashed, avg {r['avg_ms']} ms")
        if r["failed"]:  # unlabelled failures (logs from before PHASE2_5 T5) count in no label
            print(f"    {_n(r['failed'], 'failed transaction')}: CONFIRMED {r['confirmed']}, LIKELY {r['likely']}, "
                  f"UNKNOWN {r['unknown']}")
        if r["retried"] or r["withheld"] or r["unavailable"]:
            print(f"    written answer: {r['retried'] or 0} fixed on retry, {r['withheld'] or 0} withheld "
                  f"(stated things not in the evidence), {r['unavailable'] or 0} without a model")
        for g in r["gaps"]:
            if g["cause"] in PROBLEM_CAUSES:
                share = 100 * g["answers"] / r["answers"]
                print(f"    {g['cause']:<20} {g['topic']:<28} {_n(g['answers'], 'answer'):>12} ({share:.1f}%)")
        for f in r["check_failures"]:
            print(f"    check failed       {f['name']:<28} {f['n']:>5}")


@app.command("metrics")
def show_metrics(
    config: str = typer.Option(None, "--config", help="Network YAML whose storage.sqlite_path holds the log"),
    hours: float = typer.Option(24 * 30, help="Look back this many hours"),
) -> None:
    """Usage metrics from the event log, each printed with the SQL that computes it (PHASE3 T1, R6)."""
    from anychain.metrics import QUERIES, run_queries
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    if cfg.storage.event_sink != "sqlite":
        _fail(f"storage.event_sink is {cfg.storage.event_sink!r}: there is no local log to read")
    try:
        log = SqliteEventLog(cfg.storage.sqlite_path, read_only=True)
    except FileNotFoundError as exc:
        _fail(f"{exc}. storage.sqlite_path is relative to the directory you run from.")
    since = time.time() - hours * 3600
    print(f"Answers given with explain and the API in the last {hours:g} h, all networks in this log "
          f"({cfg.storage.sqlite_path}); the acceptance run, batches and answers logged before these metrics existed "
          "are left out.\n")
    for (title, sql), rows in zip(QUERIES, run_queries(log, since)):
        print(f"## {title}\n\n```sql\n{sql.strip()}\n```\n")
        if isinstance(rows, set):
            print(f"(this log has no {', '.join(sorted(rows))} column yet: it was written before them; the next "
                  "explain adds it, and answers from then on are counted)\n")
            continue
        if not rows:
            print("(no answers in this period)\n")
            continue
        names = list(rows[0])
        print(" | ".join(names))
        for row in rows:
            print(" | ".join(_cell(n, row[n]) for n in names))
        print()


def _cell(name: str, value) -> str:
    if value is None:
        return ""
    return f"{value:.1f}%" if name in ("share", "satisfaction") else str(value)


repos_app = typer.Typer(help="Contract repositories cited as source (PHASE2 T6).")
app.add_typer(repos_app, name="repos")


@repos_app.command("sync")
def repos_sync(config: str = typer.Option(None, "--config", help="Network YAML whose repos to download")) -> None:
    """Download each configured repo at its commit into storage.cache_dir (explain only reads that cache)."""
    from anychain.collectors.http import CollectorError
    from anychain.collectors.repo import RepoCache
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    if not cfg.repos:
        print("No repos configured for this network.")
        return
    cache, failed = RepoCache(cfg.storage.cache_dir), False
    for repo_cfg in cfg.repos:
        try:
            commit = cache.sync(repo_cfg)
            print(f"{repo_cfg.url} @ {commit}: ok")
        except CollectorError as exc:
            failed = True
            typer.secho(f"{repo_cfg.url}: {exc}", fg=typer.colors.RED, err=True)
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
