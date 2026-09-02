"""Where a listing IS: required to publish, and shown as a place.

Two live defects on the darom/ruberi stand, one root each.

Д71 — «Где находится» was optional, so a listing published with no
coordinates at all. It then sat outside every radius filter and every map,
visible only to someone who scrolled past it, and nothing anywhere said why.

Д76 — every card read «ул. Тверская, 7, Москва, Россия». The label was
client-supplied: the composer's picker posted the geocoder's `formatted`
line — house number first, because that is what a picker's confirmation
line is FOR — straight into `location_label_draft`, and the card printed
it. A buyer scanning a grid wants the place, and a seller's doorway is not
one.
"""
import pytest

from stapel_listings.models import ListingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def geo_provider():
    """Stand in for ``geo.reverse_geocode``, recording what was asked."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    calls: list[dict] = []
    answer: list[dict] = [
        {
            "lat": 55.7558,
            "lon": 37.6173,
            "formatted": "Тверская улица, 7, Москва, Россия",
            "address": {
                "city": "Москва",
                "district": "Тверской",
                "county": "",
                "state": "Москва",
                "country": "Россия",
                "street": "Тверская улица",
                "housenumber": "7",
            },
        }
    ]

    def _provider(payload):
        calls.append(payload)
        return answer[0]

    register_function("geo.reverse_geocode", _provider)

    class Handle:
        payloads = calls

        @staticmethod
        def answers_with(**address):
            answer[0] = {**answer[0], "address": address}

        @staticmethod
        def fails():
            def _boom(payload):
                calls.append(payload)
                raise RuntimeError("photon is down")

            function_registry._providers.pop("geo.reverse_geocode", None)
            register_function("geo.reverse_geocode", _boom)

    try:
        yield Handle
    finally:
        function_registry._providers.pop("geo.reverse_geocode", None)


@pytest.fixture
def placeless(draft_listing):
    """The ``draft_listing`` fixture with its place taken back off."""
    draft_listing.lat_draft = None
    draft_listing.lon_draft = None
    draft_listing.location_label_draft = ""
    draft_listing.save()
    return draft_listing


@pytest.fixture
def located(draft_listing):
    draft_listing.lat_draft = 55.7558
    draft_listing.lon_draft = 37.6173
    draft_listing.location_label_draft = "Тверская улица, 7, Москва, Россия"
    draft_listing.save()
    return draft_listing


# --------------------------------------------------------------------------
# Д71 — a listing without a place does not publish
# --------------------------------------------------------------------------


def test_a_draft_with_no_coordinates_is_invalid(placeless):
    from stapel_listings.services.publish import validate_draft

    result = validate_draft(placeless)
    assert result.valid is False
    slugs = [row.slug for row in result.results]
    assert "location" in slugs


def test_the_failure_is_structured_and_localizable(placeless):
    """Not a bare ValidationError: the composer renders per-field errors, and
    a flattened opaque one puts the seller in front of a form with no
    indication of which control is wrong (the image check's shape, which is
    deliberately not copied here)."""
    from stapel_listings.errors import ERR_400_LOCATION_REQUIRED
    from stapel_listings.services.publish import validate_draft

    row = next(r for r in validate_draft(placeless).results if r.slug == "location")
    assert row.localizable_error == ERR_400_LOCATION_REQUIRED


def test_publishing_without_a_place_is_refused(located, geo_provider):
    from django.core.exceptions import ValidationError
    from stapel_listings.services.publish import publish_listing

    located.lat_draft = None
    located.lon_draft = None
    located.save()
    with pytest.raises(ValidationError):
        publish_listing(located)


def test_a_label_alone_is_not_a_place(located, geo_provider):
    """The predicate is COORDINATES, not the label. The label is a string a
    client sends; the coordinates are what the radius filter, the geohash and
    the map actually need, so a listing carrying only the string would pass a
    label check and still be invisible on every geographic surface."""
    from stapel_listings.services.publish import validate_draft

    located.lat_draft = None
    located.lon_draft = None
    located.location_label_draft = "Москва"
    located.save()
    assert validate_draft(located).valid is False


def test_the_requirement_is_a_declared_switch(located, geo_provider, settings):
    settings.STAPEL_LISTINGS = {"REQUIRE_LOCATION_ON_PUBLISH": False}
    located.lat_draft = None
    located.lon_draft = None
    located.save()
    from stapel_listings.services.publish import validate_draft

    assert [r.slug for r in validate_draft(located).results if r.slug == "location"] == []


def test_a_located_draft_publishes(located, geo_provider):
    from stapel_listings.services.publish import publish_listing

    publish_listing(located)
    located.refresh_from_db()
    assert located.status in (ListingStatus.PENDING, ListingStatus.PUBLISHED)


# --------------------------------------------------------------------------
# Д76 — the card shows a place, not a doorway
# --------------------------------------------------------------------------


def test_publishing_stamps_a_place_not_a_street_address(located, geo_provider):
    from stapel_listings.services.publish import publish_listing

    publish_listing(located)
    located.refresh_from_db()
    assert located.location_label == "Москва, Тверской"
    assert geo_provider.payloads[-1]["lat"] == pytest.approx(55.7558)


def test_a_city_with_no_district_is_just_the_city(located, geo_provider):
    from stapel_listings.services.publish import publish_listing

    geo_provider.answers_with(city="Тула", district="")
    publish_listing(located)
    located.refresh_from_db()
    assert located.location_label == "Тула"


def test_a_place_with_no_city_falls_back_to_the_county(located, geo_provider):
    """Photon answers `county` for a settlement outside a city, and a card
    that printed nothing there would be worse than one that names the
    district."""
    from stapel_listings.services.publish import publish_listing

    geo_provider.answers_with(city="", county="Одинцовский район", district="")
    publish_listing(located)
    located.refresh_from_db()
    assert located.location_label == "Одинцовский район"


def test_the_client_string_is_never_trusted(located, geo_provider):
    """The seller could post anything here — it is a writable draft field.
    The published label is derived from the coordinates, so what a card shows
    is what the coordinates say, not what the client typed."""
    from stapel_listings.services.publish import publish_listing

    located.location_label_draft = "САМЫЕ ДЕШЁВЫЕ ШИНЫ ЗВОНИТЕ"
    located.save()
    publish_listing(located)
    located.refresh_from_db()
    assert located.location_label == "Москва, Тверской"


def test_a_dark_geocoder_keeps_the_draft_label(located, geo_provider):
    """Fail-soft, and on purpose. The geocoder is a network dependency and
    publication is not: a listing must not become unpublishable because
    photon is restarting. The label degrades to what the client supplied —
    the pre-0.16 behaviour — rather than to an empty card."""
    from stapel_listings.services.publish import publish_listing

    geo_provider.fails()
    publish_listing(located)
    located.refresh_from_db()
    assert located.location_label == "Тверская улица, 7, Москва, Россия"


def test_an_empty_answer_keeps_the_draft_label(located, geo_provider):
    from stapel_listings.services.publish import publish_listing

    geo_provider.answers_with(city="", county="", district="")
    publish_listing(located)
    located.refresh_from_db()
    assert located.location_label == "Тверская улица, 7, Москва, Россия"


def test_the_search_document_carries_the_derived_label(located, geo_provider):
    from stapel_listings.services.publish import publish_listing
    from stapel_listings.services.search_feed import build_search_document

    publish_listing(located)
    located.refresh_from_db()
    assert build_search_document(located)["location_label"] == "Москва, Тверской"
