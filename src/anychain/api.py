"""Local API (PHASE3 T2, R1 and R5, D46): the same answers as the command line, over HTTP.

  GET  /health    the active network, and whether its explorer, node and model are usable now
  POST /explain   {"hash", "mode"?, "fresh"?, "write"?} -> the structured answer (anychain.answer/1) + run_id
  POST /feedback  {"run_id", "value": "up" | "down"} -> saved on that answer's row (the page's thumbs)

Served by `anychain serve` on 127.0.0.1 only: there is no authentication (out of scope in the case), so it must
not be reachable from another machine, and it answers only requests addressed to this machine by name (a web page
whose own name resolves to 127.0.0.1, "DNS rebinding", is refused). Error details never show an endpoint's URL:
an RPC URL can carry the provider's key.
"""
import shutil
import time
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from anychain.bundle import InvalidHashError, build_bundle
from anychain.collectors.http import USER_AGENT, Budget, CollectorError, request_json
from anychain.events import NullEventLog, RunEvent
from anychain.redact import no_urls
from anychain.collectors.rpc import RpcClient
from anychain.config import AppConfig
from anychain.chat import MAX_QUESTIONS
from anychain.service import SKIP, WRITE, Crash, answer_transaction
from anychain.writer import write_checked

LOCAL_NAMES = ["127.0.0.1", "localhost", "[::1]", "::1"]
PAGE = Path(__file__).parent / "web" / "index.html"


def _page_policy() -> str:
    """The page loads nothing from elsewhere and talks only to this API; it cannot be framed by another site. Its one
    inline script and one style block are allowed by their hash, so nothing else inline can run (review of T4)."""
    import base64
    import hashlib
    page = PAGE.read_text(encoding="utf-8")

    def digest(tag: str) -> str:
        code = page.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
        return "'sha256-" + base64.b64encode(hashlib.sha256(code.encode()).digest()).decode() + "'"
    return (f"default-src 'none'; script-src {digest('script')}; style-src {digest('style')}; connect-src 'self'; "
            "img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


PAGE_POLICY = _page_policy()
PROBE_BUDGET_S = 15  # all of /health's probes together



class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelled field is refused, not silently dropped
    hash: str = Field(..., description="Transaction hash (0x + 64 hex)")
    mode: Literal["support", "developer", "auditor"] | None = Field(None, description="Default: the config's")
    fresh: bool = Field(False, description="Fetch everything again, ignoring the cache")
    write: bool = Field(True, description="Write the summary with the model (false: evidence only)")
    question: str | None = Field(None, max_length=500, description="The reader's question, answered first (R12)")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(..., min_length=1, max_length=2000, description="The reader's question")
    session_id: str | None = Field(None, description="To continue a conversation")
    run_id: int | None = Field(None, description="Start from the facts of this /explain answer (what the page shows)")
    hash: str | None = Field(None, description="The transaction to talk about (starts a conversation)")
    mode: Literal["support", "developer", "auditor"] | None = None


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


def chat_tools(cfg: AppConfig, bundle_for):
    """The chat's tools against the network's own sources, each request within the explanation's time budget."""
    from anychain.chat import Tools
    from anychain.collectors.explorer import ExplorerClient
    budget = cfg.assistant.time_budget_s
    return Tools(cfg, rpc_factory=lambda: RpcClient(cfg.rpc, budget=Budget(budget)),
                 explorer_factory=lambda: ExplorerClient(cfg.explorer, budget=Budget(budget)), build=bundle_for)


def create_app(cfg: AppConfig, *, log, store, build=build_bundle, write_fn=write_checked, finality=None,
               probe_client: httpx.Client | None = None, record=None, backend_factory=None, tools=None,
               sessions=None) -> FastAPI:
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
    from anychain.cache import NoCache, cached_bundle
    from anychain.chat import ChatSession, Sessions, turn_event
    from anychain.writer import backend_for
    sessions = sessions or Sessions()
    explained = Recent()  # each /explain's evidence for a while, so a chat starts from the facts the page shows
    backend_factory = backend_factory or (lambda: backend_for(cfg.llm))

    def bundle_for(tx_hash: str):
        return cached_bundle(tx_hash.strip(), cfg, store or NoCache(), build, finality)[0]
    tools = tools or chat_tools(cfg, bundle_for)

    @app.get("/", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        """The page (PHASE3 T4, R2): one static file, plain JavaScript, talking only to this API."""
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Content-Security-Policy": PAGE_POLICY,
                                                        "X-Content-Type-Options": "nosniff"})

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
                                        write_fn=write_fn, finality=finality, record=record,
                                        question=request.question)
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
        if result.run_id is not None:
            explained.put(result.run_id, (result.bundle, mode))
        return out

    @app.post("/chat")
    def chat(request: ChatRequest) -> dict:
        started = time.monotonic()
        if request.session_id:
            session = sessions.get(request.session_id)
            if session is None:
                raise HTTPException(404, "no such conversation (it ended after an hour without questions): start a "
                                         "new one with the transaction's hash")
            if request.mode and request.mode != session.mode or request.hash or request.run_id is not None:
                raise HTTPException(422, f"a conversation keeps the transaction and mode it started with "
                                         f"({session.mode}): start a new one to change them")
        elif request.run_id is not None:
            kept = explained.get(request.run_id)
            if kept is None:
                raise HTTPException(404, f"no recent explain with run_id {request.run_id}: explain the transaction again, "
                                         "or start with its hash")
            bundle, explain_mode = kept
            session = ChatSession(cfg, bundle.model_copy(deep=True), request.mode or explain_mode, tools)
            sessions.add(session)
        elif request.hash:
            try:
                session = ChatSession(cfg, bundle_for(request.hash), request.mode or cfg.assistant.default_mode, tools)
            except InvalidHashError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                record(log, RunEvent.crash(cfg.network.name, request.hash.strip(), "chat", _ms(started),
                                           f"{type(exc).__name__}: {exc}"))
                raise HTTPException(500, no_urls(f"Unexpected error ({type(exc).__name__}: {exc}). It was logged.",
                                                 node)) from exc
            sessions.add(session)
        else:
            raise HTTPException(422, "give session_id to continue a conversation, or run_id or hash to start one")
        try:
            turn = session.ask(request.message, backend_factory())
        except Exception as exc:  # never a traceback; logged like any crash
            record(log, RunEvent.crash(cfg.network.name, session.bundle.tx_hash, "chat", _ms(started),
                                       f"{type(exc).__name__}: {exc}"))
            raise HTTPException(500, no_urls(f"Unexpected error ({type(exc).__name__}: {exc}). It was logged.",
                                             node)) from exc
        run_id = record(log, turn_event(session, turn, "chat", _ms(started)))
        added = [e.model_dump() for e in session.bundle.items if e.id in turn.new_facts]
        started_now = len(session.turns) == 1
        return {"session_id": session.id, "answer": turn.answer, "outcome": turn.outcome, "new_facts": added,
                # the whole evidence when the conversation starts, so the page cites the same facts as the model
                "evidence": [e.model_dump() for e in session.bundle.items] if started_now else None,
                "tool_calls": turn.tool_calls, "problems": turn.problems,
                "error": no_urls(turn.error, node) if turn.error else None, "run_id": run_id,
                "questions_left": max(0, MAX_QUESTIONS - len([t for t in session.turns if t.outcome != "refused"]))}

    @app.post("/feedback")
    def feedback(request: FeedbackRequest) -> dict:
        if isinstance(log, NullEventLog):
            raise HTTPException(503, "the event log is off (storage.event_sink: none): feedback cannot be saved")
        if not (log.set_feedback(request.run_id, request.value, source="api")
                or log.set_feedback(request.run_id, request.value, source="chat")):
            raise HTTPException(404, f"no answer given by this API with run_id {request.run_id}")
        return {"saved": True}

    return app


class Recent:
    """The last `limit` items by key, safe across threads (the API's request threads)."""

    def __init__(self, limit: int = 200):
        import threading
        from collections import OrderedDict
        self.limit, self._items, self._lock = limit, OrderedDict(), threading.Lock()

    def put(self, key, value) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)

    def get(self, key):
        with self._lock:
            return self._items.get(key)


def _record(log, event) -> int | None:
    try:
        return log.record(event)
    except Exception:  # logging must never cost the answer
        return None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
