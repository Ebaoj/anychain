"""Portability: the same checks on six real networks, each chosen for what it proves.

| network          | chain_type     | proves                                                   |
| ethereum-mainnet | ethereum       | the reference, plus blob fees (EIP-4844)                 |
| optimism-mainnet | optimism       | L2 fee split, L1 deposits, L1 withdrawals                |
| gnosis-mainnet   | default        | another native currency (XDAI), the generic profile      |
| rootstock-mainnet| rsk            | an older EVM (legacy txs only), native RBTC              |
| celo-mainnet     | optimism-celo  | fees paid in a token instead of the native currency      |
| zksync-era       | zksync         | the scope's edge: a zkEVM, its L1 status and system calls |
"""
import re
from pathlib import Path

import pytest

from anychain.chains import ChainProfile, audit_fields
from anychain.config import load_config
from tests.conftest import ROOT, USDC_TX, replay_bundle

# config, fixture, tx hash: one real recorded transaction per network
NETWORK_CASES = [
    ("ethereum-mainnet", "eth_usdc_transfer", USDC_TX),
    ("optimism-mainnet", "op_usdc_transfer", "0x7db4433fc318dfcf4a8d07022aec6135a5adb69b227f5a82e3092b3a648b0921"),
    ("gnosis-mainnet", "gnosis_transfer", "0xac36f2fa8f0fc93e51ee3bee4f40a96421de08aa5f515c4602cdeb4a1ec6303b"),
    ("rootstock-mainnet", "rootstock_transfer", "0x677c354e740f6bcbbd1ef6f4ea5ed0247c0fd711b85fc9a16798a07473756351"),
    ("celo-mainnet", "celo_fee_currency", "0x4ca38efad66cf866bbadc1c91ecd3d61c524c747de68eb196d28b882b4b86aca"),
    ("zksync-era", "zksync_processed_on_l2", "0xe212432a6d3f26d21c1240c1f376d605b173d6be0b2c2f01444d0d296e57a9c3"),
]


def _cfg(name):
    return load_config(str(ROOT / "configs" / f"{name}.yaml"))


def _texts(bundle):
    return " ".join(e.text for e in bundle.items)


def _gaps(bundle, what):
    return [g for g in bundle.gaps if g.what == what]


# ---- the same checks on every network ---------------------------------------------

@pytest.mark.parametrize("config,fixture,tx_hash", NETWORK_CASES, ids=[c[0] for c in NETWORK_CASES])
def test_every_network_gives_a_sourced_answer_in_its_own_terms(config, fixture, tx_hash):
    cfg = _cfg(config)
    b = replay_bundle(cfg, tx_hash, fixture)
    assert b.status == "success" and b.network == config
    assert b.items[0].sources[0].url == cfg.explorer.tx_url(tx_hash)  # links point at this network's explorer
    assert all(s.url or s.detail for e in b.items for s in e.sources)
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.data["token"] in (cfg.network.native_symbol, "USD₮")  # never another network's currency
    assert any(e.kind == "cross_check" and e.data["agrees"] for e in b.items)  # RPC confirms on every network
    assert not _gaps(b, "Chain type") and not _gaps(b, "Network-specific details")  # profile fits payload


@pytest.mark.parametrize("config", sorted(p.stem for p in (ROOT / "configs").glob("*.yaml") if "example" not in p.name))
def test_every_shipped_config_loads_with_a_dedicated_profile(config):
    from anychain.chains import PROFILES
    assert _cfg(config).network.chain_type in PROFILES


def test_no_network_specific_values_in_code():
    """Portability by config only: no explorer/RPC domains, chain ids, symbols or addresses in src/."""
    src = "\n".join(p.read_text() for p in (ROOT / "src" / "anychain").rglob("*.py"))
    domains = set(re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", src)) - {"docs.blockscout.com"}
    assert not domains, f"hardcoded hosts: {domains}"
    assert not re.search(r"chain_id\s*(==|!=)\s*\d|chain_id\s+in\s*[\[({]\s*\d", src), "chain id literal"
    assert not re.findall(r"0x[0-9a-fA-F]{40}", src), "hardcoded address"
    symbols = {_cfg(p.stem).network.native_symbol for p in (ROOT / "configs").glob("*-*.yaml") if "example" not in p.name}
    for symbol in symbols:
        assert not re.search(rf"[\"']{symbol}[\"']", src), f"hardcoded currency {symbol}"


# ---- what each profile interprets --------------------------------------------------

def test_celo_fee_paid_in_token_is_not_called_celo():
    b = replay_bundle(_cfg("celo-mainnet"), NETWORK_CASES[4][2], "celo_fee_currency")
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.text.startswith("Fee paid: 0.003604051072171875 USD₮")
    assert "CELO" not in fee.text.split(";")[0]


def test_optimism_fee_is_split_and_sums_to_the_total():
    from decimal import Decimal
    b = replay_bundle(_cfg("optimism-mainnet"), NETWORK_CASES[1][2], "op_usdc_transfer")
    fee = next(e for e in b.items if e.kind == "fee")
    parts = fee.data["parts"]
    assert set(parts) == {"L2 execution", "L1 data"}
    assert sum(Decimal(v) for v in parts.values()) == Decimal(fee.data["fee"])  # exact, not just "positive"
    assert "L1 data" in fee.text and "in total" in fee.text


def test_optimism_withdrawal_and_system_transaction_are_stated():
    cfg = _cfg("optimism-mainnet")
    w = replay_bundle(cfg, "0x300df618a29d14ade1e644d22f8ac134bcbe99ac64e0aa2ca2db3309522f8217", "op_withdrawal")
    assert "Started a withdrawal to L1" in _texts(w) and "'Ready to prove'" in _texts(w)
    # This fixture is the sequencer's L1 attributes transaction, not a user deposit: the
    # text must not claim it came from L1 through the bridge.
    d = replay_bundle(cfg, "0x00cadc051b2e3894e3f7c953db6eca56e6853e325a84b5ba25c4db1f80b14be5", "op_deposit")
    assert "Deposit-type transaction (OP Stack type 126): not signed by an L2 account" in _texts(d)
    assert "created on L1" not in _texts(d)
    assert "(OP Stack depositor system account (sequencer, not a user))" in _texts(d)


def test_ethereum_blob_fee_is_added_to_the_execution_fee():
    b = replay_bundle(_cfg("ethereum-mainnet"), "0x77215ad174af1162134ff6f99bd9e118bf87ca2927db8e341aa394d7ce1728b6",
                      "eth_blob")
    fee = next(e for e in b.items if e.kind == "fee")
    # Real numbers: execution 97062677942394 wei + blob 10417524703232 wei.
    assert fee.data["fee"] == "0.000107480202645626"
    assert "Carried 2 blob(s)" in _texts(b)


def test_zksync_reports_its_l1_status_and_names_system_contracts():
    cfg = _cfg("zksync-era")
    final = replay_bundle(cfg, "0x669f390346545904cb53a9934f6140d054675fe5463f3dc71624bb3e4f351a21",
                          "zksync_executed_on_l1")
    assert "'Executed on L1' (batch 499177)" in _texts(final) and "executed in L1 transaction 0x7b254d92" in _texts(final)
    fresh = replay_bundle(cfg, NETWORK_CASES[5][2], "zksync_processed_on_l2")
    assert "not yet committed to L1" in _texts(fresh)
    assert "(zkSync bootloader: collects fees and pays refunds)" in _texts(fresh)


# ---- wrong or missing chain_type ---------------------------------------------------

def test_wrong_chain_type_is_flagged():
    cfg = _cfg("celo-mainnet")
    cfg.network.chain_type = "default"
    b = replay_bundle(cfg, NETWORK_CASES[4][2], "celo_fee_currency")
    flagged = {g.why.split("typical of ")[1].split(" ")[0] for g in _gaps(b, "Chain type")}
    assert flagged == {"'optimism'", "'optimism-celo'"}
    assert "Fee paid: 0.003604051072171875 CELO" in _texts(b)  # without the profile the fee is read as native...
    assert _gaps(b, "Chain type")  # ...which is exactly why the mismatch is reported


def test_chain_type_without_profile_falls_back_and_says_so():
    cfg = _cfg("gnosis-mainnet")
    cfg.network.chain_type = "arbitrum"
    b = replay_bundle(cfg, NETWORK_CASES[2][2], "gnosis_transfer")
    assert b.status == "success"
    assert "no dedicated profile" in _gaps(b, "Network-specific details")[0].why


def test_unknown_structured_fields_are_declared():
    tx = {"hash": "0x1", "fee": {"value": "1"}, "arbitrum": {"batch_number": 7}, "brand_new_scalar": "x"}
    audit = audit_fields(tx, ChainProfile())
    assert audit.unknown == ["arbitrum"]  # a block of chain data is declared; a plain new field is not noise
    assert audit.other_types == {}


# ---- eighth review (Fable): real cases on the new networks -------------------------

def test_celo_fee_token_without_decimals_stays_raw_and_says_so():
    tx = "0x6470963dc7a47fffeb6dee28d9ad31f6e78208be15c9919a8b193c589996404d"
    b = replay_bundle(_cfg("celo-mainnet"), tx, "celo_fee_token_no_decimals")
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.text.startswith("Fee paid: 7822697203928125 raw units of 0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B")
    assert any("decimals" in g.why for g in _gaps(b, "Fee"))


def test_revert_without_data_is_not_called_undecoded_data():
    tx = "0xf354bf88343e794ca6f4574416778ef41bd06a51f32fb5c1ddfc1e71404ab6d1"
    b = replay_bundle(_cfg("gnosis-mainnet"), tx, "gnosis_revert_no_data")
    assert b.status == "failed"
    assert "reverted without any revert data" in _texts(b) and "undecoded revert data" not in _texts(b)


def test_rootstock_reward_transaction_is_a_system_transaction():
    tx = "0xc56d7cb6e5acdadf71898c8a6c11a0c74d42d0d172eae3bb3b48d874ef521bee"
    b = replay_bundle(_cfg("rootstock-mainnet"), tx, "rootstock_remasc")
    assert "System transaction: sent from the zero address" in _texts(b)
    assert "Plain RBTC transfer" not in _texts(b)
    assert "(Rootstock REMASC native contract (block rewards))" in _texts(b)


def test_unknown_chain_type_value_degrades_instead_of_refusing_to_load(tmp_path):
    cfg = _cfg("gnosis-mainnet")
    cfg.network.chain_type = "celo"  # pre-OP Celo explorers used this value
    b = replay_bundle(cfg, NETWORK_CASES[2][2], "gnosis_transfer")
    assert b.status == "success"
    assert "not a Blockscout CHAIN_TYPE value this tool knows" in _gaps(b, "Chain type")[0].why
