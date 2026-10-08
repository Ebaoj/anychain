"""Local API (PHASE3 T2, R1 and R5, D46): the same answers as the command line, over HTTP.

  GET  /health    the active network, and whether its explorer, node and model are usable now
  POST /explain   {"hash", "mode"?, "fresh"?, "write"?} -> the structured answer (anychain.answer/1) + run_id
  POST /feedback  {"run_id", "value": "up" | "down"} -> saved on that answer's row (the page's thumbs)

Served by `anychain serve` on 127.0.0.1 only: there is no authentication (out of scope in the case), so it must
not be reachable from another machine, and it answers only requests addressed to this machine by name (a web page
whose own name resolves to 127.0.0.1, "DNS rebinding", is refused). Error details never show an endpoint's URL:
an RPC URL can carry the provider's key.
"""
import re
import shutil
import time
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from anychain.bundle import InvalidHashError, build_bundle
from anychain.collectors.http import USER_AGENT, Budget, CollectorError, request_json
from anychain.events import NullEventLog, RunEvent
from anychain.collectors.rpc import RpcClient
from anychain.config import AppConfig
from anychain.service import SKIP, WRITE, Crash, answer_transaction
from anychain.writer import write_checked

LOCAL_NAMES = ["127.0.0.1", "localhost", "[::1]", "::1"]
PROBE_BUDGET_S = 15  # all of /health's probes together
URL = re.compile(r"(?:https?|wss?)://\S+")


def no_urls(text: str, hidden_hosts: tuple[str, ...] = ()) -> str:
    """An error text without URLs, and without the node's host name even when written bare (an RPC URL can carry
    the provider's key; errors also name the host alone, e.g. "outage of rpc.example.com")."""
    text = URL.sub("<endpoint>", text)
    for host in hidden_hosts:
        if host:
            text = text.replace(host, "<node>")
    return text


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelled field is refused, not silently dropped
    hash: str = Field(..., description="Transaction hash (0x + 64 hex)")
    mode: Literal["support", "developer", "auditor"] | None = Field(None, description="Default: the config's")
    fresh: bool = Field(False, description="Fetch everything again, ignoring the cache")
    write: bool = Field(True, description="Write the summary with the model (false: evidence only)")


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int
    value: Literal["up", "down"]


def probe_explorer(cfg: AppConfig, client: httpx.Client, budget: Budget | None = None) -> dict:
    """The explorer answers its /stats endpoint (small: about 700 bytes on Blockscout, checked 2026-10-08)."""
    url = f"{cfg.explorer.base_url.rstrip('/')}{cfg.explorer.api_path}/stats"
    started = time.monotonic()
    try:
        request_json(client, "GET", url, timeout_s=min(cfg.explorer.timeout_s, 10), budget=budget)
    except CollectorError as exc:
        return {"status": "down", "detail": no_urls(str(exc))[:200], "ms": _ms(started)}
    return {"status": "ok", "ms": _ms(started)}


def probe_node(cfg: AppConfig, client: httpx.Client, budget: Budget | None = None) -> dict:
    """The node answers and is on the configured chain (eth_chainId)."""
    started = time.monotonic()
    try:
        chain = RpcClient(cfg.rpc, client, budget).chain_id()
    except CollectorError as exc:
        return {"status": "down", "detail": no_urls(str(exc), (httpx.URL(cfg.rpc.url).host,))[:200],
                "ms": _ms(started)}
    if chain != cfg.network.chain_id:
        return {"status": "wrong_chain", "detail": f"the node is on chain {chain}, the config says "
                                                   f"{cfg.network.chain_id}", "ms": _ms(started)}
    return {"status": "ok", "ms": _ms(started)}


def probe_model(cfg: AppConfig) -> dict:
    """Whether a model can be called, without calling it (each call costs): the Claude Code command is installed
    (logging in is not checked), or the API key is set (not checked against the provider)."""
    import os
    provider = cfg.llm.provider
    if provider == "claude_code":
        found = shutil.which(cfg.llm.claude_code_command)
        return {"status": "installed" if found else "missing", "provider": provider, "model": cfg.llm.model,
                "detail": "the command is installed; whether it is logged in is known only when it writes" if found
                else f"{cfg.llm.claude_code_command!r} not found"}
    key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    present = bool(key and os.environ.get(key))
    return {"status": "configured" if present else "missing", "provider": provider, "model": cfg.llm.model,
            **({} if present else {"detail": f"{key} is not set"})}


def create_app(cfg: AppConfig, *, log, store, build=build_bundle, write_fn=write_checked, finality=None,
               probe_client: httpx.Client | None = None, record=None) -> FastAPI:
    """The app for one network's config. Collaborators are passed in, so tests replay recorded traffic."""
    from contextlib import asynccontextmanager
    client = probe_client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        client.close()  # one probe client for the app's life, closed when it stops
    app = FastAPI(title="AnyChain Transaction Assistant", version="phase3", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_NAMES)
    finality = finality or (lambda: RpcClient(cfg.rpc))
    record = record or _record
    node = (httpx.URL(cfg.rpc.url).host,)

    @app.get("/health")
    def health() -> dict:
        budget = Budget(PROBE_BUDGET_S)
        explorer, node = probe_explorer(cfg, client, budget), probe_node(cfg, client, budget)
        # overall: whether answers can be given (the sources); the model is reported apart
        status = ("ok" if explorer["status"] == node["status"] == "ok" else
                  "degraded" if "ok" in (explorer["status"], node["status"]) else "down")
        return {"network": {"name": cfg.network.name, "chain_id": cfg.network.chain_id,
                            "explorer": cfg.explorer.base_url},
                "status": status, "sources": {"explorer": explorer, "node": node, "model": probe_model(cfg)}}

    @app.post("/explain")
    def explain(request: ExplainRequest) -> dict:
        mode = request.mode or cfg.assistant.default_mode
        started = time.monotonic()
        try:
            result = answer_transaction(request.hash, cfg, mode, write=WRITE if request.write else SKIP,
                                        fresh=request.fresh, source="api", log=log, store=store, build=build,
                                        write_fn=write_fn, finality=finality, record=record)
            out = result.structured()
        except InvalidHashError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Crash as exc:  # already logged by the service
            raise HTTPException(500, no_urls(f"Unexpected error while collecting data ({exc}). It was logged.",
                                             node)) from exc
        except Exception as exc:  # after collecting: log it too, never a traceback
            record(log, RunEvent.crash(cfg.network.name, request.hash.strip(), "api", _ms(started),
                                       f"{type(exc).__name__}: {exc}"))
            raise HTTPException(500, no_urls(f"Unexpected error ({type(exc).__name__}: {exc}). It was logged.",
                                             node)) from exc
        if result.writer_error:  # the command line prints it on stderr; here it travels with the answer
            out["writer_error"] = no_urls(result.writer_error, node)
        return out

    @app.post("/feedback")
    def feedback(request: FeedbackRequest) -> dict:
        if isinstance(log, NullEventLog):
            raise HTTPException(503, "the event log is off (storage.event_sink: none): feedback cannot be saved")
        if not log.set_feedback(request.run_id, request.value, source="api"):
            raise HTTPException(404, f"no answer given by this API with run_id {request.run_id}")
        return {"saved": True}

    return app


def _record(log, event) -> int | None:
    try:
        return log.record(event)
    except Exception:  # logging must never cost the answer
        return None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
