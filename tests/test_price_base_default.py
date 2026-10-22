"""``price_base`` is converted by default, and never a wrong number.

The default converter was identity, so a marketplace with EUR and RUB listings
stored ``price_base == price`` and sorted 100 EUR next to 100 RUB. The default
asks ``currencies.convert``; without a provider a cross-currency price gets
NULL (unknown) instead of the raw amount.
"""
from decimal import Decimal

import pytest

from stapel_listings.models import Listing

pytestmark = pytest.mark.django_db


@pytest.fixture
def eur_is_two_usd():
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    def provider(payload):
        assert payload["to_currency"] == "USD"
        rate = {"EUR": Decimal("2")}[payload["from_currency"]]
        return {"amount": str(Decimal(payload["amount"]) * rate)}

    register_function("currencies.convert", provider)
    yield
    function_registry._providers.pop("currencies.convert", None)
    function_registry._schemas.pop("currencies.convert", None)


def _listing(user, currency, price):
    listing = Listing(owner=user, category_id="7", currency=currency, price=Decimal(price))
    return listing.compute_price_base()


def test_a_foreign_price_is_converted_through_currencies(user, eur_is_two_usd):
    assert _listing(user, "EUR", "100") == Decimal("200")


def test_a_base_currency_price_needs_no_provider(user):
    assert _listing(user, "USD", "100") == Decimal("100")


def test_without_a_provider_a_foreign_price_is_unknown_not_raw(user):
    assert _listing(user, "EUR", "100") is None
