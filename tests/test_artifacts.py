"""PHASE2_5 T3 (R3): compiled ABIs from a repository's artifacts, for the contract pinned in address_map."""
import json

from anychain.collectors.repo import ArtifactIndex
from anychain.config import load_config
from tests.conftest import ROOT, mutated, replay_bundle
from tests.test_golden import _case

SAFE_REPO = "https://github.com/safe-global/safe-deployments"
FACTORY, SINGLETON = "0x4e1DCf7AD4e460CfD30791CCC4F9c8a4f820ec67", "0x41675C099F32341bf84BFc5382aF534df5C7461a"
NEW_SAFE = "0x9542f2eC81233818a18346cA2D1E673aA7db7E12"
ABI = [{"type": "function", "name": "f", "inputs": [], "outputs": [], "stateMutability": "nonpayable"}]


def _write(root, path, data):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))


def test_artifact_shapes(tmp_path):
    _write(tmp_path, "artifacts/contracts/A.sol/A.json", {"contractName": "A", "abi": ABI})  # Hardhat
    _write(tmp_path, "artifacts/contracts/A.sol/A.dbg.json", {"buildInfo": "x"})  # Hardhat debug file: no ABI
    _write(tmp_path, "out/B.sol/B.json", {"abi": ABI, "bytecode": {}})  # Foundry: the name is the file's
    _write(tmp_path, "abi/C.json", ABI)  # a plain ABI list
    _write(tmp_path, "abi/broken.json", "{not json")
    index = ArtifactIndex.from_dir(tmp_path, ["artifacts/**/*.json", "out/**/*.json", "abi/*.json"])
    assert index.find("A") == ("artifacts/contracts/A.sol/A.json", ABI)
    assert index.find("B") == ("out/B.sol/B.json", ABI)
    assert index.find("C") == ("abi/C.json", ABI)
    assert index.find("A.dbg") is None and index.find("broken") is None and index.find("Missing") is None


def test_two_different_abis_for_one_name_are_ambiguous(tmp_path):
    _write(tmp_path, "abi/v1/T.json", ABI)
    _write(tmp_path, "abi/v2/T.json", ABI + [{"type": "event", "name": "E", "inputs": [], "anonymous": False}])
    _write(tmp_path, "abi/v3/U.json", ABI)
    _write(tmp_path, "abi/v4/U.json", ABI)  # the same ABI twice is not ambiguous
    index = ArtifactIndex.from_dir(tmp_path, ["abi/**/*.json"])
    assert index.find("T") is None and "T" in index.ambiguous
    assert index.find("U") is not None


def _eth():
    return load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))


def _unverified():
    """The real Safe deployment, with the explorer's ABI and source removed for the factory, the singleton and
    the new Safe proxy: as if none was verified."""
    def strip(meta):
        for k in ("abi", "source_code", "additional_sources", "implementations"):
            meta.pop(k, None)
    overrides = {}
    for address in (FACTORY, SINGLETON, NEW_SAFE):
        overrides.update(mutated("eth_safe_deploy", f"/smart-contracts/{address}", strip))
    return overrides


def _safe(cfg=None, **kw):
    _config, tx = _case("eth_safe_deploy")
    return replay_bundle(cfg or _eth(), tx, "eth_safe_deploy", **kw)


def test_the_config_pins_the_safe_deployments_from_the_repo_itself():
    cfg = _eth()
    assert cfg.address_map[FACTORY.lower()].contract == "SafeProxyFactory"
    assert cfg.address_map[SINGLETON.lower()].contract == "Safe"
    # the canonical addresses are the repository's own listing (src/assets/v1.4.1, at the pinned commit)
    repo = ROOT / "tests" / "fixtures_repos" / "repos" / \
        "safe-global__safe-deployments__7b1fb6d615ab2d2999550ec9166554b180e813e5" / "src" / "assets" / "v1.4.1"
    for file, address in (("safe_proxy_factory.json", FACTORY), ("safe.json", SINGLETON)):
        assert json.loads((repo / file).read_text())["deployments"]["canonical"]["address"] == address


def test_a_pinned_contract_without_explorer_abi_is_decoded_from_the_artifact():
    b = _safe(overrides=_unverified())
    [call] = [e for e in b.items if e.kind == "call"]
    assert call.text.startswith("Called createProxyWithNonce(address,bytes,uint256) on ")
    assert "_singleton=0x41675C099F32341bf84BFc5382aF534df5C7461a" in call.text
    assert "compiled ABI src/assets/v1.4.1/safe_proxy_factory.json" in call.text and call.confidence == "single_source"
    assert any(s.kind == "repo" and s.url.endswith("/blob/7b1fb6d615ab2d2999550ec9166554b180e813e5/src/assets/v1.4.1/"
                                                    "safe_proxy_factory.json") for s in call.sources)
    events = {e.data.get("event") for e in b.items if e.kind == "event"}
    assert "ProxyCreation" in events  # emitted by the pinned factory


def test_with_the_explorer_abi_the_explorer_still_wins():
    [call] = [e for e in _safe().items if e.kind == "call"]
    assert "ABI source: explorer" in call.text


def test_artifacts_left_out_of_the_order_are_not_read():
    cfg = _eth().model_copy(deep=True)
    cfg.abi_strategy.order = ["explorer", "repo_source_signatures", "signature_db"]
    b = _safe(cfg, overrides=_unverified())
    [call] = [e for e in b.items if e.kind == "call"]
    assert "createProxyWithNonce" not in call.text  # the selector only
    assert any(g.cause == "config_error" and "SafeProxyFactory" in g.why for g in b.gaps)


# ---- review of T3 ----

def test_the_artifacts_own_address_list_proves_the_pin():
    b = _safe(overrides=_unverified())
    [call] = [e for e in b.items if e.kind == "call"]
    assert "which lists this address for chain 1" in call.text
    [event] = [e for e in b.items if e.kind == "event" and e.data.get("event") == "ProxyCreation"]
    assert event.text.startswith("Event ProxyCreation(address,address) emitted by ") and event.confidence == "single_source"
    assert any(s.kind == "repo" and s.url.endswith("safe_proxy_factory.json") for s in event.sources)


def test_a_pin_the_artifact_contradicts_decodes_nothing():
    from anychain.config import AddressMapEntry
    cfg = _eth().model_copy(deep=True)
    cfg.address_map[FACTORY.lower()] = AddressMapEntry(repo=SAFE_REPO, contract="Safe")  # the wrong contract
    b = _safe(cfg, overrides=_unverified())
    [call] = [e for e in b.items if e.kind == "call"]
    assert "createProxyWithNonce" not in call.text
    [gap] = [g for g in b.gaps if g.what == "ABI lookup" and FACTORY in g.why]
    assert gap.cause == "config_error" and "0x41675C099F32341bf84BFc5382aF534df5C7461a" in gap.why


def test_deployment_files_are_chosen_by_the_address_they_list(tmp_path):
    other = ABI + [{"type": "event", "name": "E", "inputs": [], "anonymous": False}]
    _write(tmp_path, "deployments/goerli/Token.json", {"address": "0x" + "11" * 20, "abi": other})  # hardhat-deploy
    _write(tmp_path, "deployments/mainnet/Token.json", {"address": "0x" + "22" * 20, "abi": ABI})
    index = ArtifactIndex.from_dir(tmp_path, ["deployments/**/*.json"])
    found = index.match("Token", "0x" + "22" * 20, 1)
    assert found.path == "deployments/mainnet/Token.json" and found.abi == ABI and found.listed
    assert isinstance(index.match("Token", "0x" + "33" * 20, 1), str)  # listed elsewhere: a problem, no ABI


def test_truffle_networks_and_foundry_versions(tmp_path):
    _write(tmp_path, "build/contracts/V.json", {"contractName": "V", "abi": ABI,
                                                "networks": {"1": {"address": "0x" + "44" * 20}}})
    _write(tmp_path, "out/W.sol/W.0.8.19.json", {"abi": ABI})  # Foundry, several compiler versions
    index = ArtifactIndex.from_dir(tmp_path, ["build/**/*.json", "out/**/*.json"])
    assert index.match("V", "0x" + "44" * 20, 1).listed
    assert isinstance(index.match("V", "0x" + "44" * 20, 10), str)  # not listed for chain 10
    assert index.find("W") == ("out/W.sol/W.0.8.19.json", ABI)


def test_build_info_is_never_read(tmp_path):
    _write(tmp_path, "artifacts/build-info/abc.json", "{not json at all, and huge in real repos")
    _write(tmp_path, "artifacts/contracts/A.sol/A.json", {"contractName": "A", "abi": ABI})
    import anychain.collectors.repo as repo_module
    seen = []
    real = repo_module.Path.read_text
    try:
        repo_module.Path.read_text = lambda self, *a, **k: (seen.append(self.name), real(self, *a, **k))[1]
        ArtifactIndex.from_dir(tmp_path, ["artifacts/**/*.json"])
    finally:
        repo_module.Path.read_text = real
    assert seen == ["A.json"]


def test_a_replay_custom_error_is_decoded_with_a_pinned_artifact(monkeypatch):
    from types import SimpleNamespace

    from anychain.bundle import BundleBuilder
    from anychain.decoder import AbiDecoder
    repo = SimpleNamespace(label="o/r@abc1234", file_url=lambda path: f"https://github.com/o/r/blob/abc/{path}")

    def fake(self, address):  # an artifact pinned to the contract, declaring the replay's error 0x40206e43
        self.abi_origin[address.lower()] = ("repo_artifact", repo, "abi/C.json")
        abi = [{"type": "error", "name": "Whatever", "inputs": [{"type": "uint256", "name": "x"}]}]
        decoder = AbiDecoder(abi)
        decoder.errors[bytes.fromhex("40206e43")] = abi[0]
        return decoder, "repository o/r@abc1234: compiled ABI abi/C.json (C, pinned to this address in the config)"
    monkeypatch.setattr(BundleBuilder, "_repo_decoder", fake)
    _config, tx = _case("zksync_fail_generic")
    b = replay_bundle(load_config(str(ROOT / "configs" / "zksync-era.yaml")), tx, "zksync_fail_generic")
    [replay] = [e for e in b.items if e.kind == "replay"]
    assert "Whatever(" in replay.text and any(s.url and s.url.endswith("abi/C.json") for s in replay.sources)
