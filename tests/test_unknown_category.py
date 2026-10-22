"""An unknown category id is a structured 400, not a 500.

``categories.features`` raises ``LookupError`` for an id that names no
category; through in-process ``call()`` it used to arrive as a bare
``FunctionCallError`` that nothing in validate-draft or publish caught.
"""
import pytest

from stapel_listings.models import ListingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def categories_know_only_7(stub_categories):
    from stapel_core.comm.registry import function_registry

    known = function_registry._providers["categories.features"]

    def provider(payload):
        if str(payload["category_id"]) != "7":
            raise LookupError(f"category {payload['category_id']} not found")
        return known(payload)

    function_registry._providers["categories.features"] = provider
    yield


@pytest.fixture
def owner_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def stale_category_draft(draft_listing, categories_know_only_7):
    draft_listing.category_id = "999"
    draft_listing.save()
    return draft_listing


def test_validate_draft_names_the_category(owner_client, stale_category_draft):
    resp = owner_client.get(
        f"/listings/listings/{stale_category_draft.pk}/validate-draft/"
    )
    assert resp.status_code == 200
    data = resp.json()
    body = data.get("data", data)
    assert body["valid"] is False
    first = body["results"][0]
    assert first["slug"] == "category_id"
    assert first["params"] == {"category_id": "999"}


def test_publish_answers_400_with_the_category_result(owner_client, stale_category_draft):
    resp = owner_client.post(f"/listings/listings/{stale_category_draft.pk}/publish/")
    assert resp.status_code == 400
    assert "category_id" in str(resp.json())
    stale_category_draft.refresh_from_db()
    assert stale_category_draft.status == ListingStatus.DRAFT


def test_publish_service_refuses_with_a_validation_error(stale_category_draft):
    from django.core.exceptions import ValidationError

    from stapel_listings.services import publish as publish_service

    with pytest.raises(ValidationError):
        publish_service.publish_listing(stale_category_draft)
