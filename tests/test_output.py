import json
from types import SimpleNamespace

import pytest

from anychain.render import render_markdown
from anychain.writer import WriterError, evidence_payload, write_explanation
from tests.conftest import USDC_TX, replay_bundle


class FakeClient:
    """Stands in for the Anthropic SDK; records what it was sent."""

    def __init__(self, reply="ok [E1]", fail=False):
        self.reply, self.fail, self.sent = reply, fail, None
        self.messages = self

    def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.sent = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def test_markdown_lists_ids_and_links(eth_cfg):
    md = render_markdown(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer"))
    assert "**[E1]**" in md and "https://eth.blockscout.com/tx/" in md
    assert f"`eth_getTransactionReceipt [{USDC_TX}]`" in md  # the exact RPC call, not just the node URL


def test_writer_sends_only_evidence_and_uses_config_model(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    client = FakeClient()
    assert write_explanation(bundle, eth_cfg, "support", client) == "ok [E1]"
    assert client.sent["model"] == eth_cfg.llm.model
    assert client.sent["messages"][0]["content"] == evidence_payload(bundle, eth_cfg)
    payload = json.loads(evidence_payload(bundle))
    assert payload["evidence"][0]["sources"][0]["url"].startswith("https://eth.blockscout.com/tx/")
    every_url = [src.get("url") for ev in payload["evidence"] for src in ev["sources"] if src.get("url")]
    assert all(u.startswith("https://eth.blockscout.com/tx/") for u in every_url)  # no API or RPC URLs
    assert any("eth_getTransactionReceipt" in src.get("detail", "") for ev in payload["evidence"] for src in ev["sources"])
    assert "pt-BR" in client.sent["system"] and "Mode: support" in client.sent["system"]


def test_writer_failure_is_a_writer_error(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    with pytest.raises(WriterError):
        write_explanation(bundle, eth_cfg, "developer", FakeClient(fail=True))


def test_gap_texts_reach_the_llm_without_endpoint_urls(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={"ethereum-rpc.publicnode.com"})
    assert any("publicnode" in g.why for g in bundle.gaps)  # the user-facing gap names the endpoint...
    payload = evidence_payload(bundle, eth_cfg)
    assert "publicnode" not in payload and "[endpoint]" in payload  # ...the model does not see it
