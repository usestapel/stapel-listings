"""``features_draft`` is WRITTEN as ``{slug: FeatureDto}`` but a listing is
READ back with features as a *list* of decorated DAOs (``features`` /
``features_title`` / ``features_badges`` — ``{slug, name, label,
presentation, …}``). A client that fetches a listing and posts that same
list back under ``features_draft`` used to get a 400 naming only the TYPE
DRF's own field validation saw ("expected a dictionary") and never the
shape that would have worked.

0.22.3 fixes both halves: the write accepts either shape (round trip), and
every ``features_draft`` shape 400 now names one of three failure kinds —
each carrying a one-line example of the accepted shape in
``params["example"]``.
"""
import json

import pytest

from stapel_listings.errors import (
    ERR_400_FEATURES_DRAFT_SHAPE,
    ERR_400_FEATURES_DRAFT_UNKNOWN_SLUG,
    ERR_400_FEATURES_DRAFT_VALUE_SHAPE,
)

pytestmark = pytest.mark.django_db


def _save_draft_url(listing):
    return f"/listings/listings/{listing.pk}/save-draft/"


def _draft_url(listing):
    return f"/listings/listings/{listing.pk}/draft/"


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


# --- accepted shapes: the round trip --------------------------------------


def test_the_canonical_dict_form_still_writes(auth_client, draft_listing):
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": {"mileage": {"type": "int", "value": 55000}}},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.data["features_draft"]["mileage"]["value"] == 55000

    read = auth_client.get(_draft_url(draft_listing))
    assert read.data["features_draft"]["mileage"]["value"] == 55000


def test_the_read_list_shape_round_trips_back_into_a_write(auth_client, draft_listing):
    """The exact defect: post back what a listing READ hands you.

    Every key beyond ``slug``/``type``/``value`` here (``name``, ``order``,
    ``title``, ``badge``, ``labels``, ``translate``) is decoration this
    module itself adds on the way OUT — a client that copies it back should
    not have to strip it first.
    """
    read_shape = [
        {
            "slug": "mileage",
            "type": "int",
            "value": 61000,
            "name": "Mileage",
            "order": 0,
            "title": True,
            "translate": None,
        },
        {
            "slug": "condition",
            "type": "select",
            "value": ["new"],
            "labels": ["cond.new"],
            "name": "Condition",
            "order": 1,
            "badge": True,
        },
    ]
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": read_shape},
        format="json",
    )
    assert resp.status_code == 200, resp.content

    read = auth_client.get(_draft_url(draft_listing))
    features = read.data["features_draft"]
    assert features["mileage"]["value"] == 61000
    assert features["condition"]["value"] == ["new"]
    # Decoration never entered the stored dict form.
    assert "name" not in features["mileage"]
    assert "labels" not in features["condition"]


def test_features_draft_null_still_clears_it(auth_client, draft_listing):
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": None},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert not resp.data["features_draft"]


# --- the three failure kinds, each naming its own shape -------------------


def test_features_draft_shape_400_on_a_non_object_non_list(auth_client, draft_listing):
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": "oops"},
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.data["localizable_error"] == ERR_400_FEATURES_DRAFT_SHAPE
    example = resp.data["params"]["example"]
    assert json.loads(example) == {"mileage": {"type": "int", "value": 42000}}
    assert example in resp.data["error"]

    draft_listing.refresh_from_db()
    assert draft_listing.features_draft == {
        "mileage": {"type": "int", "value": 42000},
        "condition": {"type": "select", "value": ["used"]},
    }


def test_features_draft_value_shape_400_on_a_scalar_dict_entry(auth_client, draft_listing):
    """Dict form, but a value that is not itself ``{"type": ..., "value": ...}``."""
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": {"mileage": 42000}},
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.data["localizable_error"] == ERR_400_FEATURES_DRAFT_VALUE_SHAPE
    assert resp.data["params"]["slug"] == "mileage"
    example = resp.data["params"]["example"]
    assert json.loads(example) == {"mileage": {"type": "int", "value": 42000}}


def test_features_draft_value_shape_400_on_a_non_object_list_entry(auth_client, draft_listing):
    """List form, but an entry that isn't an object at all."""
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": ["mileage"]},
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.data["localizable_error"] == ERR_400_FEATURES_DRAFT_VALUE_SHAPE
    example = resp.data["params"]["example"]
    assert json.loads(example) == [{"slug": "mileage", "type": "int", "value": 42000}]


def test_features_draft_unknown_slug_400_on_a_list_entry_missing_slug(
    auth_client, draft_listing
):
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": [{"type": "int", "value": 1}]},
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.data["localizable_error"] == ERR_400_FEATURES_DRAFT_UNKNOWN_SLUG
    assert resp.data["params"]["index"] == 0
    example = resp.data["params"]["example"]
    assert json.loads(example) == [{"slug": "mileage", "type": "int", "value": 42000}]


def test_features_draft_unknown_slug_400_on_a_blank_slug(auth_client, draft_listing):
    resp = auth_client.post(
        _save_draft_url(draft_listing),
        {"features_draft": [{"slug": "", "type": "int", "value": 1}]},
        format="json",
    )
    assert resp.status_code == 400, resp.content
    assert resp.data["localizable_error"] == ERR_400_FEATURES_DRAFT_UNKNOWN_SLUG


def test_create_also_normalizes_the_read_shape(auth_client):
    """The write shape check runs on ``create`` too, not just save-draft."""
    resp = auth_client.post(
        "/listings/listings/",
        {
            "features_draft": [
                {"slug": "mileage", "type": "int", "value": 10, "name": "Mileage"},
            ]
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.data["features_draft"] == {"mileage": {"type": "int", "value": 10}}


def test_update_also_normalizes_the_read_shape(auth_client, draft_listing):
    resp = auth_client.put(
        f"/listings/listings/{draft_listing.pk}/",
        {
            "features_draft": [
                {"slug": "mileage", "type": "int", "value": 77000, "name": "Mileage"},
            ]
        },
        format="json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.data["features_draft"] == {"mileage": {"type": "int", "value": 77000}}
