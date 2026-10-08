"""Security notes: known risky patterns in the called function's code (PHASE2_5 T2, R2, D39).

Each note is a pattern match on the source, with its line: a heuristic, never an audit and never a
finding that the contract is vulnerable. A pattern not found says nothing about safety, so no note
is ever written for an absence. Patterns (from the original plan, section 6.4):

  - tx.origin compared with a state variable or with something named as an authority (owner, admin,
    auth, operator, governor, manager): authorization by tx.origin. Comparing it with msg.sender (a
    check for contract callers) or with a fixed address (gas estimation) is not authorization;
  - delegatecall (to the contract's own code: said as such);
  - a low-level call, or an Ethereum send (one argument, no options, on an address payable), whose result
    is not used (a library or contract function named call or send is not low-level);
  - a call to another contract followed, on a path that can run after it, by a write to a state
    variable, without a reentrancy guard (a modifier named as one, a lock variable, or a state
    variable written both before and after the call, the set-then-reset pattern of a hand-written
    lock). Ether sent with transfer or send (2,300 gas) is not counted;
  - a state-changing external or public function with a name that usually needs restricted access,
    with no modifier, no check of the caller and no call to another function of its contract (which
    may hold the check) in its shown code;
  - selfdestruct, in the function or anywhere in the contracts of its chain (itself included);
  - a loop bounded only by the length of a storage array.

The scan reads the function's own code only (the helpers it calls are not followed), with comments
removed and string contents masked, statement by statement (a statement may span lines). Names
declared as parameters or locals of the function are never taken for state variables.
"""
import re
from dataclasses import dataclass, field

from anychain.solidity import Found, SolidityIndex, mask_strings

TX_ORIGIN = re.compile(r"tx\.origin\s*[=!]=\s*([\w.]+)|([\w.]+)\s*[=!]=\s*tx\.origin")
AUTH_NAME = re.compile(r"owner|admin|auth|operator|governor|manager", re.IGNORECASE)
DELEGATECALL = re.compile(r"\.delegatecall\s*\(")
SELF_DELEGATECALL = re.compile(r"address\s*\(\s*this\s*\)\s*\.delegatecall\s*\(")
LOW_LEVEL = re.compile(r"(\b[A-Za-z_]\w*|\))\s*\.\s*(call|send)\s*(\{[^}]*\})?\s*\(")
CALL_TO = re.compile(r"(\b[A-Za-z_]\w*|\))\s*\.\s*(call|send|transfer|transferFrom|safeTransfer|safeTransferFrom)"
                     r"\s*(\{[^}]*\})?\s*\(")
SELFDESTRUCT = re.compile(r"\b(selfdestruct|suicide)\s*\(")
LOOP_LENGTH = re.compile(r"^for\s*\([^;]*;([^;]*);")
LENGTH_OF = re.compile(r"\b([A-Za-z_]\w*)(?:\s*\[[^\]]*\])*\s*\.length\b")
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?:\.\w+\s*)*(?:[-+*/%|&^]?=(?!=)|\+\+|--)")
DELETE = re.compile(r"^\s*delete\s+([A-Za-z_]\w*)")
INCREMENT = re.compile(r"^\s*(?:\+\+|--)\s*([A-Za-z_]\w*)")
SENSITIVE = re.compile(r"^(set|withdraw|mint|upgrade|initialize|pause|unpause|kill|destroy|sweep|rescue|grant|revoke"
                       r"|transferOwnership|changeOwner|renounce|emergency|migrate|recover|configure|update)(?=[A-Z0-9_]|$)")
# what counts as a check in the body: the caller read directly (also in assembly), a call whose name says it
# checks permission, or an initialization guard (a one-time function guarded by its own flag)
CALLER_CHECK = re.compile(r"msg\.sender|_msgSender\s*\(|\bcaller\s*\(\s*\)|initiali[sz]"
                          r"|\b\w*(auth|owner|admin|role|only|enforce|permission|access|guard|govern|check)\w*\s*\(",
                          re.IGNORECASE)
HEADER_WORDS = {"external", "public", "internal", "private", "view", "pure", "payable", "virtual", "override",
                "returns", "memory", "calldata", "storage"}
STATE_DECLARATION = re.compile(
    r"^\s*(?:mapping\s*\(.*\)|[A-Za-z_][\w.]*(?:\s*\[[^\]]*\])*)\s+((?:(?:public|private|internal|override|transient)\s+)*)"
    r"([A-Za-z_]\w*)\s*(?:=[^;]*)?;")
NOT_DECLARATIONS = re.compile(r"^\s*(return|emit|using|event|error|function|modifier|struct|enum|require|revert|import"
                              r"|pragma|delete|if|for|while|else|constructor|fallback|receive)\b")
LOCAL_DECLARATION = re.compile(r"(?:^|[(,])\s*([A-Za-z_][\w.]*)(?:\s*\[[^\]]*\])*\s+(?:(?:memory|storage|calldata|payable)\s+)?"
                               r"([A-Za-z_]\w*)\s*(?=[=,);]|$)")
NOT_TYPES = {"return", "emit", "delete", "else", "revert", "new", "if", "while", "for", "require", "assert", "unchecked",
             "assembly", "do", "try", "catch", "returns", "memory", "storage", "calldata", "payable"}
GUARDS = re.compile(r"reentr|\block\w*\b|\bmutex\b", re.IGNORECASE)  # in the header: a guard modifier by its name
BODY_GUARD = re.compile(r"\b\w*(unlocked|locked|entered|reentran)\w*\b|\b_status\b", re.IGNORECASE)  # a hand-written lock
BLOCK_OPENERS = ("else", "do", "unchecked", "assembly", "try", "catch")


@dataclass(frozen=True)
class Note:
    pattern: str  # short key, e.g. "delegatecall"
    line: int
    text: str


@dataclass
class Statement:
    text: str  # the statement's code, lines joined with spaces
    lines: list[int]  # the source line of each character of `text`
    blocks: tuple  # ids of the blocks it is inside, outermost first
    kinds: dict = field(default_factory=dict)  # block id -> "else" | "loop" | "plain"

    def line_at(self, offset: int) -> int:
        return self.lines[min(offset, len(self.lines) - 1)]

    @property
    def first_line(self) -> int:
        """The line of its first character that is not a space (the text starts with the previous line's end)."""
        return self.line_at(len(self.text) - len(self.text.lstrip()))


def scan(index: SolidityIndex, found: Found, contract: str) -> list[Note]:
    """Notes for the function `found`, run by `contract` (whose inheritance chain is checked for selfdestruct
    and gives the state variables)."""
    f, c = found.function, found.contract
    file_text = mask_strings(index.uncommented.get(c.path, ""))
    code = file_text.split("\n")
    numbered = list(zip(range(f.start, f.end + 1), code[f.start - 1:f.end]))
    header, statements = _parse(numbered)
    after_params = _after_params(header, f.name)
    declared = _parameters(header, f.name) | {m.group(2) for s in statements for m in LOCAL_DECLARATION.finditer(s.text)
                                               if m.group(1) not in NOT_TYPES}
    state = _state_variables(index, contract) - declared
    internal = {n for n in index.contracts if index.contracts[n].kind != "library"} | {"super"}
    notes: list[Note] = []

    for s in statements:
        m = TX_ORIGIN.search(s.text)
        other = (m.group(1) or m.group(2)).split(".")[0] if m else None
        if other and other != "msg" and (other in state or AUTH_NAME.search(other)):
            n = s.line_at(m.start())
            notes.append(Note("tx_origin", n, f"line {n} compares tx.origin with {other}: when tx.origin decides "
                                              f"who may act, any contract the account owner is tricked into "
                                              f"calling passes the check"))
        m = DELEGATECALL.search(s.text)
        if m:
            n = s.line_at(m.start())
            if SELF_DELEGATECALL.search(s.text):
                in_loop = any(s.kinds.get(b) == "loop" for b in s.blocks)
                payable = re.search(r"\bpayable\b", after_params)
                text = (f"line {n} uses delegatecall to this contract's own code"
                        + (" in a loop (the multicall pattern): every item sees the same msg.value" if in_loop else
                           ": the code runs with the same msg.value and storage")
                        + (", and this function is payable: the setting of msg.value reuse bugs, where several "
                           "items each count the same payment" if in_loop and payable else ""))
            else:
                text = (f"line {n} uses delegatecall: the called code runs with this contract's storage and "
                        f"balance, so its target must be trusted")
            notes.append(Note("delegatecall", n, text))
        m = SELFDESTRUCT.search(s.text)
        if m:
            n = s.line_at(m.start())
            notes.append(Note("selfdestruct", n, f"line {n} calls selfdestruct"))
        for m in LOW_LEVEL.finditer(s.text):
            receiver, method, options = m.group(1), m.group(2), m.group(3)
            if receiver in index.contracts:
                continue  # a library or contract function named call or send (e.g. SafeCall.call)
            if method == "send" and (options or _argument_count(s.text, m.end() - 1) != 1
                                     or not _address_payable(s.text[:m.start() + len(receiver)], receiver, file_text)):
                continue  # not Ethereum's send: a contract function with that name (LayerZero, ERC777, a bridge)
            if _result_unused(s.text[:m.start() + len(receiver)]):
                n = s.line_at(m.start())
                notes.append(Note("unchecked_call", n, f"line {n} makes a low-level {method} whose result is "
                                                       f"not used: a failure there would not stop the transaction"))

    body_text = " ".join(s.text for s in statements)
    if not GUARDS.search(after_params) and not BODY_GUARD.search(body_text):
        note = _call_then_write(statements, state, internal)
        if note:
            notes.append(note)

    header_words = re.search(r"\b(external|public)\b", after_params)
    if (f.signature and SENSITIVE.match(f.name) and header_words
            and not re.search(r"\b(view|pure)\b", after_params) and not _modifiers(after_params)
            and not CALLER_CHECK.search(body_text) and not _calls_own_function(statements, index, contract)):
        notes.append(Note("no_access_check", f.start, f"{f.name} (line {f.start}) is {header_words.group(1)}, "
                                                      f"is not marked view or pure, has a name that usually needs restricted "
                                                      f"access, and its shown code has no modifier and no check of "
                                                      f"the caller (a helper it calls may still check)"))
    for s in statements:
        m = LOOP_LENGTH.match(s.text.strip())
        if not m or re.search(r"&&|\|\|", m.group(1)):
            continue  # not a for loop, or a condition with another bound
        arrays = [a for a in LENGTH_OF.findall(m.group(1)) if a in state]
        if arrays:
            n = s.first_line
            notes.append(Note("unbounded_loop", n, f"line {n} loops over the storage array {arrays[0]}: the cost "
                                                   f"grows with its length and can exceed the gas limit"))
    if not any(note.pattern == "selfdestruct" for note in notes):
        for name in index.linearization(contract) or []:
            other = index.contracts.get(name)
            if not other:
                continue
            source = mask_strings(index.uncommented.get(other.path, "")).split("\n")
            for n in range(other.start, other.end + 1):
                if SELFDESTRUCT.search(source[n - 1]):
                    notes.append(Note("selfdestruct", n, f"{other.name} ({other.path}) calls selfdestruct at line {n}"))
                    break
    return notes


def _call_then_write(statements: list[Statement], state: set[str], internal: set[str]) -> Note | None:
    """The first call to another contract followed, on a path that can run after it, by a state write."""
    calls = [(i, s.line_at(m.start())) for i, s in enumerate(statements) for m in CALL_TO.finditer(s.text)
             if _external(s.text, m, internal)]
    if not calls:
        return None
    first, call_line = calls[0]
    writes = [(i, w) for i, s in enumerate(statements) for w in [_written_state(s.text, state)] if w]
    before = {w for i, w in writes if i < first}
    after = [(i, w) for i, w in writes if i > first and not _exclusive(statements[first], statements[i])]
    if not after or before & {w for _i, w in after}:
        return None  # nothing written after, or a variable set before and again after: a hand-written lock (D39)
    i, written = after[0]
    n = statements[i].first_line
    return Note("call_before_write", n, f"line {call_line} calls another contract and line {n} can then write the "
                                        f"state variable {written}, and no reentrancy guard is visible in the "
                                        f"function: the order reentrancy attacks rely on")


def _external(text: str, m: re.Match, internal: set[str]) -> bool:
    """Whether a matched call goes to another contract: not super or a base contract by name, and not
    Ether sent with transfer or send (one argument, 2,300 gas)."""
    receiver, method, options = m.group(1), m.group(2), m.group(3)
    if receiver in internal:
        return False
    if method in ("transfer", "send") and not options and _argument_count(text, m.end() - 1) == 1:
        return False
    return True


def _exclusive(call: Statement, write: Statement) -> bool:
    """True when the write sits in an else (or catch) branch of a block that held the call: it never runs
    after the call."""
    closed = [b for b in call.blocks if b not in write.blocks]
    opened = [b for b in write.blocks if b not in call.blocks]
    braceless_branch = re.match(r"^(if|else)\b", call.text.strip())  # `if (x) a.f(); else { … }`
    return (bool(closed) or bool(braceless_branch)) and any(write.kinds.get(b) == "else" for b in opened)


def _parse(numbered: list[tuple[int, str]]) -> tuple[str, list[Statement]]:
    """The function's declaration (up to its body) and its body's statements, split at ";", "{" and "}"
    outside parentheses. A "{" after `.call` or `.send` (call options) does not open a block."""
    chars = [(ch, n) for n, text in numbered for ch in text + " "]
    header_end = next((i for i, (ch, _n) in enumerate(chars) if ch == "{"), len(chars))
    header = "".join(ch for ch, _n in chars[:header_end])
    statements: list[Statement] = []
    stack: list[int] = []
    kinds: dict[int, str] = {}
    text, lines, parens, options, next_id = [], [], 0, 0, 0

    def flush():
        joined = "".join(text)
        if joined.strip():
            statements.append(Statement(joined, list(lines), tuple(stack), kinds))
        text.clear()
        lines.clear()

    i = header_end
    while i < len(chars):
        ch, n = chars[i]
        prefix = "".join(text).strip()
        if ch == "(":
            parens += 1
        elif ch == ")":
            parens = max(0, parens - 1)
        if ch == "{" and parens == 0 and options == 0 and i != header_end:
            if re.search(r"(\.\s*\w+|\bnew\s+[\w.]+)\s*$", prefix):
                options += 1  # call options, part of the statement
            else:
                flush_kind = ("else" if re.match(r"^(else|catch)\b", prefix) else
                              "loop" if re.match(r"^(for|while|do)\b", prefix) else "plain")
                flush()
                next_id += 1
                stack.append(next_id)
                kinds[next_id] = flush_kind
                i += 1
                continue
        elif ch == "{" and i == header_end:
            stack.append(0)
            kinds[0] = "plain"
            i += 1
            continue
        elif ch == "}" and options:
            options -= 1
        elif ch == "}" and parens == 0:
            flush()
            if stack:
                stack.pop()
            i += 1
            continue
        if ch == ";" and parens == 0:
            flush()
            i += 1
            continue
        text.append(" " if ch == "\n" else ch)
        lines.append(n)
        i += 1
    flush()
    return header, statements


def _argument_count(text: str, open_paren: int) -> int:
    """Arguments of the call whose "(" is at `open_paren` (top-level commas + 1; 0 for an empty list)."""
    depth, count, empty = 0, 1, True
    for ch in text[open_paren:]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            count += 1
        elif not ch.isspace() and depth >= 1:
            empty = False
    return 0 if empty else count


def _address_payable(before: str, receiver: str, file_text: str) -> bool:
    """Whether the receiver of a send is an address payable: `payable(x).send` or a name declared so."""
    if receiver == ")":
        return bool(re.search(r"\bpayable\s*\(.*\)\s*$", before))
    return bool(re.search(rf"\baddress\s+payable\s+(?:\w+\s+)*{re.escape(receiver)}\b", file_text))


def _result_unused(before: str) -> bool:
    """True when nothing before the call in its statement takes its result (an argument of another call
    takes it too)."""
    if before.count("(") > before.count(")"):
        return False
    return not re.search(r"(?<![=!<>])=(?!=)|\breturn\b|\brequire\s*\(|\bassert\s*\(|\bif\s*\(|\bwhile\s*\(|\(\s*bool"
                         r"|[,!?:]|&&|\|\|", before)


def _written_state(text: str, state: set[str]) -> str | None:
    for m in (ASSIGNMENT.match(text), DELETE.match(text), INCREMENT.match(text)):
        if m and m.group(1) in state:
            return m.group(1)
    return None


def _calls_own_function(statements: list[Statement], index: SolidityIndex, contract: str) -> bool:
    """True when the body calls a function of the contract's own chain: its code is not read here, and it
    may hold the check."""
    names = {f.name for n in index.linearization(contract) or [] for f in getattr(index.contracts.get(n), "functions", [])}
    called = {m.group(1) for s in statements for m in re.finditer(r"(?<![.\w])([A-Za-z_]\w*)\s*\(", s.text)}
    return bool(names & called)


def _after_params(header: str, name: str) -> str:
    """The declaration after the parameter list, without the returns list: visibility, mutability, modifiers."""
    after = header.split(name, 1)[-1]
    depth, end = 0, len(after)
    for i, ch in enumerate(after):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    return re.sub(r"\breturns\s*\(.*", "", after[end + 1:], flags=re.S)


def _parameters(header: str, name: str) -> set[str]:
    after = header.split(name, 1)[-1]
    start = after.find("(")
    if start < 0:
        return set()
    depth, end = 0, len(after)
    for i, ch in enumerate(after[start:], start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    params = re.split(r",(?![^()]*\))", after[start + 1:end])
    return {p.split()[-1] for p in params if len(p.split()) >= 2}


def _modifiers(after_params: str) -> list[str]:
    """Identifiers after the parameters that are not keywords: the function's modifiers."""
    without_override = re.sub(r"\boverride\s*\([^)]*\)", " ", after_params)
    return [w for w in re.findall(r"[A-Za-z_]\w*", re.sub(r"\([^)]*\)", " ", without_override)) if w not in HEADER_WORDS]


def _state_variables(index: SolidityIndex, contract: str) -> set[str]:
    """Names of the mutable state variables declared in `contract` and the contracts it inherits."""
    names: set[str] = set()
    for name in index.linearization(contract) or []:
        c = index.contracts.get(name)
        if not c or c.kind in ("interface", "library"):
            continue
        source = mask_strings(index.uncommented.get(c.path, "")).split("\n")
        inside = {n for f in c.functions for n in range(f.start, f.end + 1)}
        depth = 0
        for n in range(c.start, c.end + 1):
            text = source[n - 1]
            if depth == 1 and n not in inside and not NOT_DECLARATIONS.match(text):
                m = STATE_DECLARATION.match(text)
                if m and not re.search(r"\b(constant|immutable)\b", text):
                    names.add(m.group(2))
            depth += text.count("{") - text.count("}")
    return names
