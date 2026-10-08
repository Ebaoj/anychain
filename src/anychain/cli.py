"""Command line interface: `anychain explain <hash>`."""
import sys
import time
from enum import Enum

import typer
from dotenv import load_dotenv

from anychain.bundle import InvalidHashError, build_bundle
from anychain.config import ConfigError, load_config
from anychain.events import PROBLEM_CAUSES, NullEventLog, RunEvent, SqliteEventLog, event_log_for
from anychain.render import render_markdown
from anychain.writer import WriterError, write_explanation

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
    as_json: bool = typer.Option(False, "--json", help="Print the evidence bundle as JSON"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM; print the evidence only"),
) -> None:
    """Explain what a transaction did, with a cited source for each fact."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    log = _open_log(cfg)
    started = time.monotonic()
    try:
        bundle = build_bundle(tx_hash.strip(), cfg)
    except InvalidHashError as exc:
        _fail(str(exc))
    except Exception as exc:  # last line of defence: never a stack trace for the user
        _record(log, RunEvent.crash(cfg.network.name, tx_hash.strip(), "cli", _ms(started),
                                    f"{type(exc).__name__}: {exc}"))
        _fail(f"Unexpected error while collecting data ({type(exc).__name__}: {exc}). Please report it.")
    _record(log, RunEvent.from_bundle(bundle, "cli", _ms(started)))

    if as_json:
        print(bundle.model_dump_json(indent=2))
        return
    evidence_md = render_markdown(bundle)
    if no_llm or not bundle.items:
        print(evidence_md)
        return
    try:
        answer = write_explanation(bundle, cfg, (mode or Mode(cfg.assistant.default_mode)).value)
        print(answer + "\n\n---\n" + evidence_md)
    except WriterError as exc:
        print(f"_(LLM unavailable: {exc}. Showing the evidence only.)_\n", file=sys.stderr)
        print(evidence_md)


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
        for g in r["gaps"]:
            if g["cause"] in PROBLEM_CAUSES:
                share = 100 * g["answers"] / r["answers"]
                print(f"    {g['cause']:<20} {g['topic']:<28} {_n(g['answers'], 'answer'):>12} ({share:.1f}%)")
        for f in r["check_failures"]:
            print(f"    check failed       {f['name']:<28} {f['n']:>5}")


if __name__ == "__main__":
    app()
