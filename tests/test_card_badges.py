"""The card badge contract — «Кирпичный · 3 · 9» must never happen again.

A live apartment card summarised itself as three bare values with no unit and
no label. These tests pin the four presentations the server now decides, on an
apartment-like schema built the way a real category builds one: the DAOs come
out of ``build_projections`` against feature configs, not hand-written, so a
unit that stops travelling from the config to the DAO fails here.
"""
import pytest

from stapel_listings.serializers import ListingCardSerializer
from stapel_listings.services.features import (
    PRESENTATION_NAME,
    PRESENTATION_NAME_VALUE,
    PRESENTATION_VALUE,
    PRESENTATION_VALUE_UNIT,
    build_projections,
    decorate_card_elements,
)


#: An apartment, as a real catalogue shapes one: a wall material from a
#: dictionary, a floor and a floor count that are vocabulary-backed NUMBERS,
#: an area in m², and a balcony flag.
APARTMENT_CONFIGS = [
    {
        "slug": "house_type",
        "name": "Тип дома",
        "show_at_title": True,
        "config": {"type": "select", "options": [
            {"value": "kirpichnyy", "label": "Кирпичный"},
            {"value": "panelnyy", "label": "Панельный"},
        ]},
    },
    {
        "slug": "floor",
        "name": "Этаж",
        "show_at_title": True,
        "config": {"type": "select", "options": [
            {"value": "3", "label": "3"},
            {"value": "4", "label": "4"},
        ]},
    },
    {
        "slug": "floors",
        "name": "Этажей в доме",
        "show_at_title": True,
        "config": {"type": "select", "options": [
            {"value": "9", "label": "9"},
        ]},
    },
    {
        "slug": "square",
        "name": "Площадь",
        "show_at_title": True,
        "config": {"type": "float", "postfix": "м²"},
    },
    {
        "slug": "balcony",
        "name": "Балкон",
        "show_as_badge": True,
        "config": {"type": "bool"},
    },
    {
        "slug": "loggia",
        "name": "Лоджия",
        "show_as_badge": True,
        "config": {"type": "bool"},
    },
]

APARTMENT_DRAFT = {
    "house_type": {"type": "select", "value": ["kirpichnyy"]},
    "floor": {"type": "select", "value": ["3"]},
    "floors": {"type": "select", "value": ["9"]},
    "square": {"type": "float", "value": 42.0},
    "balcony": {"type": "bool", "value": True},
    "loggia": {"type": "bool", "value": False},
}


@pytest.fixture
def apartment_title():
    projections = build_projections(APARTMENT_CONFIGS, APARTMENT_DRAFT)
    return decorate_card_elements(projections["features_title"])


@pytest.fixture
def apartment_badges():
    projections = build_projections(APARTMENT_CONFIGS, APARTMENT_DRAFT)
    return decorate_card_elements(projections["features_badges"])


def _by_slug(elements):
    return {element["slug"]: element for element in elements}


def test_dictionary_value_renders_alone(apartment_title):
    """«Кирпичный» — a wall material needs no label to be understood."""
    element = _by_slug(apartment_title)["house_type"]
    assert element["presentation"] == PRESENTATION_VALUE
    assert element["label"] == "Кирпичный"
    assert element["name"] == "Тип дома"
    assert "unit" not in element
    # The stored halves are still there, untouched.
    assert element["value"] == ["kirpichnyy"]


def test_number_with_a_unit_renders_value_and_unit(apartment_title):
    """«42 м²» — the unit comes off the feature, not off the client."""
    element = _by_slug(apartment_title)["square"]
    assert element["presentation"] == PRESENTATION_VALUE_UNIT
    assert element["label"] == "42"
    assert element["unit"] == "м²"
    assert element["name"] == "Площадь"


def test_number_without_a_unit_renders_name_and_value(apartment_title):
    """«Этаж: 3» / «Этажей в доме: 9» — the bug the whole contract exists for.

    Both are stored as dictionary values with numeric labels, so a rule that
    read the TYPE would print them bare and reproduce «… · 3 · 9». Both get
    the ``name_value`` colon (0.22.2, Д421): the label is a bare numeral, and
    that is true whether or not a vocabulary produced it — see
    ``test_raw_numeric_value_gets_a_colon_not_a_glued_name`` for the catalogue
    case (a ``ref_select`` term) this exact discriminator was fixed for.
    """
    elements = _by_slug(apartment_title)
    for slug, name, label in (
        ("floor", "Этаж", "3"),
        ("floors", "Этажей в доме", "9"),
    ):
        element = elements[slug]
        assert element["presentation"] == PRESENTATION_NAME_VALUE, slug
        assert element["label"] == label
        assert element["name"] == f"{name}:"
        assert "unit" not in element


def test_true_boolean_is_its_own_name_and_false_is_dropped(apartment_badges):
    """«Балкон» says everything; «Лоджия: нет» says nothing worth a badge."""
    elements = _by_slug(apartment_badges)
    assert elements["balcony"]["presentation"] == PRESENTATION_NAME
    assert elements["balcony"]["label"] == "Балкон"
    assert "loggia" not in elements


def test_headers_blanks_and_redacted_stubs_never_reach_a_card():
    elements = decorate_card_elements([
        {"slug": "sect", "type": "header", "name": "Дом"},
        {"slug": "empty", "type": "string", "name": "Комментарий", "value": ""},
        {"slug": "vin", "type": "string", "name": "VIN", "redacted": True,
         "present": True},
        {"slug": "ok", "type": "string", "name": "Серия", "value": "П-44"},
    ])
    assert [element["slug"] for element in elements] == ["ok"]
    assert elements[0]["presentation"] == PRESENTATION_VALUE


def test_multi_select_is_never_read_as_a_number():
    """Two labels joined can parse as a number and mean nothing of the kind."""
    (element,) = decorate_card_elements([
        {"slug": "rooms_multi", "type": "select", "name": "Комнаты",
         "value": ["1", "2"], "labels": ["1", "2"]},
    ])
    assert element["presentation"] == PRESENTATION_VALUE
    assert element["label"] == "1, 2"


def test_raw_numeric_value_gets_a_colon_not_a_glued_name():
    """«HONOR · Модель 90» read as one phrase — PASS-16 Д421.

    ``model`` here is a raw ``string``: no vocabulary behind its value, and
    its numeric caption gets the name's trailing colon.
    """
    (element,) = decorate_card_elements([
        {"slug": "model", "type": "string", "name": "Модель", "value": "90"},
    ])
    assert element["presentation"] == PRESENTATION_NAME_VALUE
    assert element["label"] == "90"
    assert element["name"] == "Модель:"


def test_vocabulary_backed_numeric_label_also_gets_a_colon():
    """The catalogue case 0.22.1 missed — measured live, PASS-16 Д421.

    A ``ref_select`` resolves to a catalogue TERM whose own label is the bare
    numeral «90» (a phone model number, not a floor count): vocabulary-backed
    by source, exactly like ``floor``/``floors`` above, and it glued exactly
    the same way («HONOR · Модель 90»). 0.22.1 exempted every
    vocabulary-backed caption from the colon; 0.22.2 fixes the discriminator
    to the label's shape instead of its source, so this now reads
    «Модель: 90» too.
    """
    (element,) = decorate_card_elements([
        {"slug": "model", "type": "ref_select", "name": "Модель",
         "value": ["honor-90"], "labels": ["90"]},
    ])
    assert element["presentation"] == PRESENTATION_NAME_VALUE
    assert element["label"] == "90"
    assert element["name"] == "Модель:"


def test_vocabulary_word_label_keeps_the_bare_name():
    """«Этаж третий» — a WORD term never takes the ``name_value`` colon.

    Unlike a numeral term, a word caption fails ``_is_numeric_caption`` and
    is rendered ``PRESENTATION_VALUE`` (caption alone), so it was never
    exempted by 0.22.1's rule in the first place and 0.22.2 does not touch
    it either.
    """
    (element,) = decorate_card_elements([
        {"slug": "floor", "type": "ref_select", "name": "Этаж",
         "value": ["third"], "labels": ["третий"]},
    ])
    assert element["presentation"] == PRESENTATION_VALUE
    assert element["label"] == "третий"
    assert element["name"] == "Этаж"


def test_mileage_is_grouped_with_its_unit():
    """«20 000 км», not «20000 км» — PASS-16 Д421."""
    (element,) = decorate_card_elements([
        {"slug": "mileage", "type": "int", "name": "Пробег", "value": 20000,
         "postfix": "км"},
    ])
    assert element["presentation"] == PRESENTATION_VALUE_UNIT
    assert element["label"] == "20\xa0000"
    assert element["unit"] == "км"


def test_a_year_is_never_grouped():
    """«2019», never «2 019» — a raw ``int`` year has no vocabulary either,
    so it takes the same colon as ``model`` above; grouping is the one thing
    it is exempt from, below :data:`GROUPING_THRESHOLD`."""
    (element,) = decorate_card_elements([
        {"slug": "year", "type": "int", "name": "Год выпуска", "value": 2019},
    ])
    assert element["presentation"] == PRESENTATION_NAME_VALUE
    assert element["label"] == "2019"
    assert element["name"] == "Год выпуска:"


def test_the_contract_only_adds_keys():
    """Backward compatibility: nothing stored is renamed or dropped.

    A word-label ``select`` (``PRESENTATION_VALUE``) on purpose: a numeric
    ``name_value`` caption mutates ``name`` with a trailing colon by design
    (see ``test_number_without_a_unit_renders_name_and_value``), which is not
    what this test is pinning.
    """
    stored = {
        "slug": "house_type", "type": "select", "name": "Тип дома", "order": 7,
        "title": True, "badge": False, "value": ["kirpichnyy"],
        "labels": ["Кирпичный"], "uiStyle": "dropdown", "maxSelected": 1,
    }
    (element,) = decorate_card_elements([dict(stored)])
    for key, value in stored.items():
        assert element[key] == value, key
    assert set(element) - set(stored) == {"label", "presentation"}


@pytest.mark.django_db
def test_the_card_serializer_carries_the_contract(user):
    """The wire, not just the builder — this is what a storefront reads."""
    from stapel_listings.models import Listing

    projections = build_projections(APARTMENT_CONFIGS, APARTMENT_DRAFT)
    listing = Listing.objects.create(
        owner=user,
        category_id="7",
        title="1-к квартира, 42 м², 3/9 эт.",
        price="9500000.00",
        currency="RUB",
        **projections,
    )

    data = ListingCardSerializer(listing).data
    title = _by_slug(data["features_title"])
    assert title["house_type"]["presentation"] == PRESENTATION_VALUE
    assert title["floor"]["presentation"] == PRESENTATION_NAME_VALUE
    assert title["square"]["unit"] == "м²"
    assert [element["slug"] for element in data["features_badges"]] == ["balcony"]

    # `features` is NOT decorated: the detail table has the schema's own layout
    # and the owner reads the raw stored DAO there.
    from stapel_listings.serializers import ListingDetailSerializer

    detail = ListingDetailSerializer(listing).data
    assert all("presentation" not in dao for dao in detail["features"])
