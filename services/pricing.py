"""Price-base conversion seam.

``price_base`` normalizes every listing's price to one comparable/sortable
base currency. Currency is opaque here — the actual FX lives in a currencies
module. The default, :func:`currencies_converter`, asks the
``currencies.convert`` comm Function (stapel-currencies, in this process or
routed); a price already in the base currency needs no conversion. Without a
currencies provider a cross-currency price gets ``price_base = NULL`` rather
than the raw amount, which compared 100 EUR with 100 RUB as equal.
"""
from decimal import Decimal


def currencies_converter(amount: Decimal, currency: str, base: str) -> Decimal:
    """*amount* in *currency* expressed in *base*, via ``currencies.convert``.

    Raises when no provider answers; ``Listing.compute_price_base`` stores
    NULL then.
    """
    if not currency or currency.upper() == (base or "").upper():
        return amount
    from stapel_core.comm import call

    result = call(
        "currencies.convert",
        {"amount": str(amount), "from_currency": currency, "to_currency": base},
    )
    return Decimal(str(result["amount"]))


def identity_converter(amount: Decimal, currency: str, base: str) -> Decimal:
    """Return *amount* unchanged (no cross-currency normalization).

    Signature is the seam contract: ``(amount, currency, base) -> Decimal``.
    """
    return amount
