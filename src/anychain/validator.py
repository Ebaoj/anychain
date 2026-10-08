"""Checks a written answer against the evidence before it is shown (PHASE2 R7, D28).

The check runs against exactly what the model was given (`writer.evidence_payload`). It finds
what the model may have added:
  - a citation to evidence that does not exist, in any form ([E3], [E3, E9], E9, [e9], [E 9]);
  - a hex value (address, hash, selector, slice of calldata) not in the evidence, in full or
    abbreviated as prefix...suffix (an abbreviation must fit one hex value of the evidence and
    show at least 4 hex digits); a short slice (8 to 16 hex digits, not zero-padded) may come from
    inside a longer value, as decoded calldata does;
  - a link (with or without scheme) that is not one of the explorer pages the model was given;
  - a number that is not a number of the evidence: any number with 4 or more digits, any number with
    decimals, any number with a scale word. Accepted: rounded or cut to the precision written,
    thousands separators (, . space), the Portuguese format, scale words with 2 or more significant
    digits ("5,65 milhões"), a dropped minus sign. Not accepted: an added minus sign.
Integers under 4 digits without decimals (counts like "30 of 93") are not checked: too common to
judge. That is a known limit (D28).
"""
import re
from decimal import Decimal, InvalidOperation, localcontext

from anychain.models import EvidenceBundle

SPACES = "   "  # space, no-break space, narrow no-break space: thousands separators
BRACKETS = re.compile(r"\[([^\[\]]{1,80})\]")
CITATION_ID = re.compile(r"(?i)\bE\s?(\d+)\b")
BARE_CITATION = re.compile(r"\bE(\d+)\b")
URL = re.compile(r"(?:https?://|www\.)[^\s)\]>`'\"<]+", re.IGNORECASE)
URL_TRAILING = ".,;:!?*_)>'\"/"
HEX = re.compile(r"0x([0-9a-fA-F]+)(?:(?:…|\.\.\.)(?:([0-9a-fA-F]{4,})(?![0-9a-zA-Z]))?)?")
DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
SCALE_WORDS = {"mil": 3, "thousand": 3, "k": 3, "milhão": 6, "milhões": 6, "million": 6, "mi": 6, "m": 6,
               "bilhão": 9, "bilhões": 9, "billion": 9, "bi": 9, "b": 9, "trilhão": 12, "trilhões": 12,
               "trillion": 12, "tri": 12}
NUMBER = re.compile(
    r"(?:(?<![\w-])([-−]))?(?<![\d.,])(\d{1,3}(?:[" + SPACES + r"]\d{3})+(?:,\d+)?|\d[\d.,]*\d|\d)"
    r"(?:[" + SPACES + r"]?(" + "|".join(sorted(SCALE_WORDS, key=len, reverse=True)) + r")\b)?",
    re.IGNORECASE)
MIN_DIGITS = 4
SLICE_MIN, SLICE_MAX = 8, 16  # hex digits a value read from inside calldata may have
MIN_ABBREVIATION = 4


def check_answer(answer: str, evidence: str, allowed_urls: set[str], ids: set[str]) -> list[str]:
    """Problems found in `answer` (empty: nothing the evidence does not support).

    `evidence` is the text the model was given; `ids` the evidence ids; `allowed_urls` its links.
    """
    with localcontext() as ctx:
        ctx.prec = 200  # token amounts and pool prices exceed Decimal's default 28 digits
        return _check(answer, evidence, allowed_urls, ids)


def _check(answer: str, evidence: str, allowed_urls: set[str], ids: set[str]) -> list[str]:
    problems: list[str] = []
    lower = evidence.lower()

    cited, rest = _citations(answer)
    for n in sorted(cited):
        if f"E{n}" not in ids:
            problems.append(f"E{n} is cited but no such evidence exists")

    allowed = {_bare_url(u) for u in allowed_urls}
    for url in URL.findall(rest):
        if _bare_url(url) not in allowed:
            problems.append(f"link {url.rstrip(URL_TRAILING)[:120]} is not one of the sources")
    rest = URL.sub(" ", rest)

    hex_values = [h.lower() for h in re.findall(r"0x[0-9a-fA-F]+", evidence)]
    for m in HEX.finditer(rest):
        if not _hex_ok(m, lower, hex_values):
            problems.append(f"{m.group(0)[:80]} does not appear in the evidence")
    rest = HEX.sub(" ", rest)

    for m in DATE.finditer(rest):  # a date: its day, month and year must be the evidence's
        day, month, year = (int(g) for g in m.groups())
        iso = re.compile(rf"{year}-{month:02d}-{day:02d}")
        if not iso.search(evidence):
            problems.append(f"the date {m.group(0)} is not in the evidence")
    rest = DATE.sub(" ", rest)

    known = _numbers(evidence)
    for m in NUMBER.finditer(rest):
        sign, token, scale = m.group(1), m.group(2), (m.group(3) or "").lower()
        digits = sum(c.isdigit() for c in token)
        readings = _readings(token, scale)
        has_decimals = bool(readings) and all(unit < 1 for _v, unit in readings) and not scale
        if digits < MIN_DIGITS and not scale and not has_decimals:
            continue
        if scale and len(re.sub(r"\D", "", token).strip("0")) < 2:
            problems.append(f"the number {m.group(0).strip()} is too rough to check: quote the evidence value")
            continue
        if not any(_matches(value, unit, bool(sign), known) for value, unit in readings):
            problems.append(f"the number {m.group(0).strip()} is not in the evidence")
    return problems


def _citations(answer: str) -> tuple[set[int], str]:
    """Every evidence id cited, in any bracket form or bare, and the answer without them."""
    cited: set[int] = set()

    def strip_group(m: re.Match) -> str:
        found = CITATION_ID.findall(m.group(1))
        if not found:
            return m.group(0)  # a bracket with something else in it (e.g. a markdown link text)
        cited.update(int(i) for i in found)
        return " "
    rest = BRACKETS.sub(strip_group, answer)
    cited.update(int(i) for i in BARE_CITATION.findall(rest))
    return cited, BARE_CITATION.sub(" ", rest)


def _bare_url(url: str) -> str:
    url = url.split("#", 1)[0].rstrip(URL_TRAILING)
    return re.sub(r"^(https?://)?(www\.)?", "", url, flags=re.IGNORECASE).lower()


def _hex_ok(m: re.Match, lower: str, hex_values: list[str]) -> bool:
    head, tail = m.group(1).lower(), (m.group(2) or "").lower()
    if m.group(0) != "0x" + m.group(1):  # abbreviated
        if len(head) < MIN_ABBREVIATION:
            return False
        if tail:
            return any(h[2:].startswith(head) and h.endswith(tail) for h in hex_values)
        return ("0x" + head) in lower
    if len(head) in (40, 64):  # an address or a hash must be a whole evidence value
        return any(h[2:] == head for h in hex_values)
    if any(h[2:].startswith(head) for h in hex_values):
        return True
    return (SLICE_MIN <= len(head) <= SLICE_MAX and not head.startswith("0000")
            and any(head in h for h in hex_values))


def _numbers(text: str) -> list[Decimal]:
    """Numbers of the evidence, which is written in English format (-1234.56, no separators)."""
    found = []
    for raw in re.findall(r"(?<![0-9a-zA-Z.])-?\d+(?:\.\d+)?", HEX.sub(" ", text)):
        try:
            found.append(Decimal(raw))
        except InvalidOperation:
            continue
    return found


def _readings(token: str, scale: str) -> list[tuple[Decimal, Decimal]]:
    """Possible (value, precision unit) of a number as written, in English or Portuguese format."""
    shift = Decimal(10) ** SCALE_WORDS.get(scale, 0)
    token = re.sub(f"[{SPACES}]", "", token)
    readings = []
    for thousands, decimal in ((",", "."), (".", ",")):
        if not _well_formed(token, thousands, decimal):
            continue
        plain = token.replace(thousands, "").replace(decimal, ".")
        try:
            value = Decimal(plain)
        except InvalidOperation:
            continue
        places = len(plain.split(".")[1]) if "." in plain else 0
        readings.append((value * shift, (Decimal(10) ** -places) * shift))
    return readings


def _well_formed(token: str, thousands: str, decimal: str) -> bool:
    if token.count(decimal) > 1:
        return False
    whole = token.split(decimal)[0]
    groups = whole.split(thousands)
    return len(groups) == 1 or (1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]))


def _matches(value: Decimal, unit: Decimal, negative: bool, known: list[Decimal]) -> bool:
    """An evidence number that, rounded or cut to the precision written, gives `value`. A minus sign
    written must be in the evidence; one left out is fine (prose says "sent" instead of "-")."""
    for e in known:
        if negative and e >= 0:
            continue
        target = abs(e)
        if abs(target - value) <= unit / 2 or Decimal(0) <= target - value < unit:
            return True
    return False


def evidence_ids(bundle: EvidenceBundle) -> set[str]:
    return {e.id for e in bundle.items}


def allowed_urls(bundle: EvidenceBundle) -> set[str]:
    """The links the model was given (explorer pages only: writer._sources_for_llm)."""
    return {s.url for e in bundle.items for s in e.sources if s.kind == "explorer_ui" and s.url}
