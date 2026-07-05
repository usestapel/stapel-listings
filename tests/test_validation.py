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


# --- M-7: validate-draft and publish agree on unknown slugs -----------------


def test_m7_unknown_slug_flagged_by_validate_draft(draft_listing, stub_categories):
    """A draft feature removed from the category schema: validate_draft must
    report it invalid (per-feature detail), matching publish's rejection —
    instead of the old 'valid=True here, opaque 400 on publish' divergence."""
    # Remove 'condition' from the category schema; the draft still carries it.
    stub_categories[:] = [d for d in stub_categories if d["slug"] != "condition"]
    category_schema.invalidate("7")

    result = publish_service.validate_draft(draft_listing)
    assert result.valid is False
    unknown = next(r for r in result.results if r.slug == "condition")
    assert unknown.status.value == "validation_failed"
    assert unknown.localizable_error == "error.400.listing_feature_not_allowed"


def test_m7_publish_rejects_same_unknown_slug(draft_listing, stub_categories):
    """publish_listing() rejects exactly what validate_draft flags (convergence)."""
    from django.core.exceptions import ValidationError

    stub_categories[:] = [d for d in stub_categories if d["slug"] != "condition"]
    category_schema.invalidate("7")
    with pytest.raises(ValidationError):
        publish_service.publish_listing(draft_listing)


# --- M-6: versioned cache pointer closes the read-then-set race -------------


def test_m6_stale_fetch_not_promoted_after_pointer_advance(stub_categories):
    """A category.changed advancing the pointer during a fetch means the
    stale-revision result is never served as current."""
    from django.core.cache import cache

    from stapel_listings.services import category_schema as cs

    cache.clear()  # start from a cold cache (the suite shares LocMemCache)

    # Warm at revision 1.
    assert len(cs.get_feature_configs("7")) == 2

    # Schema grows and the event announces revision 2 (pointer -> 2).
    stub_categories.append(
        {"id": 9, "slug": "color", "name": "Color", "mandatory": False,
         "config": {"type": "string"}}
    )
    cs.note_changed("7", 2)

    # Next read misses data@2 -> refetches -> sees the grown schema.
    assert len(cs.get_feature_configs("7")) == 3
