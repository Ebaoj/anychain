"""PHASE2 T6 (R4): configured repos and verified source, on real BRLC transactions and files."""
import io
import tarfile

import httpx
import pytest

from anychain.collectors.http import CollectorError
from anychain.collectors.repo import RepoCache
from anychain.config import RepoConfig, load_config
from tests.conftest import ROOT, replay_bundle

BRLC = "https://github.com/cloudwallk/brlc-token"
COMMIT = "74a5498d04b10dc882e48273a38b98b4275463bd"
SET_PAUSER = "0x610935d36b23bd879133c5344f805bc144b1c254ab78cefae8db79dcb24659ef"
TRANSFER = "0x0e755eee36ca55ca55fc386f55bb942acc85daaac17f908fe4eaac3cf968ca39"


def _eth():
    return load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))


def _sources(b):
    return [e for e in b.items if e.kind == "source"]


def test_function_from_the_repo_matches_the_verified_source():
    b = replay_bundle(_eth(), SET_PAUSER, "eth_brlc_set_pauser")
    [fact] = _sources(b)
    assert ("setPauser(address) of BRLCTokenBridgeable is defined in PausableExtUpgradeable "
            "(contracts/base/common/PausableExtUpgradeable.sol, lines 103-111)") in fact.text
    assert "same in the contract's verified source" in fact.text and fact.confidence == "single_source"
    repo_links = [s.url for s in fact.sources if s.kind == "repo"]
    assert repo_links == [f"{BRLC}/blob/{COMMIT}/contracts/base/common/PausableExtUpgradeable.sol#L103-L111"]


def test_function_outside_the_repo_and_a_different_deployed_version_are_said():
    # In the repo, transfer comes from OpenZeppelin; the deployed (verified) code defines it in
    # BRLCTokenBase, which this repo version does not have.
    b = replay_bundle(_eth(), TRANSFER, "eth_brlc_transfer")
    [fact] = _sources(b)
    assert "no implemented external or public function with selector 0xa9059cbb was found" in fact.text
    assert "its bases outside the repository: ERC20Upgradeable" in fact.text
    assert "BRLCTokenBase" in fact.text and "so its code is not the deployed code" in fact.text


def test_reason_is_located_in_the_verified_source():
    from tests.test_golden import _case
    _config, tx = _case("rootstock_fail_slippage")
    b = replay_bundle(load_config(str(ROOT / "configs" / "rootstock-mainnet.yaml")), tx, "rootstock_fail_slippage")
    [fact] = [e for e in _sources(b) if "reason" in e.data]
    assert "V3SwapRouter.sol, line 220" in fact.text  # where 'Too much requested' is raised in the deployed code
    assert len(fact.sources) == len({s.url for s in fact.sources})


def test_a_repo_not_synced_is_a_config_gap(monkeypatch, tmp_path):
    from anychain.collectors import repo as repo_module
    monkeypatch.setattr(repo_module.RepoCache, "__init__", lambda self, _d: setattr(self, "root", tmp_path))
    b = replay_bundle(_eth(), SET_PAUSER, "eth_brlc_set_pauser")
    [gap] = [g for g in b.gaps if g.what == "Source code"]
    assert gap.cause == "config_error" and "anychain repos sync" in gap.needed and not _sources(b)


def test_only_github_repos():
    with pytest.raises(CollectorError, match="only GitHub"):
        RepoCache("x").load(RepoConfig(url="https://gitlab.com/a/b", ref=COMMIT))


def _tarball(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_sync_pins_a_branch_to_its_commit(tmp_path):
    archive = _tarball({"repo-top/contracts/A.sol": "contract A { function f() external {} }"})
    asked = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: asked.append(str(r.url)) or httpx.Response(200, content=archive)))
    cache = RepoCache.__new__(RepoCache)
    cache.root = tmp_path
    cfg = RepoConfig(url="https://github.com/acme/tokens", ref="main")
    commit = cache.sync(cfg, client, resolve=lambda url, ref: "a" * 40)
    assert commit == "a" * 40 and asked == [f"https://codeload.github.com/acme/tokens/tar.gz/{'a' * 40}"]
    repo = cache.load(cfg)
    assert repo.commit == "a" * 40 and "A" in repo.index.contracts
    assert repo.permalink("contracts/A.sol", 1, 1) == f"https://github.com/acme/tokens/blob/{'a' * 40}/contracts/A.sol#L1-L1"


def test_sync_refuses_paths_that_leave_the_cache(tmp_path):
    archive = _tarball({"repo-top/../../escape.sol": "x"})
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=archive)))
    cache = RepoCache.__new__(RepoCache)
    cache.root = tmp_path / "repos"
    with pytest.raises(Exception):
        cache.sync(RepoConfig(url="https://github.com/acme/tokens", ref="b" * 40), client)
    assert not (tmp_path / "escape.sol").exists()



def _meta_override(change):
    from tests.conftest import mutated
    return mutated("eth_brlc_set_pauser", "/smart-contracts/0xbEA441d7cf3f79b57cc5ae33251a084A063825dc", change)


def test_a_different_copy_of_the_function_is_said_to_differ():
    def edit(meta):
        for extra in meta["additional_sources"]:
            if extra["file_path"].endswith("PausableExtUpgradeable.sol"):
                extra["source_code"] = extra["source_code"].replace("_pauser = newPauser;", "_pauser = address(0);")
    b = replay_bundle(_eth(), SET_PAUSER, "eth_brlc_set_pauser", overrides=_meta_override(edit))
    [fact] = _sources(b)
    assert "differs from the contract's verified source" in fact.text and fact.data["same_as_verified"] is False


def test_without_verified_source_the_match_is_only_a_candidate():
    def unverify(meta):
        meta.pop("source_code", None)
        meta.pop("additional_sources", None)
    b = replay_bundle(_eth(), SET_PAUSER, "eth_brlc_set_pauser", overrides=_meta_override(unverify))
    [fact] = _sources(b)
    assert fact.confidence == "candidate" and "has no verified source" in fact.text


def test_verified_source_without_the_function_is_not_called_unverified():
    def drop(meta):
        for extra in meta["additional_sources"]:
            if extra["file_path"].endswith("PausableExtUpgradeable.sol"):
                extra["source_code"] = extra["source_code"].replace("function setPauser", "function renamedPauser")
    b = replay_bundle(_eth(), SET_PAUSER, "eth_brlc_set_pauser", overrides=_meta_override(drop))
    [fact] = _sources(b)
    assert "could not be compared" in fact.text and "has no verified source" not in fact.text


def test_reason_search_only_uses_the_repo_matched_to_the_contract():
    from tests.test_golden import _case
    _config, tx = _case("eth_failed_unverified_bot")  # an Ethereum failure unrelated to BRLC
    b = replay_bundle(_eth(), tx, "eth_failed_unverified_bot")
    assert not [e for e in _sources(b) if "brlc-token" in e.text]


class _Git:
    def __init__(self, stdout="", returncode=0, error=None):
        self.stdout, self.returncode, self.error = stdout, returncode, error

    def __call__(self, *a, **k):
        import subprocess
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(a, self.returncode, self.stdout, "")


def test_ref_resolution_takes_the_exact_branch_and_peels_tags():
    from anychain.collectors.repo import _resolve_ref
    listing = ("1" * 40 + "\trefs/heads/archive/main\n" + "2" * 40 + "\trefs/heads/main\n")
    assert _resolve_ref("u", "main", _Git(listing)) == "2" * 40
    tags = "d" * 40 + "\trefs/tags/v1\n" + "c" * 40 + "\trefs/tags/v1^{}\n"
    assert _resolve_ref("u", "v1", _Git(tags)) == "c" * 40  # the commit, not the annotated tag object


def test_ref_resolution_failures_are_clear_errors():
    import subprocess

    from anychain.collectors.repo import _resolve_ref
    with pytest.raises(CollectorError, match="no branch or tag") as err:
        _resolve_ref("u", "nope", _Git(""))
    assert err.value.retryable is False
    with pytest.raises(CollectorError, match="git is not installed"):
        _resolve_ref("u", "main", _Git(error=FileNotFoundError("git")))
    with pytest.raises(CollectorError, match="timed out"):
        _resolve_ref("u", "main", _Git(error=subprocess.TimeoutExpired("git", 60)))


def test_network_failure_while_syncing_is_a_clear_error(tmp_path):
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    cache = RepoCache.__new__(RepoCache)
    cache.root = tmp_path
    with pytest.raises(CollectorError, match="downloading"):
        cache.sync(RepoConfig(url="https://github.com/acme/tokens", ref="c" * 40),
                   httpx.Client(transport=httpx.MockTransport(boom)))


def test_a_damaged_pins_file_is_a_clear_error(tmp_path):
    cache = RepoCache.__new__(RepoCache)
    cache.root = tmp_path
    (tmp_path / "pins.json").write_text('{"half": ')
    with pytest.raises(CollectorError, match="damaged"):
        cache.load(RepoConfig(url="https://github.com/acme/tokens", ref="main"))
