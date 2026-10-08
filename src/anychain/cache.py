"""Answers kept by (network, hash), when their facts cannot change (PHASE3 T0, R10, D44).

A transaction's evidence is kept only when it is final and complete:
  - its block is final: at or before the node's "finalized" block, or, on a node without that tag,
    `cache.min_confirmations` blocks deep (on zkSync the finalized block is the last one executed on L1,
    so a transaction not yet executed there is never kept);
  - it has no open gap: nothing worth trying again (a source down, behind, refusing, pending, our own
    error), and nothing that can be filled later (an ABI or verified source the explorer lacks today,
    a repo not synced, a signature database lookup);
  - it succeeded or failed (never pending, dropped or unknown).
The key is the chain id, the hash, a digest of this code and a digest of the config parts that shape the
facts (and of the repos' synced commits), so any change that could change a fact misses the old entry.
Entries also expire after `cache.max_age_days`: explorer names, labels and verification change over time.
Written answers are kept apart, keyed by the evidence itself, the mode, the language, the question, the
prompts and the model.
"""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from anychain.collectors.http import CollectorError
from anychain.config import AppConfig
from anychain.models import EvidenceBundle

PACKAGE = Path(__file__).resolve().parent
OPEN_CAUSES = {"source_unavailable", "source_error", "source_behind", "pending", "processing_error", "config_error"}
# Gaps the explorer, a repo sync or a database can fill later: an ABI or verified source, a custom error's
# meaning in a replay, a fee token's decimals, a signature lookup.
FILLABLE_TOPICS = {"ABI lookup", "Call decoding", "Event decoding", "Source code", "Signature database", "Replay", "Fee"}
KEPT_STATUSES = {"success", "failed"}


def _digest_files(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _code_digest() -> str:
    """This package's code, and the versions of the libraries that decode data (a new eth-abi can change a fact)."""
    from importlib.metadata import PackageNotFoundError, version
    versions = []
    for dist in ("eth-abi", "eth-utils", "eth-hash", "pydantic", "httpx"):
        try:
            versions.append(f"{dist}={version(dist)}")
        except PackageNotFoundError:
            versions.append(f"{dist}=none")
    return hashlib.sha256((_digest_files(PACKAGE.rglob("*.py")) + ",".join(versions)).encode()).hexdigest()[:16]


CODE_DIGEST = _code_digest()
PROMPTS = PACKAGE.parents[1] / "prompts"


def config_digest(cfg: AppConfig) -> str:
    """The config parts that shape the facts, plus the commit each configured repo is synced at."""
    from anychain.collectors.repo import RepoCache
    shaping = cfg.model_dump(exclude={"llm", "assistant", "storage", "cache"})
    try:
        cache = RepoCache(cfg.storage.cache_dir)
        shaping["synced"] = [cache.commit_for(r) for r in cfg.repos]
    except CollectorError:
        shaping["synced"] = "unreadable"
    return hashlib.sha256(json.dumps(shaping, sort_keys=True, default=str).encode()).hexdigest()[:16]


def keep_decision(bundle: EvidenceBundle, rpc, cfg: AppConfig) -> str | None:
    """None when the evidence can be kept, else why not (said in the event log)."""
    if bundle.status not in KEPT_STATUSES:
        return f"status {bundle.status}"
    for g in bundle.gaps:
        if g.retryable or g.cause in OPEN_CAUSES:
            return f"open gap: {g.what} ({g.cause})"
        if g.what in FILLABLE_TOPICS:
            return f"gap that can be filled later: {g.what}"
    block = next((e.data.get("block") for e in bundle.items if e.kind == "overview"), None)
    if not isinstance(block, int):
        return "no block number"
    if rpc is None:
        return "finality not checked: no node"
    try:
        finalized = rpc.finalized_block()
        if finalized is not None and block > finalized:
            return f"not final: block {block} is after the finalized block {finalized}"
        if finalized is None:
            depth = rpc.block_number() - block
            if depth < cfg.cache.min_confirmations:
                return f"not final: {depth} confirmations, fewer than {cfg.cache.min_confirmations}"
    except CollectorError as exc:
        return f"finality not checked: {exc}"
    for e in bundle.items:  # zkSync: the explorer's L1 status changes until it lists the execution (D25)
        status = e.data.get("l1_status") if e.kind == "chain" else None
        if isinstance(status, str) and "executed" not in status.lower():
            return f"the explorer's L1 status ({status!r}) can still change"
    return None


def answer_key(bundle: EvidenceBundle, cfg: AppConfig, mode: str, question: str | None = None) -> str:
    """A written answer depends on the evidence, the mode, the language, the question, the prompts and the model."""
    parts = {"evidence": bundle.model_dump(), "mode": mode, "language": cfg.assistant.language,
             "question": question, "prompts": _digest_files(PROMPTS.glob("*.md")) if PROMPTS.exists() else None,
             "model": [cfg.llm.provider, cfg.llm.model], "code": CODE_DIGEST}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS bundles (
    key TEXT PRIMARY KEY, chain_id INTEGER NOT NULL, tx_hash TEXT NOT NULL, ts REAL NOT NULL, bundle TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answers (
    key TEXT PRIMARY KEY, ts REAL NOT NULL, text TEXT NOT NULL, outcome TEXT NOT NULL);
"""


class BundleCache:
    """The cache in one SQLite file (`cache.path`)."""

    def __init__(self, path: str | Path, max_age_days: float = 30):
        self.path = Path(path)
        self.max_age_s = max_age_days * 86400
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _key(cfg: AppConfig, tx_hash: str) -> str:
        return f"{cfg.network.chain_id}:{tx_hash.lower()}:{CODE_DIGEST}:{config_digest(cfg)}"

    def get_bundle(self, cfg: AppConfig, tx_hash: str) -> EvidenceBundle | None:
        with self._connect() as db:
            row = db.execute("SELECT ts, bundle FROM bundles WHERE key = ?", (self._key(cfg, tx_hash),)).fetchone()
        if row is None or time.time() - row[0] > self.max_age_s:
            return None
        try:
            return EvidenceBundle.model_validate_json(row[1])
        except ValueError:
            return None  # a damaged entry is only a cache: fetch again

    def put_bundle(self, cfg: AppConfig, tx_hash: str, bundle: EvidenceBundle, now: float | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO bundles (key, chain_id, tx_hash, ts, bundle) VALUES (?, ?, ?, ?, ?)",
                       (self._key(cfg, tx_hash), cfg.network.chain_id, tx_hash.lower(),
                        time.time() if now is None else now, bundle.model_dump_json()))

    def get_answer(self, key: str) -> tuple[str, str] | None:
        """(text, outcome) of a written answer kept for this key."""
        with self._connect() as db:
            row = db.execute("SELECT ts, text, outcome FROM answers WHERE key = ?", (key,)).fetchone()
        if row is None or time.time() - row[0] > self.max_age_s:
            return None
        return row[1], row[2]

    def put_answer(self, key: str, text: str, outcome: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO answers (key, ts, text, outcome) VALUES (?, ?, ?, ?)",
                       (key, time.time(), text, outcome))


class NoCache:
    """Cache turned off (`cache.enabled: false`) or not openable: every answer is fetched."""

    def get_bundle(self, cfg, tx_hash):
        return None

    def put_bundle(self, cfg, tx_hash, bundle, now=None):
        pass

    def get_answer(self, key):
        return None

    def put_answer(self, key, text, outcome):
        pass


def cached_bundle(tx_hash: str, cfg: AppConfig, store, build, finality_rpc, fresh: bool = False):
    """(bundle, cache state): "hit", "stored", "off", or "not kept: <reason>". A cache that cannot be read or
    written is skipped, never fatal: the answer is built as without a cache."""
    if isinstance(store, NoCache):
        return build(tx_hash, cfg), "off"
    broken = None
    if not fresh:
        try:
            kept = store.get_bundle(cfg, tx_hash)
        except Exception as exc:  # a locked or unreadable file: answer without the cache
            kept, broken = None, f"{type(exc).__name__}: {exc}"
        if kept is not None:
            return kept, "hit"
    bundle = build(tx_hash, cfg)
    if broken:
        return bundle, f"not kept: cache error ({broken})"
    reason = keep_decision(bundle, finality_rpc() if callable(finality_rpc) else finality_rpc, cfg)
    if reason is not None:
        return bundle, f"not kept: {reason}"
    try:
        store.put_bundle(cfg, tx_hash, bundle)
    except Exception as exc:
        return bundle, f"not kept: cache error ({type(exc).__name__}: {exc})"
    return bundle, "stored"
