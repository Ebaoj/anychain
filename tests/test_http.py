"""Retry rules: network trouble is retried, request problems are not, and the budget caps the wait."""
import httpx
import pytest

from anychain.collectors.http import Budget, CollectorError, NotFoundError, request_json


def _client(responses):
    """A client that answers with `responses` in order; an Exception item is raised."""
    calls = []

    def handler(request):
        item = responses[len(calls)]
        calls.append(request)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_retries_a_503_then_succeeds():
    client, calls = _client([httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    assert request_json(client, "GET", "https://x.test/a") == {"ok": 1}
    assert len(calls) == 2


def test_retries_a_dropped_connection():
    client, calls = _client([httpx.ConnectError("down"), httpx.Response(200, json={"ok": 1})])
    assert request_json(client, "GET", "https://x.test/a") == {"ok": 1}


def test_gives_up_after_three_attempts_and_says_retryable():
    client, calls = _client([httpx.Response(503)] * 3)
    with pytest.raises(CollectorError) as err:
        request_json(client, "GET", "https://x.test/a")
    assert len(calls) == 3 and err.value.retryable


def test_404_is_not_retried():
    client, calls = _client([httpx.Response(404)])
    with pytest.raises(NotFoundError) as err:
        request_json(client, "GET", "https://x.test/a")
    assert len(calls) == 1 and not err.value.retryable


def test_bad_request_is_not_retried():
    client, calls = _client([httpx.Response(400, text="bad param")])
    with pytest.raises(CollectorError) as err:
        request_json(client, "GET", "https://x.test/a")
    assert len(calls) == 1 and not err.value.retryable


def test_exhausted_budget_stops_before_calling():
    client, calls = _client([httpx.Response(200, json={})])
    with pytest.raises(CollectorError, match="time budget"):
        request_json(client, "GET", "https://x.test/a", budget=Budget(0))
    assert calls == []


def test_busy_rpc_node_is_retried_but_bad_request_is_not():
    from anychain.collectors.rpc import RpcClient
    from anychain.config import RpcConfig
    busy = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}})
    ok = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})
    client, calls = _client([busy, ok])
    assert RpcClient(RpcConfig(url="https://rpc.test"), client).chain_id() == 1
    assert len(calls) == 2

    bad = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid params"}})
    client, calls = _client([bad])
    with pytest.raises(CollectorError) as err:
        RpcClient(RpcConfig(url="https://rpc.test"), client).chain_id()
    assert len(calls) == 1 and not err.value.retryable


def test_network_failure_on_rpc_is_not_retried_nine_times():
    from anychain.collectors.rpc import RpcClient
    from anychain.config import RpcConfig
    client, calls = _client([httpx.ConnectError("down")] * 9)
    with pytest.raises(CollectorError):
        RpcClient(RpcConfig(url="https://rpc.test"), client).chain_id()
    assert len(calls) == 3


@pytest.mark.parametrize("answer,expected", [("0xa", 10), (10, 10), ("10", 10)])
def test_chain_id_accepts_hex_and_plain_numbers(answer, expected):
    from anychain.collectors.rpc import RpcClient
    from anychain.config import RpcConfig
    client, _ = _client([httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": answer})])
    assert RpcClient(RpcConfig(url="https://rpc.test"), client).chain_id() == expected
