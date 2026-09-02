"""System checks: configuration the module refuses to guess about."""
import pytest

pytestmark = pytest.mark.django_db


def test_valid_gate_values_are_silent(settings):
    from stapel_listings.checks import check_moderation_gate

    for gate in ("pre", "post"):
        settings.STAPEL_LISTINGS = {"MODERATION_GATE": gate}
        assert check_moderation_gate(None) == []


def test_default_gate_is_silent(settings):
    from stapel_listings.checks import check_moderation_gate

    settings.STAPEL_LISTINGS = {}
    assert check_moderation_gate(None) == []


def test_invalid_gate_is_an_error(settings):
    """E001 — an unknown gate silently behaves as "pre", which is exactly the
    failure this key exists to end (a stand that believes it turned
    post-moderation on, still holding every listing in PENDING)."""
    from stapel_listings.checks import check_moderation_gate

    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "psot"}
    findings = check_moderation_gate(None)
    assert [f.id for f in findings] == ["stapel_listings.E001"]
    assert "psot" in findings[0].msg


# --- W001: view deduplication lives in the cache ---------------------------


def test_a_shared_cache_is_silent(settings):
    from stapel_listings.checks import check_view_dedup_cache

    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache"}
    }
    assert check_view_dedup_cache(None) == []


def test_a_per_process_cache_is_named(settings):
    """The failure it warns about is the silent kind: the counter still rises,
    the endpoint still answers 200, and the number is a multiple of the truth
    because each worker holds its own deduplication window."""
    from stapel_listings.checks import check_view_dedup_cache

    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    warnings = check_view_dedup_cache(None)
    assert [w.id for w in warnings] == ["stapel_listings.W001"]
