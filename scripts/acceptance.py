"""Acceptance run (docs/ACCEPTANCE.md): random transactions per network, checked against the node.

    uv run python scripts/acceptance.py --per-network 300            # all shipped networks, resumable
    uv run python scripts/acceptance.py --networks celo-mainnet --per-network 5
    uv run python scripts/acceptance.py --report                     # summary -> docs/acceptance-report.md

Sampling is uniform over transactions of the last N days: pick a random block, keep it with
probability (its tx count / cap), then a random transaction in it. Without that weighting, txs in
sparse blocks (e.g. Rootstock's per-block reward tx) would be over-represented. The seed and the
sample are saved, so a run can be reproduced and resumed.
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

from anychain.bundle import build_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import USER_AGENT, Budget
from anychain.collectors.replay import RecordingTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from anychain.oracle import check_answer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "acceptance"
NETWORKS = ["ethereum-mainnet", "optimism-mainnet", "gnosis-mainnet", "rootstock-mainnet", "celo-mainnet", "zksync-era"]
SEED = "anychain-acceptance-2026-10-07"
CAP = 150  # block size at or above which a block is always kept when sampling
MIN_AGE_BLOCKS = 200  # skip the newest blocks: explorers may not have indexed them yet
# The oracle asks a node from a DIFFERENT provider than the tool's config, so the check is
# independent of the tool's own data path. Each was verified on 2026-10-07 to return receipts
# from 30 days back (the tool's publicnode for Ethereum returns null after ~2 weeks).
ORACLE_RPC = {
    "ethereum-mainnet": "https://eth.drpc.org",
    "optimism-mainnet": "https://optimism.drpc.org",
    "gnosis-mainnet": "https://1rpc.io/gnosis",
    "rootstock-mainnet": "https://rootstock.drpc.org",
    "celo-mainnet": "https://1rpc.io/celo",
    "zksync-era": "https://1rpc.io/zksync2-era",
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
    picked, seen, tries = [], set(), 0
    while len(picked) < n and tries < n * 200:
        tries += 1
        number = rng.randint(first, latest - MIN_AGE_BLOCKS)
        block = rpc_call(client, url, "eth_getBlockByNumber", [hex(number), False])
        txs = (block or {}).get("transactions") or []
        if not txs or rng.random() >= min(1.0, len(txs) / CAP):
            continue
        tx_hash = rng.choice(txs)
        if tx_hash not in seen:
            seen.add(tx_hash)
            picked.append({"hash": tx_hash, "block": number, "block_tx_count": len(txs)})
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seed": SEED, "days": days, "first_block": first, "latest_block": latest,
                                "block_time_s": block_time, "draws": tries, "sample": picked}, indent=1))
    return json.loads(path.read_text())


def run_one(cfg, tx_hash: str, oracle_rpc: str) -> dict:
    transport = RecordingTransport()
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    budget = Budget(cfg.assistant.time_budget_s)
    started = time.monotonic()
    error = None
    try:
        bundle = build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client, budget), RpcClient(cfg.rpc, client, budget))
    except Exception as exc:  # the tool itself must never get here; recorded as a defect
        bundle, error = None, f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    node = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    tx = rpc_call(node, oracle_rpc, "eth_getTransactionByHash", [tx_hash])
    receipt = rpc_call(node, oracle_rpc, "eth_getTransactionReceipt", [tx_hash])
    corpus = "\n".join(r.get("body", "") for r in transport.records.values())
    checks = check_answer(bundle, cfg, tx, receipt, corpus) if bundle else []
    return {
        "hash": tx_hash, "elapsed_s": round(elapsed, 2), "error": error,
        "status": bundle.status if bundle else None,
        "facts": len(bundle.items) if bundle else 0,
        "gaps": [g.what for g in bundle.gaps] if bundle else [],
        "processing_errors": [g.why for g in bundle.gaps if "could not process" in g.why] if bundle else [],
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        "bundle": json.loads(bundle.model_dump_json()) if bundle else None,
    }


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
        time.sleep(0.3)
    print(f"{name}: done", flush=True)


def report() -> None:
    lines = ["# Acceptance report", "", "Generated by `scripts/acceptance.py --report`. Method: docs/ACCEPTANCE.md.", "",
             "| Network | Txs | Status | Value | Fee | Token transfers | No double native | Addresses | Tool errors |",
             "|---|---|---|---|---|---|---|---|---|"]
    flagged = []
    for name in NETWORKS:
        path = OUT / f"{name}.results.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        tally = Counter((c["name"], c["status"]) for r in rows for c in r["checks"])
        errors = sum(1 for r in rows if r.get("error") or r.get("processing_errors"))

        def cell(check):
            p, f, s = tally[(check, "pass")], tally[(check, "fail")], tally[(check, "skip")]
            return f"{p} ok / **{f} fail** / {s} skip" if f else f"{p} ok / 0 fail / {s} skip"
        lines.append(f"| {name} | {len(rows)} | {cell('status')} | {cell('value')} | {cell('fee')} | "
                     f"{cell('token_transfers')} | {cell('no_double_native')} | {cell('addresses_exist')} | {errors} |")
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
    parser.add_argument("--out", default=str(ROOT / "data" / "acceptance"), help="results folder (another one for smoke runs)")
    args = parser.parse_args()
    OUT = Path(args.out)
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
