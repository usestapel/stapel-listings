def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.staticfiles",
                "django.contrib.admin",
                "django.contrib.messages",
                "stapel_core.django.users",
                "rest_framework",
                # stapel_attributes is an L1 library (no Django app) — imported,
                # not installed. Listings depends on its value-validation engine
                # and feature-type registry.
                "stapel_listings",
            ],
            AUTH_USER_MODEL="users.User",
            STATIC_URL="/static/",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            USE_TZ=True,
            ROOT_URLCONF="stapel_listings.tests.urls",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # Synchronous in-process comm with schema validation ON, so the
            # committed contracts in schemas/ are enforced by the tests.
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            STAPEL_COMM={
                "OUTBOX_ENABLED": False,
                "ACTION_TRANSPORT": "inprocess",
                "VALIDATE_SCHEMAS": True,
            },
            MIGRATION_MODULES={
                "users": None,
                "listings": None,
            },
        )
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()

        # autoload_schemas only registers emits/ + functions/. Register the
        # consumes/ contracts too so the round-trip tests (a test emitting an
        # event we subscribe to) are schema-validated against the documented
        # shape, not delivered unchecked.
        import json
        from pathlib import Path

        from stapel_core.comm.registry import action_registry

        consumes = Path(__file__).resolve().parent / "schemas" / "consumes"
        for schema_file in sorted(consumes.glob("*.json")):
            action_registry.register_schema(
                schema_file.stem,
                json.loads(schema_file.read_text(encoding="utf-8")),
            )


import pytest  # noqa: E402


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def capture_events():
    """Factory: ``events = capture_events("listing.published")`` returns a list
    that collects every in-process delivery of that action. Torn down cleanly.
    """
    from stapel_core.comm import action_registry, subscribe_action

    subscribed = []

    def _capture(name):
        events = []

        def handler(event):
            events.append(event)

        subscribe_action(name, handler)
        subscribed.append((name, handler))
        return events

    yield _capture
    for name, handler in subscribed:
        try:
            action_registry._subscribers.get(name, []).remove(handler)
        except (ValueError, AttributeError):
            pass


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create(username="alice", email="alice@example.com")


@pytest.fixture
def other_user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create(username="bob", email="bob@example.com")


# --- comm stub: categories.features ---------------------------------------

# A small, valid two-feature schema: a mandatory int "mileage" (shown in title)
# and an optional single-select "condition" (shown as a badge).
DEFAULT_FEATURE_DEFS = [
    {
        "id": 1,
        "slug": "mileage",
        "name": "Mileage",
        "mandatory": True,
        "show_at_title": True,
        "config": {"type": "int", "min": 0, "max": 1000000, "postfix": "km"},
    },
    {
        "id": 2,
        "slug": "condition",
        "name": "Condition",
        "mandatory": False,
        "show_as_badge": True,
        "config": {
            "type": "select",
            "maxSelected": 1,
            "options": [
                {"value": "new", "label": "cond.new"},
                {"value": "used", "label": "cond.used"},
            ],
        },
    },
]


@pytest.fixture
def stub_categories():
    """Register a stub ``categories.features`` comm Function.

    Yields the mutable feature-def list so a test can reshape the schema; the
    registration is torn down afterwards so tests stay isolated.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    feature_defs = [dict(d) for d in DEFAULT_FEATURE_DEFS]
    revision = {"n": 1}

    def provider(payload):
        return {
            "category_id": payload["category_id"],
            "revision": revision["n"],
            "features": feature_defs,
        }

    register_function("categories.features", provider)
    yield feature_defs
    function_registry._providers.pop("categories.features", None)
    function_registry._schemas.pop("categories.features", None)


@pytest.fixture
def draft_listing(db, user, stub_categories):
    """A DRAFT listing with a valid draft ready to publish."""
    from stapel_listings.models import Listing

    return Listing.objects.create(
        owner=user,
        category_id="7",
        title_draft="Toyota Camry",
        description_draft="A well kept car in great condition.",
        price_draft="15000.00",
        currency="EUR",
        images_draft=["product/abc123"],
        features_draft={
            "mileage": {"type": "int", "value": 42000},
            "condition": {"type": "select", "value": ["used"]},
        },
    )
