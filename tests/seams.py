"""Test doubles for dotted-path seams."""
from decimal import Decimal


def halving_converter(amount: Decimal, currency: str, base: str) -> Decimal:
    """A price_base converter that halves the amount (proves the seam is used)."""
    return Decimal(amount) / 2
