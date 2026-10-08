"""A small Solidity source index (PHASE2 T6, D33): contracts, their bases, and functions with lines and selectors.

It reads source text, it does not compile, so it is conservative: whenever it is not sure, it gives
no answer rather than a wrong one.
  - Only external and public functions get a selector (internal and private ones are not in the ABI).
  - A parameter type gets a canonical form only when its declaration is in the indexed files: elementary
    types, contracts/interfaces (address), enums (uint8), user value types (their underlying type), structs
    (tuples) when the name is unique or qualified unambiguously. Anything else (an imported type whose file
    is not indexed, a recursive struct, a name declared twice) leaves the function without a selector.
  - Strings and comments are masked before parsing, so text inside them never becomes code.
  - Where a contract gets a function follows Solidity's C3 linearization, and a declaration without a body
    (an interface, an abstract function) is never "the definition". A contract name declared in more than
    one file is ambiguous and never matched.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from eth_utils import function_signature_to_4byte_selector

DECLARATION = re.compile(r"\b(abstract\s+contract|contract|interface|library)\s+([A-Za-z_]\w*)\s*(?:is\s+([^{]*))?\{")
FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_]\w*)\s*\(")
STRUCT = re.compile(r"\bstruct\s+([A-Za-z_]\w*)\s*\{([^}]*)\}")
ENUM = re.compile(r"\benum\s+([A-Za-z_]\w*)\s*\{")
VALUE_TYPE = re.compile(r"\btype\s+([A-Za-z_]\w*)\s+is\s+([A-Za-z_]\w*)\s*;")
ELEMENTARY = re.compile(r"^(address|bool|string|bytes([1-9]|[12]\d|3[0-2])?|u?int(8|16|24|32|40|48|56|64|72|80|88|96|"
                        r"104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?)$")
LOCATIONS = {"memory", "calldata", "storage"}
TYPE = re.compile(r"^([A-Za-z_][\w.]*)((?:\[\d*\])*)$")


@dataclass(frozen=True)
class Function:
    name: str
    signature: str | None  # canonical, e.g. "setPauser(address)"; None when not external/public or not resolvable
    start: int  # 1-based line of "function"
    end: int  # last line of its body (or of the declaration when it has none)
    text: str  # the source text without comments or whitespace, for comparing two copies
    has_body: bool

    @property
    def selector(self) -> str | None:
        return "0x" + function_signature_to_4byte_selector(self.signature).hex() if self.signature else None


@dataclass
class Contract:
    name: str
    kind: str  # contract | abstract contract | interface | library
    path: str  # file path inside the source tree
    start: int
    end: int
    bases: list[str]
    functions: list[Function] = field(default_factory=list)


@dataclass(frozen=True)
class Found:
    function: Function
    contract: Contract  # where it is defined (the contract itself or a base)


class SolidityIndex:
    def __init__(self, files: dict[str, str]):
        """`files`: path -> source text."""
        self.texts = files
        self.uncommented = {p: strip_comments(t) for p, t in files.items()}  # strings kept (literal search)
        masked = {p: mask_strings(t) for p, t in self.uncommented.items()}  # what is parsed
        self._structs: dict[str, list[tuple[str | None, str]]] = {}  # name -> [(enclosing contract, body)]
        self._enums: set[str] = set()
        self._values: dict[str, str] = {}
        self._types: set[str] = set()  # contract, interface and library names
        for text in masked.values():
            self._enums |= {m.group(1) for m in ENUM.finditer(text)}
            self._values.update({m.group(1): m.group(2) for m in VALUE_TYPE.finditer(text)})
            self._types |= {m.group(2) for m in DECLARATION.finditer(text)}
            for m in STRUCT.finditer(text):
                self._structs.setdefault(m.group(1), []).append((_enclosing(text, m.start()), m.group(2)))
        self._all: dict[str, list[Contract]] = {}
        for path, text in masked.items():
            self._read_contracts(path, text, self.uncommented[path])
        self.ambiguous = {name for name, found in self._all.items() if len(found) > 1}
        self.contracts = {name: found[0] for name, found in self._all.items() if len(found) == 1}

    @classmethod
    def from_dir(cls, root: Path, globs: list[str]) -> "SolidityIndex":
        files = {}
        for pattern in globs:
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    files[str(p.relative_to(root))] = p.read_text(errors="replace")
        return cls(files)

    def _read_contracts(self, path: str, text: str, uncommented: str) -> None:
        for m in DECLARATION.finditer(text):
            open_at = m.end() - 1
            close_at = _matching(text, open_at, "{", "}")
            if close_at is None:
                continue
            bases = [re.split(r"[\s(]", b.strip())[0] for b in _split_top(m.group(3) or "") if b.strip()]
            kind = " ".join(m.group(1).split())
            contract = Contract(m.group(2), kind, path, _line(text, m.start()), _line(text, close_at), bases)
            for f in FUNCTION.finditer(text, open_at + 1, close_at):
                contract.functions.append(self._function(text, uncommented, f, kind))
            self._all.setdefault(contract.name, []).append(contract)

    def _function(self, text: str, uncommented: str, m: re.Match, kind: str) -> Function:
        params_open = m.end() - 1
        params_close = _matching(text, params_open, "(", ")") or params_open
        i, depth = params_close + 1, 0
        while i < len(text):  # the body "{" or the ";" after modifiers like onlyRole(X)
            c = text[i]
            depth += (c == "(") - (c == ")")
            if depth == 0 and c in "{;":
                break
            i += 1
        header = text[params_close + 1:i]
        has_body = i < len(text) and text[i] == "{"
        end = (_matching(text, i, "{", "}") if has_body else i) or i
        visible = kind == "interface" or bool(re.search(r"\b(external|public)\b", header))
        types = [self._canonical(p) for p in _split_top(text[params_open + 1:params_close]) if p.strip()]
        signature = f"{m.group(1)}({','.join(types)})" if visible and None not in types else None
        body = re.sub(r"\s+", "", uncommented[m.start():end + 1])
        return Function(m.group(1), signature, _line(text, m.start()), _line(text, end), body, has_body)

    def _canonical(self, param: str, seen: frozenset = frozenset()) -> str | None:
        param = re.sub(r"\baddress\s+payable\b", "address", param)
        words = [w for w in param.split() if w not in LOCATIONS]
        if not words:
            return None
        raw = words[0] + (words[1] if len(words) > 1 and words[1].startswith("[") else "")
        m = TYPE.match(raw)
        if not m:
            return None
        base, arrays = m.groups()
        parts = base.split(".")
        name = parts[-1]
        if len(parts) == 1 and ELEMENTARY.match(name):
            return {"uint": "uint256", "int": "int256"}.get(name, name) + arrays
        if name in self._values:
            inner = self._canonical(self._values[name], seen)
            return inner + arrays if inner else None
        if name in self._enums:
            return "uint8" + arrays
        if name in self._structs:
            if name in seen:
                return None  # recursive struct: not resolvable here
            candidates = self._structs[name]
            if len(parts) > 1:
                candidates = [c for c in candidates if c[0] == parts[-2]]
            if len(candidates) != 1:
                return None  # ambiguous or unknown qualifier
            members = [self._canonical(s.strip(), seen | {name}) for s in candidates[0][1].split(";") if s.strip()]
            return None if None in members else f"({','.join(members)})" + arrays
        if len(parts) == 1 and name in self._types:
            return "address" + arrays  # contract and interface types are addresses in the ABI
        return None

    # ---- lookups ------------------------------------------------------------------------------

    def linearization(self, contract: str) -> list[str] | None:
        """Solidity's C3 order (most derived first); bases outside the index are leaves. None if inconsistent."""
        def lin(name: str, path: frozenset) -> list[str] | None:
            if name in path:
                return None
            c = self.contracts.get(name)
            if c is None:
                return None if name in self.ambiguous else [name]
            parts = [lin(b, path | {name}) for b in reversed(c.bases)]
            if any(p is None for p in parts):
                return None
            merged = _merge([list(p) for p in parts] + [list(reversed(c.bases))])
            return None if merged is None else [name] + merged
        return lin(contract, frozenset())

    def find(self, contract: str, selector: str) -> Found | None:
        """The implemented function `contract` gets for `selector`, following the C3 order."""
        order = self.linearization(contract)
        for name in order or []:
            c = self.contracts.get(name)
            for f in c.functions if c else []:
                if f.selector == selector and f.has_body:
                    return Found(f, c)
        return None

    def external_bases(self, contract: str) -> list[str]:
        """Bases in the inheritance chain whose source is not in this index (e.g. an imported library)."""
        return sorted(n for n in (self.linearization(contract) or []) if n not in self.contracts and n != contract)

    def literal(self, text: str) -> list[tuple[str, int]]:
        """(path, line) of every string literal exactly equal to `text`, outside comments."""
        quoted = (f'"{text}"', f"'{text}'")
        return [(path, n) for path, source in self.uncommented.items()
                for n, line in enumerate(source.splitlines(), 1) if any(q in line for q in quoted)]


def _merge(seqs: list[list[str]]) -> list[str] | None:
    out = []
    seqs = [s for s in seqs if s]
    while seqs:
        for s in seqs:
            head = s[0]
            if not any(head in other[1:] for other in seqs):
                break
        else:
            return None
        out.append(head)
        seqs = [[x for x in s if x != head] for s in seqs]
        seqs = [s for s in seqs if s]
    return out


def strip_comments(text: str) -> str:
    """Comments replaced by spaces (line numbers and offsets kept); string literals left alone."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            i = _string_end(text, i) + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            out[i:j] = " " * (j - i)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]
            i = j
        else:
            i += 1
    return "".join(out)


def mask_strings(text: str) -> str:
    """String contents replaced by spaces (quotes and offsets kept): code is parsed, never text."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        if text[i] in "\"'":
            j = _string_end(text, i)
            out[i + 1:j] = [ch if ch == "\n" else " " for ch in text[i + 1:j]]
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _string_end(text: str, start: int) -> int:
    quote, j = text[start], start + 1
    while j < len(text) and text[j] != quote:
        j += 2 if text[j] == "\\" else 1
    return min(j, len(text) - 1)


def _enclosing(text: str, offset: int) -> str | None:
    """Name of the contract or library whose body contains `offset` (None at file level)."""
    for m in DECLARATION.finditer(text, 0, offset):
        close_at = _matching(text, m.end() - 1, "{", "}")
        if close_at is not None and close_at > offset:
            inner = _enclosing(text[m.end():close_at], offset - m.end())
            return inner or m.group(2)
    return None


def _matching(text: str, at: int, open_c: str, close_c: str) -> int | None:
    depth = 0
    for i in range(at, len(text)):
        if text[i] == open_c:
            depth += 1
        elif text[i] == close_c:
            depth -= 1
            if depth == 0:
                return i
    return None


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for c in text:
        depth += (c in "([") - (c in ")]")
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
