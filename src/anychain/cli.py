"""Command line interface: `anychain explain <hash>`."""
import json
import sys
import time
from enum import Enum

import typer
from dotenv import load_dotenv

from anychain.answer import structured_answer
from anychain.bundle import InvalidHashError, build_bundle
from anychain.cache import BundleCache, NoCache, cached_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.rpc import RpcClient
from anychain.config import ConfigError, load_config
from anychain.events import PROBLEM_CAUSES, NullEventLog, RunEvent, SqliteEventLog, event_log_for
from anychain.render import render_markdown
from anychain.writer import write_checked

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
    question: str = typer.Option(None, "--question", help="Your question about it, answered first"),
) -> None:
    """Explain what a transaction did, with a cited source for each fact."""
    from anychain.service import NONE, SKIP, WRITE, Crash, answer_transaction
    if as_json and as_evidence:
        _fail("Use either --json (the structured answer) or --evidence (the evidence bundle), not both.")
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    mode_name = (mode or Mode(cfg.assistant.default_mode)).value
    write = NONE if as_evidence else SKIP if no_llm else WRITE
    clarified = None
    if write == WRITE and cfg.assistant.max_clarifying_questions > 0:  # not a hash: which transaction (PHASE4 R1)
        from anychain.collectors.http import Budget
        from anychain.triage import for_input
        asked = for_input(tx_hash, ExplorerClient(cfg.explorer, budget=Budget(cfg.assistant.time_budget_s)))
        if asked is not None:
            tx_hash = _ask(asked, as_json)
            clarified = {"kind": "which_transaction", "answer": tx_hash}

    def run(clarified):
        try:
            return answer_transaction(
                tx_hash, cfg, mode_name, write=write, fresh=fresh, source="cli", log=_open_log(cfg),
                store=cache_for(cfg), build=build_bundle, write_fn=write_checked,
                finality=lambda: finality_rpc_for(cfg), record=_record, question=question, clarified=clarified)
        except InvalidHashError as exc:
            _fail(str(exc))
        except Crash as exc:
            _fail(f"Unexpected error while collecting data ({exc}). Please report it.")
    result = run(clarified)
    if result.clarify is not None:  # one question, then the answer (PHASE4 D2)
        result = run({"kind": result.clarify.kind, "answer": _ask(result.clarify, as_json)})
    for note in result.notes:
        print(note, file=sys.stderr)
    if as_evidence:
        print(result.bundle.model_dump_json(indent=2))
        return
    if result.writer_error:
        print(f"_(LLM unavailable: {result.writer_error}. " + ("The answer has no summary.)_" if as_json else
                                                               "Showing the evidence only.)_\n"), file=sys.stderr)
    if as_json:
        print(json.dumps({**structured_answer(result.bundle, result.text, result.summary_status, mode_name),
                          "question": result.question, "clarify": None}, indent=2,
                         ensure_ascii=False))
        return
    evidence_md = render_markdown(result.bundle)
    if result.summary_status == "withheld":
        listed = "; ".join(result.problems[:3])
        print(f"_(The written explanation was withheld: twice it stated things not in the evidence ({listed}). "
              "Showing the evidence only.)_\n")
    if result.text is None:
        print(evidence_md)
        return
    print(result.text + "\n\n---\n" + evidence_md)


def _ask(clarify, as_json: bool) -> str:
    """Asks the clarifying question in the terminal and returns the answer (an option's id or typed text). Without a
    terminal to answer in, it prints the question (as JSON with --json) and stops with exit code 1: nothing was
    explained yet."""
    if not sys.stdin.isatty():
        if as_json:
            print(json.dumps({"clarify": clarify.to_dict()}, indent=2, ensure_ascii=False))
        else:
            print(clarify.question)
            for i, o in enumerate(clarify.options, 1):
                print(f"  {i}. {o.label} ({o.id})")
        raise typer.Exit(1)
    clean = lambda t: "".join(ch for ch in t if ch.isprintable())  # noqa: E731  explorer text: no terminal codes
    print(clean(clarify.question), file=sys.stderr)
    for i, o in enumerate(clarify.options, 1):
        print(f"  {i}. {clean(o.label)}", file=sys.stderr)
    reply = typer.prompt("Your answer (a number" + (", or " + clarify.free_text if clarify.free_text else "") + ")",
                         err=True).strip()
    if reply.isdigit() and 1 <= int(reply) <= len(clarify.options):
        return clarify.options[int(reply) - 1].id
    return reply


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
    hashes already answered in --out are skipped."""
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


def _record(log, event: RunEvent) -> int | None:
    """Logging must never cost the user an answer. Returns the row's id (None: not logged)."""
    try:
        return log.record(event)
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


@app.command()
def chat(
    tx_hash: str = typer.Argument(..., help="Transaction hash (0x + 64 hex)"),
    mode: Mode = typer.Option(None, help="Who the answers are for (default: from config)"),
    config: str = typer.Option(None, "--config", help="Path to network YAML (or set ANYCHAIN_CONFIG)"),
) -> None:
    """Ask follow-up questions about one transaction. The model may ask for read-only data (a contract's
    state, a function's code, another transaction); every answer is checked against the evidence."""
    from anychain.api import chat_tools
    from anychain.chat import ChatSession, turn_event
    from anychain.writer import backend_for
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    log, store = _open_log(cfg), cache_for(cfg)

    def bundle_for(h: str):
        return cached_bundle(h.strip(), cfg, store, build_bundle, lambda: finality_rpc_for(cfg))[0]
    try:
        bundle = bundle_for(tx_hash)
    except InvalidHashError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"Unexpected error while collecting data ({type(exc).__name__}: {exc}). Please report it.")
    session = ChatSession(cfg, bundle, (mode or Mode(cfg.assistant.default_mode)).value, chat_tools(cfg, bundle_for))
    backend = backend_for(cfg.llm)
    print(f"Transaction {bundle.tx_hash} on {cfg.network.name}: {bundle.status}. Ask about it (empty line to quit).")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question or question in ("exit", "quit"):
            break
        started = time.monotonic()
        try:
            turn = session.ask(question, backend)
        except KeyboardInterrupt:
            print("\n(question stopped)")
            continue
        _record(log, turn_event(session, turn, "chat", _ms(started)))
        facts = {e.id: e.text.split("\n")[0] for e in session.bundle.items}
        for call in turn.tool_calls:
            result = call["result"]
            shown = result if isinstance(result, str) else "; ".join(f"{i}: {facts.get(i, '')[:120]}" for i in result)
            print(f"  [{call.get('tool', '?')}] {shown}")
        if turn.answer:
            print(turn.answer)
        elif turn.outcome == "withheld":
            print("_(The answer was withheld: it stated things not in the evidence "
                  f"({'; '.join(turn.problems[:3])}).)_")
        else:
            print(f"_({turn.error or 'No answer.'})_")
            if turn.outcome == "refused":
                break


@app.command("eval")
def run_eval(
    cases: str = typer.Option("eval/cases.yaml", "--cases", help="The evaluation set"),
    case: str = typer.Option(None, "--case", help="Run only this case id"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Measure the evidence only (no written answers, no cost)"),
    out: str = typer.Option("eval", "--out", help="Folder for report.md and report.json"),
) -> None:
    """Run the evaluation set: real recorded transactions, offline, measured, saved as a report."""
    from pathlib import Path

    from anychain.evaluate import load_cases, report, run_case
    try:
        selected = [c for c in load_cases(Path(cases)) if case is None or c["id"] == case]
    except (OSError, KeyError) as exc:
        _fail(f"Cannot read the evaluation set {cases}: {exc}")
    if not selected:
        _fail(f"No case {case!r} in {cases}")
    results = []
    for c in selected:
        print(f"{c['id']}…", end=" ", flush=True)
        r = run_case(c, write=not no_llm)
        results.append(r)
        print(f"{r.got['status']} {r.got['rule'] or ''} {r.summary_status}")
    from anychain.llm_settings import effective
    model = "none (--no-llm)" if no_llm else effective(
        load_config(str(Path("configs") / f"{selected[0]['config']}.yaml")).llm).model
    summary = report(results, model, Path(out))
    print(json.dumps(summary, indent=1))
    print(f"Report: {Path(out) / 'report.md'}")


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def in_container() -> bool:
    """Running inside a Docker container (Docker creates /.dockerenv in every container)."""
    from pathlib import Path
    return Path("/.dockerenv").exists()


@app.command()
def serve(
    config: str = typer.Option(None, "--config", help="Path to network YAML (or set ANYCHAIN_CONFIG)"),
    host: str = typer.Option("127.0.0.1", help="Address to listen on: this machine only"),
    port: int = typer.Option(8000, min=1, max=65535),
) -> None:
    """Run the local API and its page: /explain, /chat, /health, /feedback, /settings/llm. It has no authentication, so it only listens on
    this machine."""
    import uvicorn

    from anychain.api import create_app
    container = host == "0.0.0.0" and in_container()  # 127.0.0.1 inside a container is unreachable from its host
    if host not in LOCAL_HOSTS and not container:
        _fail(f"The API has no authentication, so it only listens on this machine: use 127.0.0.1, not {host}"
              " (0.0.0.0 is allowed only inside a container).")
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    import socket
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as listening:  # someone answering on it: another program
        listening.settimeout(1)
        if listening.connect_ex((host, port)) == 0:
            _fail(f"Port {port} on {host} is taken: another program is listening on it. Pick another with --port.")
    with socket.socket(family) as probe:  # not listening, but not free yet: a server stopped moments ago
        try:
            probe.bind((host, port))
        except OSError as exc:
            _fail(f"Port {port} on {host} cannot be used yet ({exc.strerror}): it was released moments ago, or "
                  "another program holds it. Try again in a minute, or pick another with --port.")
    app_ = create_app(cfg, log=_open_log(cfg), store=cache_for(cfg), build=build_bundle, write_fn=write_checked,
                      finality=lambda: finality_rpc_for(cfg), record=_record)
    shown = f"[{host}]" if ":" in host else host
    print(f"AnyChain API for {cfg.network.name} on http://{shown}:{port} (Ctrl+C to stop)")
    if container:
        print(f"Inside a container: publish the port on the host's 127.0.0.1 only "
              f"(docker run -p 127.0.0.1:{port}:{port} ...), as the API has no authentication.")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


@app.command("metrics")
def show_metrics(
    config: str = typer.Option(None, "--config", help="Network YAML whose storage.sqlite_path holds the log"),
    hours: float = typer.Option(24 * 30, help="Look back this many hours"),
) -> None:
    """Usage metrics from the event log, each printed with the SQL that computes it."""
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


repos_app = typer.Typer(help="Contract repositories cited as source.")
app.add_typer(repos_app, name="repos")

llm_app = typer.Typer(help="The model that writes the answers, and its API key (saved outside the repository).")
app.add_typer(llm_app, name="llm")


def _show_llm() -> None:
    from anychain import llm_settings
    d = llm_settings.describe()
    print(f"Model: {d['provider'] or 'from the network config'}" + (f" / {d['model']}" if d["model"] else ""))
    for provider, state in d["keys"].items():
        print(f"  {provider} key: {state or 'none'}")
    print(f"Saved in {llm_settings.path()} (readable by you only)")


@llm_app.command("show")
def llm_show() -> None:
    """Show the chosen model and which keys are saved (never the keys)."""
    _show_llm()


@llm_app.command("set")
def llm_set(
    provider: str = typer.Option(..., help="claude_code (local CLI, no key) | anthropic | openai"),
    model: str = typer.Option(None, help="Model id; when left out, pick it from the provider's list"),
) -> None:
    """Choose the model; for anthropic or openai, type the API key when asked (it is not shown), then pick the
    model from the list the provider gives for that key."""
    from anychain import llm_settings
    key = None
    if provider in llm_settings.ENV_KEYS:
        key = typer.prompt(f"{provider} API key (not shown; empty keeps the saved one)", hide_input=True,
                           default="", show_default=False) or None
        try:
            models = llm_settings.list_models(provider, key)
        except llm_settings.ModelListError as exc:
            _fail(f"Could not list the models: {exc}")
        if not models:
            _fail(f"{provider} lists no model for this key.")
        if model is None:
            for i, m in enumerate(models, 1):
                print(f"  {i}. {m['label']}" + (f"  ({m['id']})" if m["label"] != m["id"] else ""))
            choice = typer.prompt("Model number", type=int)
            if not 1 <= choice <= len(models):
                _fail(f"Pick a number from 1 to {len(models)}.")
            model = models[choice - 1]["id"]
    try:
        llm_settings.save(provider, model, key)
    except ValueError as exc:
        _fail(str(exc))
    _show_llm()


@llm_app.command("test")
def llm_test(config: str = typer.Option(None, "--config", help="Network YAML (its llm block is the fallback)")) -> None:
    """One tiny call to the chosen model (a few tokens), to see that the key and the model work."""
    from anychain.writer import WriterError, backend_for
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    try:
        reply = backend_for(cfg.llm).complete("Reply with the single word OK.", "Is this model reachable?")
    except WriterError as exc:
        _fail(f"The model did not answer: {exc}")
    except Exception as exc:  # a provider's text can echo part of a key: only the kind of error
        _fail(f"The model did not answer: {type(exc).__name__}")
    print(f"The model answered: {(reply or '').strip()[:40]}")


@llm_app.command("clear")
def llm_clear() -> None:
    """Forget the chosen model and the saved keys (back to the network config and the environment)."""
    from anychain import llm_settings
    llm_settings.clear()
    print("Cleared: the network config's model and the environment's keys apply.")


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
