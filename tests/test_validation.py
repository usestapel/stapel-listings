"""Value-validation delegation and category-schema fetch/caching over comm."""
import pytest

from stapel_listings.services import category_schema, publish as publish_service

pytestmark = pytest.mark.django_db


def test_get_feature_configs_fetches_over_comm(stub_categories):
    configs = category_schema.get_feature_configs("42")
    assert [c["slug"] for c in configs] == ["mileage", "condition"]


def test_get_feature_configs_is_cached(stub_categories):
    category_schema.get_feature_configs("42")
    # Mutate the underlying stub; without invalidation the cache still serves the
    # old shape.
    stub_categories.clear()
    assert len(category_schema.get_feature_configs("42")) == 2
    # Bypassing the cache reflects the mutation.
    assert category_schema.get_feature_configs("42", use_cache=False) == []


def test_validate_draft_valid(draft_listing):
    result = publish_service.validate_draft(draft_listing)
    assert result.valid is True


def test_validate_draft_reports_below_minimum(draft_listing, stub_categories):
    # Set mileage below its configured min (0) to trigger a structured code.
    stub_categories[0]["config"]["min"] = 100000
    category_schema.invalidate("7")
    result = publish_service.validate_draft(draft_listing)
    assert result.valid is False
    mileage = next(r for r in result.results if r.slug == "mileage")
    assert mileage.error is not None
    assert mileage.localizable_error is not None


def test_validate_draft_short_description(draft_listing):
    draft_listing.description_draft = "hi"
    draft_listing.save()
    result = publish_service.validate_draft(draft_listing)
    assert result.valid is False
    assert any(r.slug == "description" for r in result.results)


def test_invalid_value_type_blocks_publish(draft_listing):
    from django.core.exceptions import ValidationError

    draft_listing.features_draft = {"mileage": {"type": "int", "value": "not-a-number"}}
    draft_listing.save()
    with pytest.raises(ValidationError):
        publish_service.publish_listing(draft_listing)
