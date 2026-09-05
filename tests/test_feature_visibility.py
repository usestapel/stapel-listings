"""A value marked non-public never leaves this module to a reader without it.

The product ruling: a VIN and an IMEI identify a *specific physical unit*, so
they may be collected, validated, moderated and shown to the seller who typed
them — and never published. Before stapel-attributes 0.8.0 there was nothing to
express that, so listing 287 on the live stand answered an anonymous
``GET /listings/api/v1/listings/287/`` with ``"value": "JTNBE40K803512345"``
and carried the same string in ``features_search``, where it was exactly
filterable.

Three kinds of test live here, and they fail for three different reasons:

* **Behaviour** — today's payloads are clean.
* **Structure** — every serializer emitting a feature column inherits the
  redacting mixin, so a *new* endpoint cannot quietly inherit the old leak.
* **Reach** — the raw columns are not read anywhere outside the files that are
  supposed to read them.

The last two matter more than the first. The original leak was not a bug in a
redaction rule; it was the absence of one, in a place nobody was looking.
"""
import inspect
from pathlib import Path

import pytest

import stapel_listings
from stapel_attributes.guard import assert_raw_access_confined
from stapel_listings import serializers as listing_serializers
from stapel_listings.events import _public_features_search
from stapel_listings.models import Listing, ListingStatus
from stapel_listings.serializers import (
    FeatureVisibilityMixin,
    ListingFeaturesOutputField,
)
from stapel_listings.services.features import build_projections
from stapel_listings.services.search_feed import build_search_document, hidden_slugs

pytestmark = pytest.mark.django_db

VIN = "JTNBE40K803512345"

VIN_DEF = {
    "id": 3,
    "slug": "vin",
    "name": "VIN, номер кузова или SN",
    "mandatory": True,
    "visibility": "owner",
    "config": {"type": "string", "minLength": 17, "maxLength": 17},
}


@pytest.fixture
def schema_with_vin(stub_categories):
    """The stub category schema, plus a mandatory owner-only VIN."""
    stub_categories.append(dict(VIN_DEF))
    return stub_categories


@pytest.fixture
def draft_with_vin(draft_listing, schema_with_vin):
    draft_listing.features_draft = {
        **draft_listing.features_draft,
        "vin": {"type": "string", "value": VIN},
    }
    draft_listing.save(update_fields=["features_draft"])
    return draft_listing


@pytest.fixture
def published_with_vin(draft_with_vin):
    """Published and INDEXED, so an anonymous retrieve actually reaches it.

    ``publish_listing`` leaves the row in ``pending`` when the preset moderates
    on publish; that status is not in ``INDEXED_STATUSES``, so a stranger would
    get a 404 and the redaction would never be exercised. The point of these
    tests is the payload, not the lifecycle.
    """
    from stapel_listings.services.publish import publish_listing

    publish_listing(draft_with_vin)
    draft_with_vin.refresh_from_db()
    if draft_with_vin.status != ListingStatus.PUBLISHED:
        Listing.objects.filter(pk=draft_with_vin.pk).update(
            status=ListingStatus.PUBLISHED
        )
        draft_with_vin.refresh_from_db()
    return draft_with_vin


# --------------------------------------------------------------- build time

class TestTheProjectionsAreBuiltClean:
    """Three of the four columns are public artefacts and never hold the value.

    They are read raw — by a card, by the indexer, by two bus payloads — none
    of which has a viewer in hand. The only way they can be safe for every
    reader is for the value never to enter them.
    """

    def test_the_value_is_stored_in_features(self, schema_with_vin):
        projections = build_projections(
            schema_with_vin,
            {
                "mileage": {"type": "int", "value": 1},
                "vin": {"type": "string", "value": VIN},
            },
        )
        vin_dao = next(d for d in projections["features"] if d["slug"] == "vin")
        assert vin_dao["value"] == VIN
        assert vin_dao["visibility"] == "owner"

    def test_it_is_absent_from_features_search(self, schema_with_vin):
        projections = build_projections(
            schema_with_vin,
            {
                "mileage": {"type": "int", "value": 1},
                "vin": {"type": "string", "value": VIN},
            },
        )
        assert "vin" not in projections["features_search"]
        # ... and the public sibling is still indexed, so the filter is
        # targeted rather than a blanket refusal.
        assert projections["features_search"]["mileage"] == [1]

    def test_it_is_never_a_title_or_a_badge_even_if_the_schema_asks(
        self, schema_with_vin
    ):
        schema_with_vin[-1]["show_at_title"] = True
        schema_with_vin[-1]["show_as_badge"] = True
        projections = build_projections(
            schema_with_vin,
            {
                "mileage": {"type": "int", "value": 1},
                "vin": {"type": "string", "value": VIN},
            },
        )
        assert [d["slug"] for d in projections["features_title"]] == ["mileage"]
        assert "vin" not in [d["slug"] for d in projections["features_badges"]]

    def test_a_public_schema_is_projected_exactly_as_before(self, stub_categories):
        """The axis costs nothing when nobody uses it."""
        projections = build_projections(
            stub_categories, {"mileage": {"type": "int", "value": 42000}}
        )
        mileage = next(d for d in projections["features"] if d["slug"] == "mileage")
        assert "visibility" not in mileage
        assert projections["features_search"] == {"mileage": [42000]}


# ---------------------------------------------------------------- read time

class TestTheAnonymousPayload:
    def test_the_detail_read_carries_no_vin_string_anywhere(
        self, api_client, published_with_vin
    ):
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        assert response.status_code == 200
        assert VIN not in response.content.decode()

    def test_the_row_survives_as_a_stub_so_a_buyer_can_see_it_exists(
        self, api_client, published_with_vin
    ):
        """Dropping the row would hide that the field exists at all, which is a
        worse answer for a buyer deciding whether to ask."""
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        vin = next(d for d in response.data["features"] if d["slug"] == "vin")
        assert vin["redacted"] is True
        assert vin["present"] is True
        assert "value" not in vin
        assert vin["name"] == VIN_DEF["name"]

    def test_the_public_siblings_are_untouched(self, api_client, published_with_vin):
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        mileage = next(d for d in response.data["features"] if d["slug"] == "mileage")
        assert mileage["value"] == 42000
        assert "redacted" not in mileage

    def test_the_engine_claims_no_verification(self, api_client, published_with_vin):
        """Presence is observed; verification is a claim, and nothing here runs
        a VIN check. The stub must not imply one."""
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        vin = next(d for d in response.data["features"] if d["slug"] == "vin")
        assert "verification" not in vin

    def test_the_card_list_carries_no_vin(self, api_client, published_with_vin):
        assert VIN not in api_client.get("/listings/listings/").content.decode()


class TestTheOwnersOwnView:
    def test_the_owner_reads_their_own_value(self, api_client, user, published_with_vin):
        api_client.force_authenticate(user)
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        vin = next(d for d in response.data["features"] if d["slug"] == "vin")
        assert vin["value"] == VIN

    def test_another_signed_in_user_does_not(
        self, api_client, other_user, published_with_vin
    ):
        api_client.force_authenticate(other_user)
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        vin = next(d for d in response.data["features"] if d["slug"] == "vin")
        assert vin["redacted"] is True

    def test_the_owner_dashboard_keeps_the_value(
        self, api_client, user, published_with_vin
    ):
        """``my_listings`` builds its serializer by hand; without an explicit
        context the mixin would fail closed and redact the seller's own VIN out
        of their own dashboard."""
        api_client.force_authenticate(user)
        response = api_client.get("/listings/listings/my/listings/")
        assert response.status_code == 200
        row = next(
            r for r in response.data["items"] if r["id"] == published_with_vin.pk
        )
        # The dashboard card projects title/badges, which never carry a hidden
        # value at all — what this pins is that the OWNER audience is resolved,
        # so nothing on their own row comes back redacted.
        assert not any(d.get("redacted") for d in row["features_title"])
        assert VIN not in str(row["features_title"]), (
            "a hidden value must not reach a card projection even for its owner"
        )

    def test_a_staff_reader_keeps_the_value(
        self, api_client, other_user, published_with_vin
    ):
        other_user.is_staff = True
        other_user.save(update_fields=["is_staff"])
        api_client.force_authenticate(other_user)
        response = api_client.get(f"/listings/listings/{published_with_vin.pk}/")
        vin = next(d for d in response.data["features"] if d["slug"] == "vin")
        assert vin["value"] == VIN


class TestTheMixinFailsClosed:
    def test_no_request_in_context_means_anonymous(self, published_with_vin):
        """A comm caller, a management command, a bare ``many=True`` — anything
        that never said who is asking gets the redacted payload."""
        data = listing_serializers.ListingDetailSerializer(published_with_vin).data
        vin = next(d for d in data["features"] if d["slug"] == "vin")
        assert vin["redacted"] is True


# ---------------------------------------------------- the indexer's document

class TestTheSearchDocument:
    def test_it_carries_no_hidden_value(self, published_with_vin):
        doc = build_search_document(published_with_vin)
        assert "vin" not in doc["features_search"]
        assert VIN not in repr(doc)

    def test_a_stale_row_is_filtered_on_the_way_out(self, published_with_vin):
        """The installed base on the day this ships is rows projected BEFORE
        the axis: their ``features_search`` still holds the VIN until
        ``listings_reproject_features`` runs. An indexer that pulled one in the
        meantime would keep serving it as a filterable term long afterwards, so
        the document builder filters again rather than trusting its input.
        """
        published_with_vin.features_search = {
            **published_with_vin.features_search,
            "vin": [VIN],
        }
        published_with_vin.save(update_fields=["features_search"])
        assert hidden_slugs(published_with_vin) == {"vin"}
        doc = build_search_document(published_with_vin)
        assert "vin" not in doc["features_search"]

    def test_a_stale_title_projection_is_filtered_too(self, published_with_vin):
        """``features_title`` feeds the free-text arm — a hidden value there is
        the same oracle as a facet, spelled differently."""
        published_with_vin.features_title = list(published_with_vin.features_title) + [
            {"slug": "vin", "type": "string", "value": VIN, "visibility": "owner",
             "title": True}
        ]
        published_with_vin.save(update_fields=["features_title"])
        doc = build_search_document(published_with_vin)
        assert VIN not in repr(doc["features_title"])

    def test_the_bus_payload_is_filtered_the_same_way(self, published_with_vin):
        """An event fans out to every subscriber in the fleet — the widest
        audience a value ever reaches."""
        published_with_vin.features_search = {
            **published_with_vin.features_search,
            "vin": [VIN],
        }
        assert "vin" not in _public_features_search(published_with_vin)


# ------------------------------------------------------- the structural gates

class TestEveryFeatureEmittingSerializerRedacts:
    """The gate that catches the endpoint added next quarter.

    The original leak was not a wrong redaction rule — it was a plain
    ``JSONField`` that every new serializer listing ``features`` inherited for
    free. This fails on the day such a serializer is written.
    """

    def _serializers_emitting_features(self):
        for name, obj in vars(listing_serializers).items():
            if not inspect.isclass(obj) or not hasattr(obj, "_declared_fields"):
                continue
            if getattr(obj, "__module__", None) != listing_serializers.__name__:
                continue
            declared = obj._declared_fields
            emits = any(
                isinstance(field, ListingFeaturesOutputField)
                for field in declared.values()
            )
            meta_fields = set(getattr(getattr(obj, "Meta", None), "fields", None) or ())
            emits = emits or bool(
                meta_fields & set(FeatureVisibilityMixin.FEATURE_DAO_FIELDS)
            )
            if emits:
                yield name, obj

    def test_there_is_at_least_one_to_check(self):
        assert list(self._serializers_emitting_features())

    def test_all_of_them_inherit_the_mixin(self):
        offenders = [
            name
            for name, cls in self._serializers_emitting_features()
            if not issubclass(cls, FeatureVisibilityMixin)
        ]
        assert offenders == [], (
            f"{offenders} emit a feature column without FeatureVisibilityMixin. "
            "A stored DAO may be a VIN or an IMEI; inherit the mixin so the "
            "payload is redacted for whoever is actually asking."
        )


def test_the_raw_feature_columns_are_read_only_where_they_should_be():
    """Reach, not correctness: nothing outside these files touches the columns.

    A grep rather than an import graph — a grep cannot be defeated by
    indirection, and adding a file below is a line in this test that the next
    reviewer reads.
    """
    assert_raw_access_confined(
        root=Path(stapel_listings.__file__).parent,
        names=("features", "features_title", "features_badges", "features_search"),
        # `categories.features` is the comm function returning the category
        # SCHEMA — the definitions, not the values. Same word, different thing;
        # blank the homograph so these files keep being checked for the real
        # columns instead of being allowlisted wholesale.
        ignore=(r"categories\.features",),
        allow=(
            # Declares the columns and re-derives features_search from features.
            "models.py",
            # THE definition of the projections; drops hidden values at build.
            "services/features.py",
            # Promotes a validated draft through build_projections.
            "services/publish.py",
            # Re-stamps stored values after a visibility change.
            "services/reproject.py",
            # The command that drives it: prose and an argparse help string.
            "management/commands/listings_reproject_features.py",
            # Prose only, and about the schema half of the word: it renames
            # draft KEYS and prints counts. Values never reach it — the
            # projections are rebuilt by services/reproject.py, which is
            # where the redaction already lives.
            "management/commands/listings_rename_feature_keys.py",
            # The document builder: filters again on the way out.
            "services/search_feed.py",
            # Emits the two bus payloads through _public_features_search.
            "events.py",
            # The redaction chokepoint itself.
            "serializers.py",
            # Read-only JSON in the Django admin, which is staff-only.
            "admin.py",
            # Resolves the category SCHEMA over comm; handles FeatureDefs, not
            # stored values — it never sees a listing row.
            "services/category_schema.py",
            # `listings.draft_content` answers ``features_draft`` — the
            # seller's own unredacted draft values — and only ever to their
            # OWNER: the payload's owner_id is checked against the row and a
            # mismatch raises LookupError. Same exemption, and the same
            # reason, as ListingDraftSerializer's below: the reader is the
            # person who typed the value. It emits no published projection.
            "functions.py",
            # The test harness's own settings module and stub schema provider.
            "conftest.py",
            "_codegen_settings.py",
        ),
    )


def test_features_draft_is_the_owners_own_write_and_is_not_scanned():
    """An honest note about what this gate does NOT cover.

    ``ListingDraftSerializer`` echoes ``features_draft`` — the raw DTO the
    seller submitted, values and all — back in the create/update/save-draft
    responses. That is not redacted and should not be: every one of those
    routes is behind ``ListingViewSet._get_own``, so the only reader is the
    person who just typed the value. This test pins that gate rather than
    leaving the exemption implicit.
    """
    from stapel_listings.views import ListingViewSet

    assert hasattr(ListingViewSet, "_get_own")
    draft_fields = set(listing_serializers.ListingDraftSerializer.Meta.fields)
    assert "features_draft" in draft_fields
    assert not (draft_fields & {"features", "features_title", "features_badges"}), (
        "the draft serializer must not also emit a published projection — it is "
        "the one feature payload that is deliberately not redacted"
    )


def test_a_reprojection_restamps_a_row_written_before_the_axis(
    published_with_vin, schema_with_vin
):
    """The migration story, as a test.

    A value stored before its definition became non-public carries no stamp and
    still reads as public. Changing ``visibility`` is not complete until
    ``listings_reproject_features`` has run.
    """
    from stapel_listings.services.reproject import reproject_listings

    stale = [
        {k: v for k, v in dao.items() if k != "visibility"}
        for dao in published_with_vin.features
    ]
    Listing.objects.filter(pk=published_with_vin.pk).update(
        features=stale, features_search={"vin": [VIN], "mileage": [42000]}
    )
    published_with_vin.refresh_from_db()
    assert hidden_slugs(published_with_vin) == set(), "precondition: the row is stale"

    before = published_with_vin.status
    reproject_listings(category_ids=[published_with_vin.category_id])

    published_with_vin.refresh_from_db()
    # The command re-derives the projections and nothing else: lifecycle and
    # moderation are untouched, which is what makes it safe to run on a live
    # stand.
    assert published_with_vin.status == before
    assert hidden_slugs(published_with_vin) == {"vin"}
    assert "vin" not in published_with_vin.features_search
