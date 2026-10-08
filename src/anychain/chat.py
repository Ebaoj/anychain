"""Follow-up questions about one transaction, with read-only tools (PHASE3 T3, R3 and R4, D47).

The model never runs anything. When it needs more data it answers with a JSON object only:

    {"tools": [{"tool": "read", "contract": "0x…", "function": "balanceOf(address)", "args": ["0x…"],
                "returns": "uint256", "block": "parent"},
               {"tool": "code", "contract": "0x…", "function": "_transfer"},
               {"tool": "transaction", "hash": "0x…"}]}

Our code checks each request (fixed list of tools, address and hash formats, simple types only, a known
block), runs it, and adds what it found to the session's evidence as numbered facts with sources; a request it
refuses is told back with the reason. Then the model is asked again, and its answer goes through the same check
as every written answer (D28): retried once with the problems, else withheld. At most MAX_TOOLS tool requests per
question and MAX_QUESTIONS questions per session.
"""
import json
import re
import uuid
from dataclasses import dataclass, field

from eth_utils import is_address, to_checksum_address

import threading

from anychain.collectors.http import CollectorError
from anychain.models import EvidenceBundle, Source
from anychain.reads import Read, StateReader, UnreadableState
from anychain.redact import no_urls
from anychain.units import is_amount
from anychain.validator import allowed_urls, check_answer, evidence_ids
from anychain.writer import WriterError, add_usage, evidence_payload, load_prompt

MAX_TOOLS = 3  # tool requests per question (all rounds together)
MAX_ROUNDS = 2  # times the model may ask for tools before it must answer
MAX_QUESTIONS = 10  # per session
SIMPLE_TYPES = re.compile(r"^(address|bool|string|bytes(?:[1-9]|[12][0-9]|3[0-2])?|u?int(?:8|16|32|64|128|256)?)$")
SIGNATURE = re.compile(r"^[A-Za-z_]\w{0,63}\((.*)\)$")
HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
TOOLS = ("read", "code", "file", "transaction")
MAX_SHOWN = 200  # characters of one value shown in a fact or echoed back
MAX_FILE_LINES = 120


class Refused(Exception):
    """A tool request this code will not run, with the reason told back to the model."""


@dataclass
class Turn:
    question: str
    answer: str | None  # None: withheld or no model
    outcome: str  # ok | retried | withheld | unavailable | refused (the session is over)
    new_facts: list[str] = field(default_factory=list)  # ids added by tools this turn
    tool_calls: list[dict] = field(default_factory=list)  # each request with "result": fact ids or "refused: …"
    problems: list[str] = field(default_factory=list)
    usage: dict | None = None
    error: str | None = None


class Tools:
    """The tools, run by our code against the network's own sources."""

    def __init__(self, cfg, rpc_factory, explorer_factory, build):
        self.cfg, self.rpc_factory, self.explorer_factory, self.build = cfg, rpc_factory, explorer_factory, build

    def run(self, request: dict, bundle: EvidenceBundle, tx_block: int | None) -> list[str]:
        """Run one request; add its facts to `bundle`; return their ids. Raises Refused (also for any unexpected
        failure: a tool never ends the conversation)."""
        if not isinstance(request, dict) or request.get("tool") not in TOOLS:
            raise Refused(f"unknown tool; the tools are {', '.join(TOOLS)}")
        try:
            return getattr(self, f"_{request['tool']}")(request, bundle, tx_block)
        except Refused:
            raise
        except Exception as exc:
            raise Refused(f"the tool failed unexpectedly ({type(exc).__name__})") from exc

    def _read(self, r: dict, bundle: EvidenceBundle, tx_block: int | None) -> list[str]:
        contract = _address(r.get("contract"), "contract")
        signature = str(r.get("function") or "")
        m = SIGNATURE.match(signature)
        types = m.group(1).split(",") if m and m.group(1) else []
        if not m or any(not t for t in types):
            raise Refused("function must be a signature like balanceOf(address)")
        out = str(r.get("returns") or "")
        if not all(SIMPLE_TYPES.match(t) for t in types + [out]):
            raise Refused("only simple types are read (address, bool, string, bytes, bytesN, intN, uintN)")
        args = r.get("args") or []
        if not isinstance(args, list) or len(args) != len(types):
            raise Refused(f"{signature} takes {len(types)} argument(s)")
        args = tuple(_argument(t, a) for t, a in zip(types, args))
        block = _block(r.get("block", "parent"), tx_block)
        detail = Read(signature, contract, args, block, None).detail
        same = next((e for e in bundle.items if e.data.get("call") == detail and _same_type(e, out)), None)
        if same is not None:  # read already, as the same type (by the diagnosis, or earlier in the chat)
            return [same.id]
        try:
            read = StateReader(self.rpc_factory()).view(contract, signature, args, out, block)
        except (CollectorError, UnreadableState) as exc:
            raise Refused(f"the node could not read it: {exc}") from exc
        value = _capped(read.value)
        units, unit_text = None, ""
        if is_amount(signature) and out.startswith("uint") and isinstance(read.value, int):
            try:  # the token's own decimals and symbol at the same block: the converted amount is data (D49)
                units = StateReader(self.rpc_factory()).token_units(contract, block)
            except (CollectorError, UnreadableState):
                units = None
            if units is not None:
                unit_text = f" That is {units.amount(read.value)}, with the token's {units.described} at the same block."
        # The arguments and the block were chosen in the chat: the answer check never takes them as evidence
        # (kind chat_read, validator.py); only what the node returned counts.
        fact = bundle.add("chat_read", f"Asked in the chat: {signature.split('(')[0]}"
                          f"({', '.join(_capped(_shown(a)) for a in args)}) on {to_checksum_address(contract)} at "
                          f"block {read.block}. The node returned {value}, decoded as {out}, the type the chat asked "
                          f"for (not read from the contract's ABI); raw answer {_capped(read.raw or '')}.{unit_text}",
                          [Source(kind="rpc", label="RPC eth_call", detail=r.detail)
                           for r in [read] + (units.reads if units else [])],
                          {"call": read.detail, "value": str(value), "returns": out, "from_chat": True})
        return [fact.id]

    def _code(self, r: dict, bundle: EvidenceBundle, tx_block: int | None) -> list[str]:
        from anychain.bundle import render_code
        contract = _address(r.get("contract"), "contract")
        name = str(r.get("function") or "")
        if not re.match(r"^[A-Za-z_]\w{0,63}$", name):
            raise Refused("function must be a name like _transfer")
        explorer = self.explorer_factory()
        try:
            meta = explorer.smart_contract(contract)
        except CollectorError as exc:
            raise Refused(f"the explorer could not give its source: {exc}") from exc
        # a proxy's implementations first (the code that runs for a call to it), then the contract's own source
        places, unreachable = [], []
        for impl in meta.get("implementations") or []:
            address = impl.get("address_hash") or impl.get("address") if isinstance(impl, dict) else None
            if isinstance(address, str) and is_address(address):
                try:
                    places.append((address, explorer.smart_contract(address),
                                   f"the implementation the explorer lists today for {to_checksum_address(contract)} "
                                   f"({to_checksum_address(address)}; it may have been upgraded since this "
                                   "transaction)"))
                except CollectorError:
                    unreachable.append(to_checksum_address(address))
        places.append((contract, meta, f"{to_checksum_address(contract)}"))
        names = []
        for address, info, origin in places:
            index, contract_name = _verified_index(info)
            if index is None:
                continue
            names.append(contract_name)
            found = [(c, f) for n in index.linearization(contract_name) or [] if (c := index.contracts.get(n))
                     for f in c.functions if f.name == name and f.has_body]
            if not found:
                continue
            api = Source(kind="explorer_api", label="Explorer API: verified source",
                         url=f"{self.cfg.explorer.base_url.rstrip('/')}{self.cfg.explorer.api_path}/smart-contracts/"
                             f"{address}")
            ids = []
            for c, f in found[:3]:  # overloads: at most three
                lines = index.lines(c.path, f.start, f.end)
                fact = bundle.add("code", f"Code of {f.signature or f.name + '(…)'} in {c.name} ({c.path}, lines "
                                  f"{f.start}-{f.end}), from the verified source of {origin}, as the explorer lists "
                                  "it (asked in the chat):\n" + render_code(lines, f.start),
                                  [api], {"function": f.signature or f.name, "contract": c.name, "path": c.path,
                                          "lines": [f.start, f.end], "from_chat": True})
                ids.append(fact.id)
            return ids
        missing = f" (the source of {', '.join(unreachable)} could not be fetched)" if unreachable else ""
        if not names:
            raise Refused(f"{contract} has no verified source on the explorer{missing}")
        raise Refused(f"{', '.join(names)} and the contracts they inherit have no function {name} with a body"
                      f"{missing}")

    def _file(self, r: dict, bundle: EvidenceBundle, tx_block: int | None) -> list[str]:
        """Lines of a source file of a configured, synced repo (only files its source_globs index)."""
        from anychain.bundle import render_code
        from anychain.collectors.repo import RepoCache
        url, path = str(r.get("repo") or ""), str(r.get("path") or "")
        repo_cfg = next((c for c in self.cfg.repos if c.url == url), None)
        if repo_cfg is None:
            raise Refused(f"repo must be one of the configured repos: {', '.join(c.url for c in self.cfg.repos) or 'none'}")
        repo = RepoCache(self.cfg.storage.cache_dir).load(repo_cfg)
        if repo is None:
            raise Refused(f"{url} is not synced (anychain repos sync)")
        if path not in repo.index.texts:  # only indexed source files: no "..", no other file of the disk
            raise Refused(f"{path!r} is not one of the repo's indexed source files")
        lines = repo.index.texts[path].split("\n")
        start, end = 1, len(lines)
        wanted = r.get("lines")
        if isinstance(wanted, list) and len(wanted) == 2 and all(isinstance(n, int) and not isinstance(n, bool)
                                                                 for n in wanted):
            start, end = max(1, wanted[0]), min(len(lines), wanted[1])
        if end < start:
            raise Refused("lines must be [first, last] within the file")
        end = min(end, start + MAX_FILE_LINES - 1)
        source = Source(kind="repo", label=f"Repository {repo.label}", url=repo.permalink(path, start, end))
        fact = bundle.add("code", f"Lines {start}-{end} of {path} in {repo.label} (asked in the chat):\n"
                          + render_code(lines[start - 1:end], start), [source],
                          {"path": path, "lines": [start, end], "repo": url, "from_chat": True})
        return [fact.id]

    def _transaction(self, r: dict, bundle: EvidenceBundle, tx_block: int | None) -> list[str]:
        tx_hash = str(r.get("hash") or "")
        if not HASH.match(tx_hash):
            raise Refused("hash must be 0x and 64 hex digits")
        if tx_hash.lower() == bundle.tx_hash.lower():
            raise Refused("that is the transaction already explained")
        try:
            other = self.build(tx_hash)
        except Exception as exc:  # a broken lookup is a refusal, never a crash of the chat
            raise Refused(f"that transaction could not be explained: {type(exc).__name__}: {exc}") from exc
        ids = []
        for e in other.items:
            if e.kind in ("overview", "diagnosis"):
                fact = bundle.add("related_tx", f"Transaction {tx_hash} (asked in the chat): {e.text}", e.sources,
                                  {"tx_hash": tx_hash, "kind": e.kind, "from_chat": True}, confidence=e.confidence)
                ids.append(fact.id)
        if not ids:
            raise Refused(f"nothing is known about {tx_hash}: " + "; ".join(g.why for g in other.gaps[:2]))
        return ids


class ChatSession:
    def __init__(self, cfg, bundle: EvidenceBundle, mode: str, tools: Tools):
        self.id = uuid.uuid4().hex
        self.cfg, self.bundle, self.mode, self.tools = cfg, bundle, mode, tools
        self.turns: list[Turn] = []
        self.tx_block = next((e.data.get("block") for e in bundle.items if e.kind == "overview"), None)
        self._lock = threading.Lock()  # one question at a time: facts are numbered as they are added
        self._hidden = tuple(h for h in [_host(cfg.rpc.url)] if h)

    def ask(self, question: str, backend) -> Turn:
        with self._lock:
            return self._ask(question, backend)

    def _ask(self, question: str, backend) -> Turn:
        question = question.strip()
        if len([t for t in self.turns if t.outcome != "refused"]) >= MAX_QUESTIONS:
            return Turn(question, None, "refused", error=f"this conversation reached {MAX_QUESTIONS} questions: "
                                                          "start a new one")
        turn = Turn(question, None, "unavailable")
        tools_left, feedback, nagged = MAX_TOOLS, None, False
        try:
            for round_ in range(MAX_ROUNDS + 2):  # tool rounds, then the answer and one retry
                reply = backend.complete(self._system(), self._user(question, turn, feedback, nagged))
                turn.usage = add_usage(turn.usage, getattr(backend, "last_usage", None))
                requests = parse_tool_request(reply)
                if requests is not None and round_ < MAX_ROUNDS and tools_left > 0 and feedback is None and not nagged:
                    for request in requests[:tools_left]:
                        turn.tool_calls.append(self._run(request, turn))
                    for request in requests[tools_left:]:
                        turn.tool_calls.append({**_shown_request(request),
                                                "result": f"refused: at most {MAX_TOOLS} tool requests per question"})
                    tools_left -= min(len(requests), tools_left)
                    continue
                if requests is not None:  # out of tool rounds: it must answer from what it has (not a failed check)
                    nagged = True
                    continue
                problems = check_answer(reply, evidence_payload(self.bundle, self.cfg), allowed_urls(self.bundle),
                                        evidence_ids(self.bundle))
                if not problems:
                    turn.answer, turn.outcome = reply, "retried" if turn.problems else "ok"
                    break
                if feedback is not None and turn.problems:  # the retry failed the check too
                    turn.outcome, turn.problems = "withheld", problems
                    break
                turn.problems, feedback = problems, problems
            else:  # the rounds ran out without an answer that passed the check
                turn.outcome = "withheld"
                turn.problems = turn.problems or ["no answer from the evidence within the allowed rounds"]
        except WriterError as exc:
            turn.error = no_urls(str(exc), self._hidden)
            turn.usage = add_usage(turn.usage, exc.usage)
        self.turns.append(turn)
        return turn

    def _run(self, request, turn: Turn) -> dict:
        shown = _shown_request(request)
        known = {e.id for e in self.bundle.items}
        try:
            ids = self.tools.run(request, self.bundle, self.tx_block)
        except Refused as exc:  # told to the model and the reader: never with the node's address
            return {**shown, "result": "refused: " + no_urls(str(exc), self._hidden)}
        turn.new_facts += [i for i in ids if i not in known]  # a reused fact is not new
        return {**shown, "result": ids}

    def _system(self) -> str:
        from anychain.writer import PROMPTS_DIR
        return (load_prompt(self.mode, self.cfg.assistant.language) + "\n\n"
                + (PROMPTS_DIR / "chat.md").read_text().replace("{MAX_TOOLS}", str(MAX_TOOLS)))

    def _user(self, question: str, turn: Turn, feedback: list[str] | None, nagged: bool = False) -> str:
        parts = [evidence_payload(self.bundle, self.cfg)]
        earlier = [t for t in self.turns if t.answer]
        if earlier:
            parts.append("Earlier in this conversation:\n" + "\n".join(
                f"Reader: {t.question}\nYou: {t.answer}" for t in earlier[-4:]))
        if turn.tool_calls:
            parts.append("Your tool requests and what they gave (new evidence ids are in the evidence above):\n"
                         + json.dumps(turn.tool_calls, ensure_ascii=False))
        parts.append(f"The reader asks: {question}")
        if nagged:
            parts.append("You already used the tool rounds for this question: answer now from the evidence you have, "
                         "and say what is missing.")
        if feedback:
            parts.append("Your previous answer was rejected because it contained things that are not in the "
                         "evidence:\n" + "\n".join(f"- {p[:160]}" for p in feedback[:20])
                         + "\nAnswer again, quoting values exactly as the evidence has them.")
        return "\n\n".join(parts)


def _verified_index(meta: dict):
    """(index of a contract's verified source files, its contract name), or (None, None) when not verified."""
    from anychain.solidity import SolidityIndex
    files = {}
    if isinstance(meta.get("source_code"), str) and meta.get("file_path"):
        files[str(meta["file_path"])] = meta["source_code"]
    for extra in meta.get("additional_sources") or []:
        if isinstance(extra, dict) and isinstance(extra.get("source_code"), str) and extra.get("file_path"):
            files[str(extra["file_path"])] = extra["source_code"]
    if not files or not isinstance(meta.get("name"), str):
        return None, None
    return SolidityIndex(files), meta["name"]


def parse_tool_request(reply: str) -> list[dict] | None:
    """The tool requests when the whole reply is one JSON object {"tools": [...]} (a code fence around it is
    accepted); None when the reply is an answer."""
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)  # a fenced request, even with prose around it
    candidates = [fence.group(1)] if fence else []
    if text.startswith("{"):
        candidates.append(text)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if "tools" in data:
            tools = data["tools"]
            return list(tools) if isinstance(tools, list) else [{"tool": None}]  # a malformed request: refused
        if "tool" in data:  # one request without the wrapper
            return [data]
    return None


def _address(value, what: str) -> str:
    if not isinstance(value, str) or not is_address(value):
        raise Refused(f"{what} must be a 0x address of 40 hex digits")
    return to_checksum_address(value)


def _argument(kind: str, value):
    """A tool argument checked against its type (an address, a whole number, a boolean, hex bytes, text)."""
    if kind == "address":
        return _address(value, "an address argument")
    if kind.startswith(("uint", "int")):
        if isinstance(value, bool) or isinstance(value, float):
            raise Refused(f"{value!r} is not a whole number for {kind}")
        try:
            number = int(value, 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError):
            raise Refused(f"{value!r} is not a whole number for {kind}") from None
        bits = int(kind.lstrip("uint") or 256)
        low, high = (0, 2 ** bits - 1) if kind.startswith("uint") else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
        if not low <= number <= high:
            raise Refused(f"{number} is outside {kind}")
        return number
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise Refused("a bool argument must be true or false")
    if kind.startswith("bytes"):
        if not (isinstance(value, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value)):
            raise Refused(f"a {kind} argument must be 0x and hex digits")
        raw = bytes.fromhex(value[2:])
        size = kind[5:]
        if size and len(raw) != int(size):
            raise Refused(f"{kind} takes exactly {size} bytes")
        return raw
    if not isinstance(value, str) or len(value) > MAX_SHOWN:
        raise Refused(f"a string argument must be text of at most {MAX_SHOWN} characters")
    return value


def _block(value, tx_block: int | None) -> int:
    """"parent" (the block before the transaction: its state when it ran), "tx" (the state right after its block), or
    a block number not after the transaction's."""
    if tx_block is None:
        raise Refused("the transaction's block is not known, so no block can be chosen")
    if value in ("parent", None):
        return tx_block - 1
    if value == "tx":
        return tx_block
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= tx_block:
        return value
    raise Refused('block must be "parent", "tx", or a number up to the transaction\'s block')


def _shown(value) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return to_checksum_address(value) if isinstance(value, str) and is_address(value) else str(value)


def _shown_request(request) -> dict:
    """The request as echoed back, every value capped (a request can be as large as the model writes it)."""
    if not isinstance(request, dict):
        return {"request": _capped(str(request))}
    return {str(k)[:40]: _capped(v) if isinstance(v, str) else _capped(json.dumps(v)) if not isinstance(v, (int, bool))
            else v for k, v in list(request.items())[:8]}


def _same_type(fact, out: str) -> bool:
    """Whether an earlier read of the same call was decoded as `out` (the diagnosis's reads keep no type: judged by
    their value: a whole number, a bool, an address)."""
    if "returns" in fact.data:
        return fact.data["returns"] == out
    value = str(fact.data.get("value", ""))
    if out == "bool":
        return value in ("True", "False")
    if out == "address":
        return value.startswith("0x") and len(value) == 42
    return out == "uint256" and value.isdigit()


def _capped(value) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_SHOWN else f"{text[:MAX_SHOWN]}… ({len(text) - MAX_SHOWN} more characters)"


def _host(url: str) -> str | None:
    import httpx
    try:
        return httpx.URL(url).host or None
    except Exception:
        return None


class Sessions:
    """Chat sessions kept in memory by the API: at most `limit`, each dropped after `ttl_s` without a question."""

    def __init__(self, limit: int = 100, ttl_s: float = 3600):
        import threading
        self.limit, self.ttl_s, self._lock, self._items = limit, ttl_s, threading.Lock(), {}

    def add(self, session: ChatSession) -> None:
        import time
        with self._lock:
            self._expire(time.time())
            if len(self._items) >= self.limit:  # the least recently used goes
                oldest = min(self._items, key=lambda k: self._items[k][1])
                del self._items[oldest]
            self._items[session.id] = (session, time.time())

    def get(self, session_id: str) -> ChatSession | None:
        import time
        with self._lock:
            self._expire(time.time())
            item = self._items.get(session_id)
            if item is None:
                return None
            self._items[session_id] = (item[0], time.time())
            return item[0]

    def _expire(self, now: float) -> None:
        for key in [k for k, (_s, seen) in self._items.items() if now - seen > self.ttl_s]:
            del self._items[key]


def turn_event(session: ChatSession, turn: Turn, source: str, duration_ms: int):
    """The event-log row of one chat question (its writer outcome, check problems, usage)."""
    from anychain.events import CheckEvent, RunEvent
    checks = (CheckEvent("answer_check", "fail" if turn.answer is None else "retried", "; ".join(turn.problems)),
              ) if turn.problems else ()
    return RunEvent.from_bundle(session.bundle, source, duration_ms, checks=checks, mode=session.mode,
                                writer={"refused": "skipped"}.get(turn.outcome, turn.outcome), usage=turn.usage)
