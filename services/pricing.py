"""Price-base conversion seam.

``price_base`` normalizes every listing's price to one comparable/sortable
base currency. Currency is opaque here — the actual FX lives in a currencies
module. The default converter is identity (no conversion); a host wires
``STAPEL_LISTINGS["PRICE_BASE_CONVERTER"]`` to a callable that consults its
currencies backend (e.g. a wrapper over the ``currencies.convert`` comm
Function).
"""
from decimal import Decimal


def identity_converter(amount: Decimal, currency: str, base: str) -> Decimal:
    """Return *amount* unchanged (no cross-currency normalization).

    Signature is the seam contract: ``(amount, currency, base) -> Decimal``.
    """
    return amount
