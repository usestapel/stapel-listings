"""Conditional rules and vocabulary-backed features on the publish path.

stapel-attributes 0.5.0 runs a rule pre-pass before every per-feature check,
so requiredness is `RuleState.required` — not `FeatureDef.mandatory` — and a
feature the rules hide is neither validated nor stored. Both facts change what
publishing does, and neither is visible from this module's own code: the six
new `FeatureDef` keys arrive inside the `categories.features` payload and are
carried through untouched. These tests are the gate on that carriage.
"""
import pytest
from django.core.exceptions import ValidationError

from stapel_attributes.results import ValidationErrorCode
from stapel_listings.models import Listing
from stapel_listings.services import publish as publish_service

pytestmark = pytest.mark.django_db


# A schema whose shape is the one a catalogue import produces: a controlling
# select, a conditionally required sibling, a conditionally hidden number, and
# an option forbidden by the same control.
RULE_FEATURE_DEFS = [
    {
        "id": 1,
        "slug": "condition",
        "name": "Condition",
        "mandatory": True,
        "config": {
            "type": "select",
            "maxSelected": 1,
            "options": [
                {"value": "novoe", "label": "cond.new"},
                {"value": "b-u", "label": "cond.used"},
            ],
        },
    },
    {
        "id": 2,
        "slug": "screen_condition",
        "name": "Screen condition",
        "mandatory": False,
        "config": {
            "type": "select",
            "maxSelected": 1,
            "options": [
                {"value": "ideal", "label": "screen.ideal"},
                {"value": "scratched", "label": "screen.scratched"},
            ],
        },
        "rules": [
            {
                "effect": "require",
                "when": {"all": [{"feature": "condition", "op": "in", "values": ["b-u"]}]},
            }
        ],
    },
    {
        "id": 3,
        "slug": "akb",
        "name": "Battery health",
        "mandatory": False,
        "config": {"type": "int", "min": 0, "max": 100, "postfix": "%"},
        "rules": [
            {
                "effect": "hide",
                "when": {"all": [{"feature": "condition", "op": "in", "values": ["novoe"]}]},
            }
        ],
    },
    {
        "id": 4,
        "slug": "warranty",
        "name": "Warranty",
        "mandatory": False,
        "config": {
            "type": "select",
            "maxSelected": 1,
            "options": [
                {"value": "yes", "label": "warranty.yes"},
                {"value": "no", "label": "warranty.no"},
            ],
        },
        "rules": [
            {
                "effect": "forbid_option",
                "option": "yes",
                "when": {"all": [{"feature": "condition", "op": "in", "values": ["b-u"]}]},
            }
        ],
    },
]


REF_FEATURE_DEFS = [
    {
        "id": 1,
        "slug": "vendor",
        "name": "Vendor",
        "mandatory": False,
        "show_at_title": True,
        "config": {
            "type": "ref_select",
            "optionsRef": {"vocabulary": "phones", "level": "Vendor"},
            "minSelected": 0,
            "maxSelected": 1,
            "uiStyle": "dropdown",
        },
    },
    {
        "id": 2,
        "slug": "model",
        "name": "Model",
        "mandatory": False,
        "show_as_badge": True,
        "config": {
            "type": "ref_select",
            "optionsRef": {
                "vocabulary": "phones",
                "level": "Model",
                "parentFeature": "vendor",
            },
            "minSelected": 0,
            "maxSelected": 1,
            "uiStyle": "dropdown",
        },
    },
]


def _install(stub_categories, feature_defs):
    """Swap the stub schema and drop the revision-keyed config cache.

    ``get_feature_configs`` caches on ``(category_id, revision)`` and the
    process-wide locmem cache outlives a test, so reshaping the schema under
    the same revision has to invalidate it explicitly.
    """
    from django.core.cache import cache

    cache.clear()
    stub_categories[:] = [dict(d) for d in feature_defs]
    return stub_categories


@pytest.fixture
def rules_schema(stub_categories):
    """Reshape the stub ``categories.features`` into the rule-bearing schema."""
    yield _install(stub_categories, RULE_FEATURE_DEFS)
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def ref_schema(stub_categories):
    """The two-level ref_select schema plus the in-memory vocabulary it needs."""
    from django.core.cache import cache

    from stapel_attributes.tests.fake_vocabulary import FakeVocabularyResolver
    from stapel_attributes.vocabularies import register_vocabulary_resolver

    register_vocabulary_resolver(FakeVocabularyResolver())
    yield _install(stub_categories, REF_FEATURE_DEFS)
    register_vocabulary_resolver(None)
    cache.clear()


@pytest.fixture
def make_listing(db, user):
    """Factory: a DRAFT listing carrying *features* and ready to publish."""

    def _make(features):
        return Listing.objects.create(
            owner=user,
            category_id="7",
            title_draft="Pixel 8",
            description_draft="A well kept phone in great condition.",
            price_draft="400.00",
            currency="EUR",
            images_draft=["product/abc123"],
            # A publishable draft carries a place (Д71).
            lat_draft="55.755800",
            lon_draft="37.617300",
            features_draft=features,
        )

    return _make


def _error_for(result, slug):
    """The structured result entry for *slug*, or None."""
    return next((r for r in result.results if r.slug == slug), None)


# --------------------------------------------------------------------------
# rules on the publish path
# --------------------------------------------------------------------------


def test_a_rule_that_does_not_fire_leaves_publishing_alone(rules_schema, make_listing):
    """`condition=novoe`: the sibling is not required, so the draft is complete."""
    listing = make_listing({"condition": {"type": "select", "value": ["novoe"]}})

    assert publish_service.validate_draft(listing).valid
    publish_service.publish_listing(listing)
    listing.refresh_from_db()

    assert {dao["slug"] for dao in listing.features} == {"condition"}


def test_a_conditionally_required_sibling_blocks_both_paths(rules_schema, make_listing):
    """`condition=b-u` requires `screen_condition` — `mandatory` is False on it.

    The structured path and the raising path must agree: a draft the validate
    endpoint calls invalid cannot be one `publish_listing` accepts.
    """
    listing = make_listing({"condition": {"type": "select", "value": ["b-u"]}})

    result = publish_service.validate_draft(listing)
    assert not result.valid
    entry = _error_for(result, "screen_condition")
    assert entry is not None, "the rule-required sibling is not reported at all"
    assert entry.error == ValidationErrorCode.MANDATORY_MISSING

    with pytest.raises(ValidationError) as excinfo:
        publish_service.publish_listing(listing)
    assert any("screen_condition" in message for message in excinfo.value.messages)

    listing.refresh_from_db()
    assert listing.features == []


def test_the_same_draft_publishes_once_the_sibling_is_answered(rules_schema, make_listing):
    listing = make_listing(
        {
            "condition": {"type": "select", "value": ["b-u"]},
            "screen_condition": {"type": "select", "value": ["scratched"]},
        }
    )

    assert publish_service.validate_draft(listing).valid
    publish_service.publish_listing(listing)
    listing.refresh_from_db()

    assert listing.features_search["screen_condition"] == ["scratched"]


def test_a_hidden_feature_is_dropped_even_when_it_was_submitted(rules_schema, make_listing):
    """`condition=novoe` hides `akb`. A hidden answer is not stored, anywhere.

    The value is in the draft — a composer that filled it before the control
    moved, or a client that never filtered — and it must not survive into the
    published projections, or a listing shows an attribute its own schema says
    does not apply to it.
    """
    listing = make_listing(
        {
            "condition": {"type": "select", "value": ["novoe"]},
            "akb": {"type": "int", "value": 87},
        }
    )

    assert publish_service.validate_draft(listing).valid
    publish_service.publish_listing(listing)
    listing.refresh_from_db()

    assert "akb" not in {dao["slug"] for dao in listing.features}
    assert "akb" not in listing.features_search
    assert listing.features_draft["akb"]["value"] == 87, "the draft keeps the answer"


def test_a_forbidden_option_is_rejected_as_not_in_options(rules_schema, make_listing):
    """`forbid_option` narrows the config, so `select` reports it itself.

    No new error vocabulary for a rule violation: the option is removed from
    the config before `parse_config`, and the type answers with the code it
    always answers with for a value outside its options.
    """
    listing = make_listing(
        {
            "condition": {"type": "select", "value": ["b-u"]},
            "screen_condition": {"type": "select", "value": ["ideal"]},
            "warranty": {"type": "select", "value": ["yes"]},
        }
    )

    result = publish_service.validate_draft(listing)
    assert not result.valid
    entry = _error_for(result, "warranty")
    assert entry is not None
    assert entry.error == ValidationErrorCode.NOT_IN_OPTIONS

    with pytest.raises(ValidationError):
        publish_service.publish_listing(listing)


def test_the_same_option_passes_when_the_rule_does_not_fire(rules_schema, make_listing):
    listing = make_listing(
        {
            "condition": {"type": "select", "value": ["novoe"]},
            "warranty": {"type": "select", "value": ["yes"]},
        }
    )

    assert publish_service.validate_draft(listing).valid
    publish_service.publish_listing(listing)
    listing.refresh_from_db()

    assert listing.features_search["warranty"] == ["yes"]


# --------------------------------------------------------------------------
# vocabulary-backed features in the four projections
# --------------------------------------------------------------------------


def test_a_ref_dao_carries_labels_to_the_title_and_badges(ref_schema, make_listing):
    """Title and badge projections are the DAO itself, and a ref DAO snapshots
    its ``labels`` at write time — so display never re-reads the vocabulary,
    while ``value`` stays the codes a filter is built from."""
    listing = make_listing(
        {
            "vendor": {"type": "ref_select", "value": ["apple"]},
            "model": {"type": "ref_select", "value": ["iphone-15"]},
        }
    )

    publish_service.publish_listing(listing)
    listing.refresh_from_db()

    (title,) = listing.features_title
    assert title["slug"] == "vendor"
    assert title["labels"] == ["Apple"]
    assert title["value"] == ["apple"]

    (badge,) = listing.features_badges
    assert badge["slug"] == "model"
    assert badge["labels"] == ["iPhone 15"]
    assert badge["value"] == ["iphone-15"]


def test_a_ref_feature_indexes_its_codes_not_its_labels(ref_schema, make_listing):
    """`features_search` is the filter axis: codes, exactly as for `select`.

    A label is a display string that changes with the vocabulary's language;
    indexing it would make a stored filter stop matching on translation.
    """
    listing = make_listing(
        {
            "vendor": {"type": "ref_select", "value": ["apple"]},
            "model": {"type": "ref_select", "value": ["iphone-15"]},
        }
    )

    publish_service.publish_listing(listing)
    listing.refresh_from_db()

    assert listing.features_search == {"vendor": ["apple"], "model": ["iphone-15"]}


def test_features_search_survives_the_rederivation(ref_schema, make_listing):
    """`features_search` is derived from `features`; re-deriving it must not
    quietly turn a ref feature's codes into anything else."""
    listing = make_listing({"vendor": {"type": "ref_select", "value": ["apple"]}})

    publish_service.publish_listing(listing)
    listing.refresh_from_db()
    before = dict(listing.features_search)

    listing.features_search = {}
    listing.rebuild_features_search()
    assert listing.features_search == before


def test_a_ref_value_outside_the_parent_is_rejected(ref_schema, make_listing):
    """The parent narrows the level: `galaxy-s24` is not an Apple model."""
    listing = make_listing(
        {
            "vendor": {"type": "ref_select", "value": ["apple"]},
            "model": {"type": "ref_select", "value": ["galaxy-s24"]},
        }
    )

    result = publish_service.validate_draft(listing)
    assert not result.valid
    entry = _error_for(result, "model")
    assert entry is not None
    assert entry.error == ValidationErrorCode.NOT_IN_OPTIONS

    with pytest.raises(ValidationError):
        publish_service.publish_listing(listing)


# --------------------------------------------------------------------------
# the carriage itself
# --------------------------------------------------------------------------


def test_the_new_feature_def_keys_reach_the_engine_untouched(rules_schema):
    """`get_feature_configs` must not whitelist keys.

    The six v2 keys (`rules`, `description`, `example`, `default`, `hints`,
    `group`) live on the `categories.features` payload; this module neither
    reads nor reshapes them, it hands the dicts to `coerce_feature_defs`. A
    whitelist anywhere on that path would silently disarm every rule.
    """
    from stapel_attributes import coerce_feature_defs

    from stapel_listings.services.category_schema import get_feature_configs

    configs = get_feature_configs("7", use_cache=False)
    assert configs[1]["rules"] == RULE_FEATURE_DEFS[1]["rules"]

    defs = {d.slug: d for d in coerce_feature_defs(configs)}
    assert defs["screen_condition"].rules == RULE_FEATURE_DEFS[1]["rules"]
    assert defs["akb"].rules[0]["effect"] == "hide"
