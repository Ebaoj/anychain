"""Record the public signature database's real answers for every selector and topic the test recordings
ask about, into tests/fixtures_signatures/signatures.json (the offline tests read only that file).

Usage: uv run python scripts/record_signatures.py
"""
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from anychain.collectors import repo as repo_module  # noqa: E402
from anychain.collectors import signatures as sig_module  # noqa: E402
from anychain.collectors.http import USER_AGENT  # noqa: E402
from anychain.config import load_config  # noqa: E402
from tests.conftest import REPO_CACHE, SIGNATURES, replay_bundle  # noqa: E402
from tests.test_golden import FIXTURE_NAMES, _case  # noqa: E402

real_init = sig_module.SignatureDb.__init__


def live(self, cfg, _cache_dir, _client=None, budget=None, timeout_s=10):
    # the replay transport only knows recorded explorer and node answers: the database is asked for real
    real_init(self, cfg, SIGNATURES, httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True),
              None, 20)


sig_module.SignatureDb.__init__ = live
repo_module.RepoCache.__init__ = lambda self, _d: setattr(self, "root", REPO_CACHE)
for fixture in FIXTURE_NAMES:
    config, tx_hash = _case(fixture)
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    offline = {cfg.explorer.base_url.split("//")[1]} if "rpc_only" in fixture else None
    bundle = replay_bundle(cfg, tx_hash, fixture, offline_hosts=offline)
    found = [e for e in bundle.items if e.kind == "candidate"]
    print(f"{fixture}: {len(found)} candidate fact(s)", flush=True)
