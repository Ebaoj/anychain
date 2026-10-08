"""Public signature database lookups (PHASE2 T7, R5, D35): candidates for selectors and event topics no ABI decodes.

Entries are submitted by anyone, and different signatures can share a 4-byte selector, so everything
from here is a candidate, never the decoded call. Every entry is read (all pages), because a later
entry can fit the data as well as the canonical one, and then the answer must say both fit.

API (4byte.directory, read 2026-10-08): GET {url}/signatures/ and {url}/event-signatures/ with
hex_signature=0x<8 or 64 hex> and ordering=created_at (oldest first), answering {"count", "next",
"previous", "results": [{"id", "text_signature", "hex_signature", ...}]}. The hex_signature filter
matches by prefix, so only results whose hex_signature equals the key are kept, and only exact-length
keys are ever sent.

Answers are kept in a disk cache (storage.cache_dir/signatures.json): an empty answer is asked again
after a week, others after a month (new submissions can add colliding texts). Lookups have their own
small time budget, and after one failure none are made for the rest of the explanation.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from anychain.collectors.http import Budget, CollectorError, request_json
from anychain.config import SignatureDbConfig

EMPTY_TTL_S = 7 * 24 * 3600
FOUND_TTL_S = 30 * 24 * 3600
MAX_PAGES = 5  # 100 entries a page; a selector with more is very unusual
LOOKUP_BUDGET_S = 8  # for all lookups of one explanation, apart from the explanation's own budget
FUNCTION_KEY = re.compile(r"^0x[0-9a-f]{8}$")
EVENT_KEY = re.compile(r"^0x[0-9a-f]{64}$")


class SignatureDb:
    def __init__(self, cfg: SignatureDbConfig, cache_dir: str | Path, client: httpx.Client | None = None,
                 budget: Budget | None = None, timeout_s: float = 10, offline: bool = False):
        """`offline`: answer from the cache only (tests). `budget` is ignored in favour of a small one of
        its own, so candidate lookups can never use up the explanation's time."""
        self.cfg, self.client, self.timeout_s, self.offline = cfg, client, timeout_s, offline
        self.budget = Budget(LOOKUP_BUDGET_S)
        self.path = Path(cache_dir) / "signatures.json"
        self._lock = threading.Lock()
        self.failed = False  # set after a failed lookup: no more lookups in this explanation

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.url)

    def functions(self, selector: str) -> list[str]:
        """Text signatures for a 4-byte selector (functions and custom errors share this namespace)."""
        return self._lookup("function", "signatures", selector.lower(), FUNCTION_KEY)

    def events(self, topic: str) -> list[str]:
        return self._lookup("event", "event-signatures", topic.lower(), EVENT_KEY)

    def _lookup(self, kind: str, endpoint: str, key: str, shape: re.Pattern) -> list[str]:
        if not self.enabled or not shape.match(key):
            return []
        hit = self._read().get(f"{kind}:{key}")
        if isinstance(hit, dict) and isinstance(hit.get("signatures"), list) and isinstance(hit.get("at"), (int, float)):
            ttl = FOUND_TTL_S if hit["signatures"] else EMPTY_TTL_S
            if self.offline or time.time() - hit["at"] < ttl:
                return hit["signatures"]
        if self.offline or self.failed:
            return []
        try:
            signatures = self._fetch_all(endpoint, key)
        except CollectorError:
            self.failed = True
            raise
        self._write(f"{kind}:{key}", {"signatures": signatures, "at": time.time()})
        return signatures

    def _fetch_all(self, endpoint: str, key: str) -> list[str]:
        client = self.client or httpx.Client(timeout=self.timeout_s, follow_redirects=True)
        url, params, found = f"{self.cfg.url.rstrip('/')}/{endpoint}/", {"hex_signature": key, "ordering": "created_at"}, []
        for _page in range(MAX_PAGES):
            payload = request_json(client, "GET", url, params=params, timeout_s=self.timeout_s, budget=self.budget)
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise CollectorError(f"the signature database answered an unexpected shape for {key}", retryable=False)
            found += [r for r in results if isinstance(r, dict) and isinstance(r.get("text_signature"), str)
                      and str(r.get("hex_signature", "")).lower() == key]  # the filter matches by prefix
            nxt = payload.get("next")
            if not isinstance(nxt, str) or not nxt:
                break
            url, params = nxt, None
        found.sort(key=lambda r: r.get("id") or 0)  # oldest submission first
        return list(dict.fromkeys(r["text_signature"] for r in found))

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}  # a damaged cache is only a cache: ask again

    def _write(self, key: str, value: dict) -> None:
        with self._lock:
            try:
                cache = self._read()
                cache[key] = value
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(cache, indent=1, sort_keys=True))
                os.replace(tmp, self.path)
            except OSError:
                pass  # no cache this time; the answer was still used
