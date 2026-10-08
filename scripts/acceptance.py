"""Acceptance run (docs/ACCEPTANCE.md): random transactions per network, checked against the node.

    uv run python scripts/acceptance.py --per-network 300            # all shipped networks, resumable
    uv run python scripts/acceptance.py --networks celo-mainnet --per-network 5
    uv run python scripts/acceptance.py --report                     # summary -> docs/acceptance-report.md
    uv run python scripts/acceptance.py --recheck                    # re-run every check after an oracle change

The samples of the 2026-10-07 run are kept in docs/acceptance-samples/ (copy them into
data/acceptance/ to re-run exactly the same transactions).

Sampling is uniform over transactions of the last N days: pick a random block, keep it with
probability (its tx count / cap), then a random transaction in it. Without that weighting, txs in
sparse blocks (e.g. Rootstock's per-block reward tx) would be over-represented. The cap is the
largest block seen in a pilot of 200 random blocks of that network (blocks above it are always
kept: a small, documented bias). The seed and the sample are saved, so a run can be reproduced
and resumed.
"""
import argparse
import json
import random
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
from eth_utils import keccak

from anychain.bundle import build_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import USER_AGENT, Budget
from anychain.collectors.replay import RecordingTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from anychain.events import CheckEvent, RunEvent, SqliteEventLog
from anychain.models import EvidenceBundle
from anychain.oracle import check_answer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "acceptance"
NETWORKS = ["ethereum-mainnet", "optimism-mainnet", "gnosis-mainnet", "rootstock-mainnet", "celo-mainnet", "zksync-era"]
SEED = "anychain-acceptance-2026-10-07"
PILOT_BLOCKS = 200  # random blocks looked at first to calibrate the per-network cap
MIN_AGE_BLOCKS = 200  # skip the newest blocks: explorers may not have indexed them yet
# The oracle asks a node from a DIFFERENT provider than the tool's config, so the check is
# independent of the tool's own data path. Each was verified on 2026-10-07 to return receipts
# from 30 days back (the tool's publicnode for Ethereum returns null after ~2 weeks).
# Tried in order; the last one is the tool's own provider, used only if the others refuse
# (still independent of the explorer). Each result records which node answered.
ORACLE_RPC = {
    "ethereum-mainnet": ["https://eth.drpc.org", "https://1rpc.io/eth"],
    "optimism-mainnet": ["https://optimism.drpc.org", "https://mainnet.optimism.io"],
    # Public nodes prune history inconsistently (forno returned null for a 1-week-old Celo receipt that
    # drpc and ankr served), and 1rpc has a daily quota, so 1rpc goes last.
    "gnosis-mainnet": ["https://gnosis.drpc.org", "https://rpc.gnosischain.com", "https://1rpc.io/gnosis"],
    "rootstock-mainnet": ["https://rootstock.drpc.org", "https://public-node.rsk.co"],
    "celo-mainnet": ["https://celo.drpc.org", "https://rpc.ankr.com/celo", "https://forno.celo.org", "https://1rpc.io/celo"],
    "zksync-era": ["https://zksync.drpc.org", "https://mainnet.era.zksync.io", "https://1rpc.io/zksync2-era"],
}


def rpc_call(client: httpx.Client, url: str, method: str, params: list):
    for attempt in range(4):
        try:
            r = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            body = r.json()
            if "result" in body:
                return body["result"]
        except Exception:
            pass
        time.sleep(1 + attempt)
    raise RuntimeError(f"{method} failed on {url}")


def draw_sample(name: str, cfg, n: int, days: int) -> list[dict]:
    path = OUT / f"{name}.sample.json"
    if path.exists():
        return json.loads(path.read_text())
    rng = random.Random(f"{SEED}-{name}")
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    url = cfg.rpc.url
    latest = int(rpc_call(client, url, "eth_blockNumber", []), 16)
    head = rpc_call(client, url, "eth_getBlockByNumber", [hex(latest), False])
    back = rpc_call(client, url, "eth_getBlockByNumber", [hex(latest - 10000), False])
    block_time = (int(head["timestamp"], 16) - int(back["timestamp"], 16)) / 10000
    first = max(1, latest - int(days * 86400 / block_time))

    def tx_list(number: int) -> list:
        return (rpc_call(client, url, "eth_getBlockByNumber", [hex(number), False]) or {}).get("transactions") or []

    cap = max(1, max(len(tx_list(rng.randint(first, latest - MIN_AGE_BLOCKS))) for _ in range(PILOT_BLOCKS)))
    picked, seen, tries = [], set(), 0
    while len(picked) < n and tries < n * 200:
        tries += 1
        number = rng.randint(first, latest - MIN_AGE_BLOCKS)
        txs = tx_list(number)
        if not txs or rng.random() >= min(1.0, len(txs) / cap):
            continue
        tx_hash = rng.choice(txs)
        if tx_hash not in seen:
            seen.add(tx_hash)
            picked.append({"hash": tx_hash, "block": number, "block_tx_count": len(txs)})
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seed": SEED, "days": days, "first_block": first, "latest_block": latest,
                                "block_time_s": block_time, "cap": cap, "draws": tries, "sample": picked}, indent=1))
    print(f"{name}: sampled {len(picked)} txs from {tries} block draws (cap {cap})", flush=True)
    return json.loads(path.read_text())


def node_view(urls: list[str], tx_hash: str) -> tuple[dict | None, dict | None, str]:
    """Transaction and receipt from the first oracle node that answers both."""
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    last = "no node answered"
    for url in urls:
        try:
            tx = rpc_call(client, url, "eth_getTransactionByHash", [tx_hash])
            receipt = rpc_call(client, url, "eth_getTransactionReceipt", [tx_hash])
            if tx is not None and receipt is not None:
                return tx, receipt, url
            last = f"{url} returned null"
        except RuntimeError as exc:
            last = str(exc)
    raise RuntimeError(last)


DECIMALS_SELECTOR = "0x313ce567"  # decimals()


def fee_token_decimals(cfg, tx: dict | None, urls: list[str]) -> int | None:
    """Decimals of a Celo fee currency: from config (adapters), else the contract's decimals()."""
    currency = (tx or {}).get("feeCurrency")
    if not currency:
        return None
    known = cfg.fee_tokens.get(currency.lower())
    if known:
        return known.decimals
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    for url in urls:
        try:
            value = rpc_call(client, url, "eth_call", [{"to": currency, "data": DECIMALS_SELECTOR}, "latest"])
            return int(value, 16)
        except Exception:
            continue
    return None


L1_STEPS = (("precommitted", "ethPrecommitTxHash"), ("committed", "ethCommitTxHash"),
            ("proven", "ethProveTxHash"), ("executed", "ethExecuteTxHash"))
# Only the official node answers zks_* (zksync.drpc.org returns null, 1rpc 502 on 2026-10-07), and it is
# the tool's own node. So each step it names is proven on Ethereum through other providers: the L1
# transaction succeeded and the zkSync Era diamond proxy logged that step for this batch.
L1_ORACLE = ORACLE_RPC["ethereum-mainnet"]
ERA_DIAMOND = "0x32400084c286cf3e17e7b677ea9583e60a000324"  # emitter of the events below (live, 2026-10-07)
STEP_EVENTS = {  # topic0 from keccak of the signature; batch number position checked on batch 517442
    "committed": ("0x" + keccak(text="BlockCommit(uint256,bytes32,bytes32)").hex(), "exact"),
    "proven": ("0x" + keccak(text="BlocksVerification(uint256,uint256)").hex(), "range"),  # (previous, current]
    "executed": ("0x" + keccak(text="BlockExecution(uint256,bytes32,bytes32)").hex(), "exact"),
}
ZKSYNC_NODES = ["https://mainnet.era.zksync.io"]


def _first(urls: list[str], method: str, params: list):
    client = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    for url in urls:
        try:
            value = rpc_call(client, url, method, params)
            if value is not None:
                return value
        except Exception:
            continue
    return None


def _proves_step(l1_receipt: dict, label: str, batch: int) -> bool:
    topic0, kind = STEP_EVENTS[label]
    for log in l1_receipt.get("logs") or []:
        topics = [t.lower() for t in log.get("topics") or []]
        if (log.get("address") or "").lower() != ERA_DIAMOND or not topics or topics[0] != topic0:
            continue
        if kind == "exact" and len(topics) > 1 and int(topics[1], 16) == batch:
            return True
        if kind == "range" and len(topics) > 2 and int(topics[1], 16) < batch <= int(topics[2], 16):
            return True
    return False


def l1_details(cfg, tx_hash: str, urls: list[str]) -> dict | None:
    """zkSync only: the L1 steps of this transaction's batch, each proven on Ethereum, with its L1 block time.

    None (no verdict) when a step cannot be proven, including precommit, whose event is not checked yet.
    """
    if cfg.network.chain_type != "zksync":
        return None
    details = _first(ZKSYNC_NODES, "zks_getTransactionDetails", [tx_hash])
    receipt = _first(urls, "eth_getTransactionReceipt", [tx_hash])
    if not isinstance(details, dict) or not isinstance(receipt, dict) or not receipt.get("l1BatchNumber"):
        return None
    batch = int(receipt["l1BatchNumber"], 16)
    steps, times = {}, {}
    for label, key in L1_STEPS:
        h = details.get(key)
        if not h:
            continue
        if label not in STEP_EVENTS:
            return None
        l1_receipt = _first(L1_ORACLE, "eth_getTransactionReceipt", [h])
        if not isinstance(l1_receipt, dict) or l1_receipt.get("status") != "0x1" or not _proves_step(l1_receipt, label, batch):
            return None
        block = _first(L1_ORACLE, "eth_getBlockByNumber", [l1_receipt["blockNumber"], False])
        steps[label] = h
        if isinstance(block, dict):
            times[label] = int(block["timestamp"], 16)
    return {"steps": steps, "times": times}


def run_one(cfg, tx_hash: str, oracle_rpcs: list[str]) -> dict:
    transport = RecordingTransport()
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    budget = Budget(cfg.assistant.time_budget_s)
    started, answered_at = time.monotonic(), time.time()
    error = None
    try:
        bundle = build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client, budget), RpcClient(cfg.rpc, client, budget))
    except Exception as exc:  # the tool itself must never get here; recorded as a defect
        bundle, error = None, f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    tx, receipt, oracle_node = node_view(oracle_rpcs, tx_hash)
    corpus = "\n".join(r.get("body", "") for r in transport.records.values())
    checks = check_answer(bundle, cfg, tx, receipt, corpus, fee_token_decimals(cfg, tx, oracle_rpcs),
                          l1_details(cfg, tx_hash, oracle_rpcs), answered_at) if bundle else []
    return {
        "hash": tx_hash, "ts": answered_at, "elapsed_s": round(elapsed, 2), "error": error, "oracle_node": oracle_node,
        "status": bundle.status if bundle else None,
        "facts": len(bundle.items) if bundle else 0,
        "gaps": [g.what for g in bundle.gaps] if bundle else [],
        "processing_errors": [g.why for g in bundle.gaps if "could not process" in g.why] if bundle else [],
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        "bundle": json.loads(bundle.model_dump_json()) if bundle else None,
    }


def _legacy_cause(gap: dict) -> str:
    """Cause for a gap saved before gaps had one (the 2026-10-07 run), by the tool's own rule.

    Checked against every distinct reason in that run: all retryable gaps were time-budget or
    ABI-lookup timeouts (source unavailable); the one non-retryable "RPC transaction data" gap was a
    node error -32603 (source refused); the rest were expected limits (no ABI, list truncated).
    """
    if "could not process" in gap["why"]:
        return "processing_error"
    if gap["retryable"]:
        return "source_unavailable"
    return "source_error" if gap["what"] == "RPC transaction data" else "not_interpretable"


def event_for(name: str, result: dict) -> RunEvent:
    """The run log's view of one checked answer (source "canary": independent check against a node)."""
    ms = int((result.get("elapsed_s") or 0) * 1000)
    ts = result.get("ts") or result.get("_file_ts")
    if not result.get("bundle"):
        return RunEvent.crash(name, result["hash"], "canary", ms, result.get("error") or "no answer", ts)
    raw = result["bundle"]
    for gap in raw.get("gaps", []):
        gap.setdefault("cause", _legacy_cause(gap))
    checks = tuple(CheckEvent(c["name"], c["status"], c.get("detail", "")) for c in result.get("checks", []))
    return RunEvent.from_bundle(EvidenceBundle.model_validate(raw), "canary", ms, checks, ts)


def event_log(cfg) -> SqliteEventLog:
    path = Path(cfg.storage.sqlite_path)
    return SqliteEventLog(path if path.is_absolute() else ROOT / path)


LOG_EVENTS = True  # set in main(): only the default results folder, or --log, writes canary answers


def log_event(cfg, name: str, result: dict) -> None:
    """The latest check of an answer replaces earlier ones. Never stops the run: the JSONL is written."""
    if not LOG_EVENTS:
        return
    try:
        event_log(cfg).record(event_for(name, result), replace=True)
    except Exception as exc:
        print(f"{name}: event log unavailable ({type(exc).__name__}: {exc})", flush=True)


def log_results() -> None:
    """Load saved results into the run log (oracle errors stay out: they are the harness, not the tool)."""
    for name in NETWORKS:
        path = OUT / f"{name}.results.jsonl"
        if not path.exists():
            continue
        cfg = load_config(str(ROOT / "configs" / f"{name}.yaml"))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        kept = [r for r in rows if not (r.get("error") or "").startswith("harness")]
        log, added = event_log(cfg), 0
        for r in kept:
            if log.has(name, "canary", r["hash"]):
                continue  # already logged (live, or by an earlier load)
            # rows from before `ts` was saved get the file's last write: the run's end, not its start
            r.setdefault("_file_ts", path.stat().st_mtime)
            log.record(event_for(name, r))
            added += 1
        print(f"{name}: {added} logged, {len(kept) - added} already there", flush=True)


def run_network(name: str, n: int, days: int) -> None:
    cfg = load_config(str(ROOT / "configs" / f"{name}.yaml"))
    sample = draw_sample(name, cfg, n, days)["sample"]
    out = OUT / f"{name}.results.jsonl"
    done = {json.loads(line)["hash"] for line in out.read_text().splitlines()} if out.exists() else set()
    for item in sample:
        if item["hash"] in done:
            continue
        try:
            result = run_one(cfg, item["hash"], ORACLE_RPC[name])
        except Exception as exc:
            result = {"hash": item["hash"], "error": f"harness: {type(exc).__name__}: {exc}", "checks": []}
        result["block"] = item["block"]
        with out.open("a") as fh:
            fh.write(json.dumps(result) + "\n")
        if not (result.get("error") or "").startswith("harness"):
            log_event(cfg, name, result)
        time.sleep(0.3)
    print(f"{name}: done", flush=True)


def recheck() -> None:
    """Re-run every result's checks with the current oracle (after an oracle change).

    Uses the tool's saved answer, so the tool is not re-run; the node is asked again. The
    addresses check needs the raw responses, which are not saved, so its earlier verdict is kept.
    """
    from anychain.models import EvidenceBundle
    for name in NETWORKS:
        path = OUT / f"{name}.results.jsonl"
        if not path.exists():
            continue
        cfg = load_config(str(ROOT / "configs" / f"{name}.yaml"))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        changed = 0
        for r in rows:
            if not r.get("bundle"):
                continue
            try:
                tx, receipt, node = node_view(ORACLE_RPC[name], r["hash"])
            except RuntimeError as exc:
                print(f"  {r['hash']}: node unavailable ({exc}); kept as is", flush=True)
                continue
            r["oracle_node"] = node
            kept = {c["name"]: c for c in r["checks"] if c["name"] == "addresses_exist"}
            decimals = fee_token_decimals(cfg, tx, ORACLE_RPC[name])
            # judged at the time it answered; rows from before `ts` was saved use the results file's last
            # write, which is later than the answer (a step in between would count against it)
            answered_at = r.get("ts") or path.stat().st_mtime
            fresh = [c for c in check_answer(EvidenceBundle.model_validate(r["bundle"]), cfg, tx, receipt, "", decimals,
                                             l1_details(cfg, r["hash"], ORACLE_RPC[name]), answered_at)
                     if c.name != "addresses_exist"]
            r["checks"] = [{"name": c.name, "status": c.status, "detail": c.detail} for c in fresh] + list(kept.values())
            r["rechecked"] = True
            log_event(cfg, name, r)
            changed += 1
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"{name}: rechecked {changed}", flush=True)


def report() -> None:
    lines = ["# Acceptance report", "", "Generated by `scripts/acceptance.py --report`. Method: docs/ACCEPTANCE.md.", "",
             "| Network | Txs | Status | Value | Fee | Token transfers | No double native | Addresses | L1 status | "
             "Tool errors | Oracle errors |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    flagged = []
    for name in NETWORKS:
        path = OUT / f"{name}.results.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        tally = Counter((c["name"], c["status"]) for r in rows for c in r["checks"])
        errors = sum(1 for r in rows if (r.get("error") and not r["error"].startswith("harness"))
                     or r.get("processing_errors"))
        harness = sum(1 for r in rows if (r.get("error") or "").startswith("harness"))

        def cell(check):
            p, f, s = tally[(check, "pass")], tally[(check, "fail")], tally[(check, "skip")]
            if not (p or f or s):
                return "n/a"
            return f"{p} ok / **{f} fail** / {s} skip" if f else f"{p} ok / 0 fail / {s} skip"
        lines.append(f"| {name} | {len(rows)} | {cell('status')} | {cell('value')} | {cell('fee')} | "
                     f"{cell('token_transfers')} | {cell('no_double_native')} | {cell('addresses_exist')} | "
                     f"{cell('l1_status')} | {errors} | {harness} |")
        for r in rows:
            for c in r["checks"]:
                if c["status"] == "fail":
                    flagged.append(f"- `{name}` `{r['hash']}` **{c['name']}**: {c['detail'][:300]}")
            if r.get("error") or r.get("processing_errors"):
                flagged.append(f"- `{name}` `{r['hash']}` **tool error**: {r.get('error') or r['processing_errors'][0][:300]}")
    lines += ["", "## Flagged for review", ""] + (flagged or ["None."])
    (ROOT / "docs" / "acceptance-report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:12]))
    print(f"... {len(flagged)} flagged")


def main() -> None:
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--networks", nargs="*", default=NETWORKS)
    parser.add_argument("--per-network", type=int, default=300)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--recheck", action="store_true", help="re-run the checks on flagged results")
    parser.add_argument("--log-results", action="store_true", help="load saved results into the run log")
    parser.add_argument("--log", action="store_true",
                        help="write to the run log even with --out (default: only the default results folder logs)")
    parser.add_argument("--out", default=str(ROOT / "data" / "acceptance"), help="results folder (another one for smoke runs)")
    args = parser.parse_args()
    OUT = Path(args.out)
    global LOG_EVENTS
    LOG_EVENTS = args.log or OUT == ROOT / "data" / "acceptance"
    if args.recheck:
        recheck()
        return
    if args.log_results:
        if not LOG_EVENTS:
            sys.exit("--log-results with --out writes to the run log only with --log")
        log_results()
        return
    if args.report:
        report()
        return
    threads = [threading.Thread(target=run_network, args=(n, args.per_network, args.days)) for n in args.networks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    sys.exit(main())
