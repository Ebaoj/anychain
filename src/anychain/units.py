"""A token amount in the token's own units (PHASE3, D49): raw / 10**decimals, exact (Decimal), never rounded.

The decimals and the symbol come from the token contract itself (decimals() and symbol(), read on the node at the
same block), so the converted amount is data, with a source, not arithmetic the model did.
"""
from decimal import Decimal, localcontext

import unicodedata

# ERC-20 functions whose result is an amount of the contract's own token, so its decimals apply. Full signatures:
# ERC-1155 balanceOf(address,uint256) counts one id, Permit2 allowance(address,address,address) is another contract's.
AMOUNT_SIGNATURES = ("balanceOf(address)", "allowance(address,address)", "totalSupply()")
SYMBOL_CLAIM = "the name the contract gives itself, not a proof of which token it is"


def is_amount(signature: str) -> bool:
    return signature.replace(" ", "") in AMOUNT_SIGNATURES


def plain_symbol(symbol: str | None) -> bool:
    """A symbol fit to write after an amount: 1 to 12 characters, no spaces, no digits (they would read as part of
    the amount), nothing invisible or direction-changing."""
    return (isinstance(symbol, str) and 0 < len(symbol) <= 12 and symbol.isprintable()
            and not any(c.isspace() or c.isdigit() or unicodedata.category(c) == "Cf" for c in symbol))


def in_units(raw: int, decimals: int) -> str:
    """33540 with 6 decimals -> "0.03354"; exact, no exponent, no trailing zeros."""
    with localcontext() as ctx:
        ctx.prec = 100
        text = format(Decimal(raw).scaleb(-decimals), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
