"""`anychain eval` (PHASE3 T5, R7, D50): the evaluation set run offline, measured, and saved as a report.

Each case in eval/cases.yaml is a real transaction recorded into tests/fixtures; its evidence is rebuilt from the
recording (no network), the configured model writes the answer through the same check as `explain` (D28), and:

  status / rule / label / abi_source   compared with the case's expected values
  citation coverage   among the answer's sentences that state something (at least five words, not a heading or a
                      question), the share that cite a fact [E#]
  hallucination       the model's first attempt had something not in the evidence (the check caught it: the answer
                      was then fixed on the retry, or withheld); also the count of such problems
  degradation         a case that expects gaps (a source down) declares them with the expected causes
  entities            the case's key values appear in the written answer (any accepted spelling)
  latency, tokens     the written answer's time, tokens and cost as the backend reports them

The report goes to eval/report.md and eval/report.json, with the date, the commit and the model.
"""
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
import yaml

from anychain.answer import structured_answer
from anychain.bundle import build_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.replay import ReplayTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from anychain.events import abi_source_of
from anychain.writer import WriterError, write_checked

ROOT = Path(__file__).resolve().parents[2]
CITE = re.compile(r"\[\s*E\d+")


@dataclass
class CaseResult:
    id: str
    category: str
    network: str
    status_ok: bool
    rule_ok: bool
    label_ok: bool
    abi_ok: bool
    degradation_ok: bool | None  # None: the case expects no gap
    got: dict
    summary_status: str = "skipped"
    first_attempt_problems: list[str] = field(default_factory=list)
    sentences: int = 0
    cited: int = 0
    entities_found: list[bool] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)  # facts the answer must cite: the conclusions, the timeline
    key_facts_cited: list[str] = field(default_factory=list)
    seconds: float | None = None
    usage: dict | None = None
    answer: str | None = None
    error: str | None = None


def load_cases(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())["cases"]


def run_case(case: dict, write: bool, backend=None) -> CaseResult:
    cfg = load_config(str(ROOT / "configs" / f"{case['config']}.yaml"))
    fixture = ROOT / "tests" / "fixtures" / f"{case['fixture']}.json"
    transport = ReplayTransport(fixture, set(case.get("offline_hosts") or []))
    client = httpx.Client(transport=transport)
    tx_hash = _recorded_hash(fixture)
    bundle = build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))
    own = next((e for e in bundle.items if e.kind == "diagnosis" and not e.data.get("from_replay")), None)
    replay = next((e for e in bundle.items if e.kind == "diagnosis" and e.data.get("from_replay")), None)
    got = {"status": bundle.status, "rule": own.data.get("rule") if own else None,
           "label": own.data.get("label") if own else None, "abi_source": abi_source_of(bundle),
           "gaps": sorted({g.cause for g in bundle.gaps}),
           "replay": replay.data.get("meaning") if replay else None}  # the cause the replay on the node found
    expect = case["expect"]
    expected_gaps = set(expect.get("gaps") or [])
    result = CaseResult(
        case["id"], case["category"], cfg.network.name, got["status"] == expect["status"],
        got["rule"] == expect.get("rule") and got["replay"] == expect.get("replay"), got["label"] == expect.get("label"),
        got["abi_source"] == expect.get("abi_source"),
        expected_gaps <= set(got["gaps"]) if expected_gaps else None, got)
    result.key_facts = key_facts(bundle)
    if not write:
        return result
    started = time.monotonic()
    try:
        checked = write_checked(bundle, cfg, case.get("mode", "support"), backend=backend)
    except WriterError as exc:
        result.summary_status, result.error = "unavailable", str(exc)
        return result
    result.seconds = round(time.monotonic() - started, 1)
    result.usage = checked.usage
    result.summary_status = checked.outcome
    result.answer = checked.text
    if checked.outcome in ("retried", "withheld"):
        result.first_attempt_problems = list(checked.problems)
    if checked.text:
        sentences = factual_sentences(checked.text)
        result.sentences, result.cited = len(sentences), sum(1 for s in sentences if CITE.search(s))
        cited = set(re.findall(r"E\d+", checked.text))
        result.key_facts_cited = [f for f in result.key_facts if f in cited]
        lowered = checked.text.lower()
        result.entities_found = [any(alt.lower() in lowered for alt in entity) for entity in expect.get("entities", [])]
    structured_answer(bundle, checked.text, checked.outcome, case.get("mode", "support"))  # the shape stays valid
    return result


def key_facts(bundle) -> list[str]:
    """The facts an answer is not complete without: each conclusion (the failure's own and the replay's) and each
    pattern of the sender's timeline (a retry that succeeded, repeated failures)."""
    return [e.id for e in bundle.items
            if e.kind == "diagnosis" or (e.kind == "timeline" and e.data.get("pattern"))]


def factual_sentences(text: str) -> list[str]:
    """The answer's sentences that state something: split at sentence ends and line breaks; at least five words;
    not a heading (a line of bold text or one ending in a colon) and not a question."""
    out = []
    # a citation written after the sentence's full stop ("… moved. [E1]") belongs to that sentence
    text = re.sub(r"([.!?])((?:\s*\[[^\]\n]*E\d+[^\]\n]*\])+)", r"\2\1", text)
    for line in text.split("\n"):
        line = line.strip().lstrip("-*0123456789.) ").strip()
        if not line or line.startswith("#") or (line.startswith("**") and line.rstrip(":").endswith("**")):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9*\[])", line):
            words = re.findall(r"[\wÀ-ÿ]+", sentence)
            if len(words) >= 5 and not sentence.rstrip().endswith("?") and not sentence.rstrip().endswith(":"):
                out.append(sentence)
    return out


def summarize(results: list[CaseResult]) -> dict:
    written = [r for r in results if r.summary_status not in ("skipped", "unavailable")]
    degradation = [r for r in results if r.degradation_ok is not None]
    entities = [found for r in written for found in r.entities_found]
    pct = lambda n, d: round(100 * n / d, 1) if d else None  # noqa: E731
    return {
        "cases": len(results),
        "status_accuracy": pct(sum(r.status_ok for r in results), len(results)),
        "diagnosis_accuracy": pct(sum(r.rule_ok and r.label_ok for r in results), len(results)),
        "abi_source_accuracy": pct(sum(r.abi_ok for r in results), len(results)),
        "degradation_declared": pct(sum(bool(r.degradation_ok) for r in degradation), len(degradation)),
        "written": len(written),
        "citation_coverage": pct(sum(r.cited for r in written), sum(r.sentences for r in written)),
        "hallucination_rate": pct(sum(1 for r in written if r.first_attempt_problems), len(written)),
        "delivered_with_unsupported_values": 0,  # by construction: an answer the check rejects twice is withheld
        "hallucinated_items": sum(len(r.first_attempt_problems) for r in written),
        "withheld": sum(1 for r in written if r.summary_status == "withheld"),
        "entities_found": pct(sum(entities), len(entities)),
        "key_facts_cited": pct(sum(len(r.key_facts_cited) for r in written), sum(len(r.key_facts) for r in written)),
        "seconds_per_case": round(sum(r.seconds or 0 for r in written) / len(written), 1) if written else None,
        "tokens": {k: sum((r.usage or {}).get(k) or 0 for r in written)
                   for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")},
        "cost_usd": round(sum((r.usage or {}).get("cost_usd") or 0 for r in written), 4),
    }


def report(results: list[CaseResult], model: str, out_dir: Path) -> dict:
    summary = summarize(results)
    meta = {"date": time.strftime("%Y-%m-%d %H:%M"), "commit": _commit(), "model": model}
    (out_dir / "report.json").write_text(json.dumps({**meta, "summary": summary,
                                                     "cases": [asdict(r) for r in results]}, indent=1,
                                                    ensure_ascii=False))
    lines = [f"# Evaluation report\n", f"Run {meta['date']}, commit {meta['commit']}, model {model}. "
             "Every case is a real transaction replayed from its recording (eval/cases.yaml).\n",
             "| Metric | Result |", "|---|---|"]
    names = {"status_accuracy": "Status accuracy", "diagnosis_accuracy": "Diagnosis rule and label accuracy",
             "abi_source_accuracy": "ABI source accuracy", "degradation_declared": "Degradation declared correctly",
             "citation_coverage": "Citation coverage (factual sentences citing a fact)",
             "hallucination_rate": "First drafts the check caught (a value or citation not in the evidence; rewritten)",
             "entities_found": "Key values present in the answer",
             "key_facts_cited": "Key facts cited (each conclusion, and what the sender's timeline shows)"}
    for key, name in names.items():
        lines.append(f"| {name} | {'' if summary[key] is None else str(summary[key]) + '%'} |")
    lines += [f"| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |",
              f"| Values the check caught on a first attempt | {summary['hallucinated_items']} |",
              f"| Answers withheld | {summary['withheld']} of {summary['written']} |",
              f"| Time per written answer | {summary['seconds_per_case']} s |",
              f"| Tokens (input, output, cache read, cache write) | {', '.join(str(v) for v in summary['tokens'].values())} |",
              f"| Cost reported by the backend | {summary['cost_usd']} USD |", "",
              "| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    mark = lambda ok: "ok" if ok else "MISS"  # noqa: E731
    for r in results:
        g = r.got
        replay = f" (replay: {g['replay']})" if g.get("replay") else ""
        lines.append(f"| {r.id} | {r.network} | {mark(r.status_ok)} {g['status']} | {mark(r.rule_ok and r.label_ok)} "
                     f"{g['rule'] or '-'} {g['label'] or ''}{replay} | {mark(r.abi_ok)} {g['abi_source'] or '-'} | "
                     f"{'' if r.degradation_ok is None else mark(r.degradation_ok)} {', '.join(g['gaps']) or '-'} | "
                     f"{r.summary_status} | {r.cited}/{r.sentences} | {sum(r.entities_found)}/{len(r.entities_found)} | "
                     f"{len(r.key_facts_cited)}/{len(r.key_facts)} | "
                     f"{'' if r.seconds is None else r.seconds} | {_tokens(r.usage)} |")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def _tokens(usage: dict | None) -> str:
    """Input tokens (cache reads and writes included) and output tokens, as the backend reported them."""
    if not usage:
        return ""
    sent = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    return f"{sent}/{usage.get('output_tokens') or 0}"


def _recorded_hash(fixture: Path) -> str:
    for key in json.loads(fixture.read_text()):
        m = re.search(r"/transactions/(0x[0-9a-fA-F]{64})$", key)
        if m:
            return m.group(1)
    for key in json.loads(fixture.read_text()):
        m = re.search(r'eth_getTransactionByHash \["(0x[0-9a-fA-F]{64})"\]', key)
        if m:
            return m.group(1)
    raise ValueError(f"no transaction hash in {fixture.name}")


def _commit() -> str:
    """The commit the code ran at, marked when the source had changes not committed yet."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip() or "unknown"
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "prompts", "configs"], cwd=ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return f"{head} + uncommitted changes" if dirty else head
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
