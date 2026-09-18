"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* the operations that genuinely cannot be driven in-process are listed by
  name in ``UNDRIVABLE`` with a one-line reason each. That list is asserted
  to be exactly current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails — an empty array validates
  against any item schema, so an empty answer is a check that looked at
  nothing;
* where a second state is cheap, a recipe drives BOTH. Every null finding of
  the first wave of this gate was on the EMPTY state, so "populated only" is
  a coverage number that hides the interesting half. Here that means: a
  listing with photos and one without, a draft and a published one, an
  anonymous reader and the owner, a page with rows and a page with none.

Runs on every interpreter: it reads the committed schema and never emits.

The urlconf below is the EMISSION mount (``codegen_urls.py``):
``listings/api/`` + the module's own ``v1/``, giving the canonical
``/listings/api/v1/…`` prefix the document is written against.
``tests/urls.py`` mounts ``urls_v1`` DIRECTLY under ``listings/``, skipping
both the host's ``api/`` segment and the module's own ``v1/`` — so the whole
existing suite drives ``/listings/listings/…`` and not one path in the
committed document resolves under it. Three of the first four libraries this
gate was written for had exactly that shape; this is the fourth.

What it found on its first run: 20 of 20 operations driven, one red.

* ``GET /listings/{id}/status/`` declared ``ListingStatus`` — six REQUIRED
  properties, ``owner_id`` and ``moderation_status`` among them — and
  answered ``{"is_deleted": …}`` and nothing else to every caller who is
  neither the listing's owner nor the service transport. The route is
  ``AllowAny`` on purpose, so that is the ordinary reader. The narrow body is
  real and deliberate (``ListingStatusPublicSerializer``: the full view was
  an enumeration oracle over other people's drafts and was cut on purpose);
  what was never updated was the annotation on ``ListingViewSet.status``,
  which promised one shape for a route that answers two.

  Closed in 0.23.0: the route declares BOTH bodies as
  ``ListingStatusResponse``, a union discriminated on ``scope``, and both
  bodies carry ``scope`` on the wire (``"owner"`` / ``"public"``) — so a
  generated client reads which body it holds instead of probing for a field
  a stranger's answer will never have. Driven below for all four audiences.
"""
import contextlib
import copy
import json
import re
import uuid
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client.
urlpatterns = [
    url_path("listings/api/", include("stapel_listings.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/listings/api/v1"

#: Matches ``SERVICE_API_KEY`` in the suite's settings (conftest.py), which is
#: what ``ServiceAPIKeyMiddleware`` compares ``X-API-KEY`` against.
SERVICE_KEY = "test-service-key"


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Photo and gallery surfaces write through ``MEDIA_ROOT``, which the
    harness settings never set — so it defaults to the working directory and
    anything written lands in the checkout beside a flat-layout package,
    where a stray directory also shadows a submodule. Pinned per test."""
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _blank_string_alternative(node):
    """A ``oneOf`` branch that means "or the empty string".

    ``category_id`` is a ``CharField(allow_blank=True)`` with a regex, emitted
    as ``oneOf: [{pattern: …, maxLength: 64}, {maxLength: 0}]``. The branches
    are disjoint only if the pattern is read as asserting; JSON Schema is
    happy to let ``""`` fail the pattern and match the second branch, but a
    non-empty value matches only the first — so the exclusive ``oneOf`` is
    fine for values and wrong for ``""`` under a validator that treats the
    pattern as non-asserting for the empty string. Read as alternatives: this
    is a generator idiom colliding with ``oneOf`` exclusivity, not a claim
    the wire breaks.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "string" and b.get("maxLength") == 0
        for b in branches
    )


def _undiscriminated_union(node):
    """A ``oneOf`` whose branches OVERLAP by construction.

    A card feature is emitted as ``oneOf: [allOf[FeatureDao, CardFeatureElement],
    RedactedFeatureDao]``. ``RedactedFeatureDao`` requires ``redacted`` and
    ``present`` but forbids nothing else, and the decorated branch adds no
    ``additionalProperties: false`` either, so a body can satisfy both and an
    exclusive ``oneOf`` rejects what the document plainly describes. The union
    carries no ``discriminator`` to tell them apart — a union that DOES
    (``FeatureDto`` / ``FeatureDao``, whose branches pin ``type`` to disjoint
    single-value enums) is left exclusive, because there an overlap would be
    a real finding.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list) or "discriminator" in node:
        return False
    if _blank_string_alternative(node):
        return False
    return len(branches) > 1


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null. The second
    conversion is the two overlapping-``oneOf`` idioms above. Everything else
    drf-spectacular emits (``$ref``, ``allOf``, ``enum``, ``const``,
    ``required``, ``readOnly``, ``discriminator``) is JSON Schema as written,
    or inert.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if _blank_string_alternative(rebuilt) or _undiscriminated_union(rebuilt):
        rebuilt["anyOf"] = rebuilt.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user(**kwargs):
    User = get_user_model()
    defaults = dict(
        username=_unique("wire_"), email=f"{_unique('wire-')}@example.com"
    )
    defaults.update(kwargs)
    return User.objects.create(**defaults)


def client_for(user=None, **extra):
    client = APIClient(**extra)
    if user is not None:
        client.force_authenticate(user=user)
    return client


def anonymous(**extra):
    return APIClient(**extra)


def service_client():
    """The fleet transport: ``ServiceAPIKeyMiddleware`` marks the request
    service-originated when ``X-API-KEY`` matches the harness setting."""
    return APIClient(HTTP_X_API_KEY=SERVICE_KEY)


#: The category schema this module asks ``categories.features`` for: one
#: mandatory int shown in the title line, one optional single-select badge.
#: Same shape as the suite's ``stub_categories`` fixture — restated here
#: because the recipes are plain functions and cannot request a fixture.
FEATURE_DEFS = [
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

DRAFT_FEATURES = {
    "mileage": {"type": "int", "value": 42000},
    "condition": {"type": "select", "value": ["used"]},
}


@contextlib.contextmanager
def category_features(feature_defs=None):
    """Register the ``categories.features`` comm Function for the duration.

    The module talks to the catalogue BY NAME and never imports it, so
    without a provider every feature-bearing read degrades to "no schema" —
    which is a legitimate state, but not the one a storefront runs in. The
    recipes below drive the answered state; ``no schema`` is reachable by
    simply not entering this block, and ``GET /listings/{id}/`` drives both.
    """
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    defs = [dict(d) for d in (feature_defs if feature_defs is not None else FEATURE_DEFS)]

    # A comm function name has exactly one provider, and these blocks nest
    # (a recipe holds one open while ``publish`` opens its own): the inner
    # entry is a no-op rather than a second registration.
    if "categories.features" in getattr(function_registry, "_providers", {}):
        yield defs
        return

    def provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": defs}

    register_function("categories.features", provider)
    try:
        yield defs
    finally:
        function_registry._providers.pop("categories.features", None)
        function_registry._schemas.pop("categories.features", None)


def make_draft(owner=None, *, with_photos=True, ready=True, **kwargs):
    """A DRAFT listing. ``ready`` means "publishable": the publish rules want
    a title, a description, a price, a photo, a place and every mandatory
    feature (REQUIRE_IMAGE_ON_PUBLISH / REQUIRE_LOCATION_ON_PUBLISH)."""
    from stapel_listings.models import Listing

    owner = owner if owner is not None else make_user()
    fields = dict(owner=owner, category_id="7")
    if ready:
        fields.update(
            title_draft="Toyota Camry",
            description_draft="A well kept car in great condition.",
            price_draft=Decimal("15000.00"),
            currency="EUR",
            lat_draft=Decimal("55.755800"),
            lon_draft=Decimal("37.617300"),
            location_label_draft="A street, a city, a country",
            features_draft=dict(DRAFT_FEATURES),
        )
    fields["images_draft"] = ["product/abc123"] if with_photos else []
    fields.update(kwargs)
    return Listing.objects.create(**fields)


def publish(listing):
    """Take a draft all the way to PUBLISHED.

    ``MODERATION_GATE`` is ``pre`` by default, so publishing parks the row at
    PENDING; the approval is the moderator's half, applied here directly so
    the gate can reach the published state the shop window serves.
    """
    from stapel_listings.services import publish as publish_service

    with category_features():
        publish_service.publish_listing(listing)
    listing.refresh_from_db()
    listing.apply_moderation("approved")
    listing.refresh_from_db()
    return listing


def make_published(owner=None, *, with_photos=True, **kwargs):
    if with_photos:
        return publish(make_draft(owner, with_photos=True, **kwargs))
    return _published_without_photos(owner, **kwargs)


def _published_without_photos(owner=None, **kwargs):
    """A PUBLISHED listing carrying no images at all.

    ``REQUIRE_IMAGE_ON_PUBLISH`` is on by default, so this state cannot be
    reached through ``publish``; it is reachable in a deployment that turns
    the rule off, and it is the state a card's ``images`` claim has to
    survive. Written straight to the row rather than pretending otherwise.
    """
    from stapel_listings.models import Listing, ListingStatus, ModerationStatus

    owner = owner if owner is not None else make_user()
    fields = dict(
        owner=owner,
        category_id="7",
        title="A listing with no photographs",
        description="Sold as seen.",
        price=Decimal("10.00"),
        currency="EUR",
        images=[],
        status=ListingStatus.PUBLISHED,
        moderation_status=ModerationStatus.APPROVED,
    )
    fields.update(kwargs)
    return Listing.objects.create(**fields)


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path, code):
        self.method = method
        self.path = path
        self.code = code

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template)``. Each recipe receives a ``Call``
#: bound to that operation and returns either the response it produced or a
#: list of ``(state name, response)`` pairs — every one of which is validated.
RECIPES = {}


def recipe(method, path):
    def register(fn):
        key = (method, V1 + path)
        assert key not in RECIPES, f"duplicate recipe for {method} {path}"
        RECIPES[key] = fn
        return fn

    return register


#: Operations that cannot be driven in-process, by name and with the reason.
#: EMPTY: every operation this contract declares is reachable from a test
#: client. A short, visible list would be acceptable here; a silent skip is
#: not.
UNDRIVABLE: dict = {}


# ── the shop window ──────────────────────────────────────────────────────────


@recipe("GET", "/listings/")
def _listings_list(call):
    owner = make_user()
    reader = make_user()
    with category_features():
        make_published(owner)
        make_published(owner, with_photos=False)
        return [
            ("signed-in reader, published rows", call(client_for(reader))),
            ("anonymous reader (is_favorited/viewed null)", call(anonymous())),
            (
                "empty page: the window filtered to nothing",
                call(client_for(reader), query="?limit=1&anchor=0"),
            ),
        ]


@recipe("POST", "/listings/")
def _listings_create(call):
    with category_features():
        return [
            ("bare draft (category alone)",
             call(client_for(make_user()), data={"category_id": "7"})),
            (
                "draft opened with content and photos",
                call(
                    client_for(make_user()),
                    data={
                        "category_id": "7",
                        "title_draft": "Toyota Camry",
                        "description_draft": "A well kept car.",
                        "price_draft": "15000.00",
                        "currency": "EUR",
                        "images_draft": ["product/abc123"],
                        "features_draft": dict(DRAFT_FEATURES),
                    },
                ),
            ),
        ]


@recipe("GET", "/listings/{id}/")
def _listing_detail(call):
    owner = make_user()
    reader = make_user()
    with category_features():
        with_photos = make_published(owner)
        without_photos = make_published(owner, with_photos=False)
        answered = [
            ("owner reading their own published listing",
             call(client_for(owner), params={"id": with_photos.pk})),
            ("stranger reading a listing with photos",
             call(client_for(reader), params={"id": with_photos.pk})),
            ("anonymous reader, listing with NO photos",
             call(anonymous(), params={"id": without_photos.pk})),
        ]
    # ...and once more with the catalogue unreachable, which is the state a
    # deployment without stapel-categories permanently sits in.
    unanswered = call(client_for(reader), params={"id": with_photos.pk})
    return answered + [("catalogue unanswered (no categories.features)", unanswered)]


@recipe("GET", "/listings/engagement/")
def _engagement(call):
    owner = make_user()
    reader = make_user()
    with category_features():
        first = make_published(owner)
        second = make_published(owner, with_photos=False)
    return [
        (
            "signed-in reader over two ids",
            call(client_for(reader), query=f"?ids={first.pk},{second.pk}"),
        ),
        (
            "anonymous reader (viewed/is_favorited null)",
            call(anonymous(), query=f"?ids={first.pk}"),
        ),
        ("no ids at all: an empty overlay map", call(client_for(reader), query="?ids=")),
    ]


# ── the seller's cabinet ─────────────────────────────────────────────────────


@recipe("GET", "/listings/my/counters/")
def _my_counters(call):
    owner = make_user()
    fresh = call(client_for(make_user()))
    with category_features():
        make_draft(owner)
        make_published(owner)
    return [
        ("an account that has never sold anything (every count zero)", fresh),
        ("a draft and a published listing", call(client_for(owner))),
    ]


@recipe("GET", "/listings/my/listings/")
def _my_listings(call):
    owner = make_user()
    empty = call(client_for(make_user()))
    with category_features():
        make_draft(owner, with_photos=False)
        make_published(owner)
        return [
            ("empty cabinet", empty),
            ("a draft with no photos and a published listing", call(client_for(owner))),
            ("narrowed to the drafts tab",
             call(client_for(owner), query="?status=draft,rejected")),
        ]


@recipe("GET", "/listings/my/favorites/")
def _my_favorites(call):
    from stapel_listings.models import Favorite

    reader = make_user()
    empty = call(client_for(make_user()))
    with category_features():
        listing = make_published(make_user())
        Favorite.objects.create(user=reader, listing=listing)
        return [
            ("nothing favorited yet", empty),
            ("one favorited listing", call(client_for(reader))),
        ]


@recipe("GET", "/listings/{id}/draft/")
def _draft_readback(call):
    owner = make_user()
    with category_features():
        return [
            ("a draft that has never been touched",
             call(client_for(owner), params={"id": make_draft(owner, ready=False).pk})),
            ("a draft filled in, with a photo",
             call(client_for(owner), params={"id": make_draft(owner).pk})),
        ]


@recipe("POST", "/listings/{id}/save-draft/")
def _save_draft(call):
    owner = make_user()
    with category_features():
        return [
            (
                "a single field written to an untouched draft",
                call(
                    client_for(owner),
                    params={"id": make_draft(owner, ready=False).pk},
                    data={"title_draft": "Nice bike"},
                ),
            ),
            (
                "the whole draft written at once, photos included",
                call(
                    client_for(owner),
                    params={"id": make_draft(owner, ready=False).pk},
                    data={
                        "title_draft": "Toyota Camry",
                        "description_draft": "A well kept car.",
                        "price_draft": "15000.00",
                        "images_draft": ["product/abc123", "product/def456"],
                        "features_draft": dict(DRAFT_FEATURES),
                        "location_label_draft": "A street, a city, a country",
                    },
                ),
            ),
        ]


@recipe("PUT", "/listings/{id}/")
def _listing_put(call):
    owner = make_user()
    with category_features():
        return [
            (
                "a draft replaced with the minimum the write accepts",
                call(
                    client_for(owner),
                    params={"id": make_draft(owner, ready=False).pk},
                    data={"category_id": "7"},
                ),
            ),
            (
                "a draft replaced with content and photos",
                call(
                    client_for(owner),
                    params={"id": make_draft(owner).pk},
                    data={
                        "category_id": "7",
                        "title_draft": "Toyota Camry",
                        "description_draft": "A well kept car.",
                        "price_draft": "15000.00",
                        "images_draft": ["product/abc123"],
                        "features_draft": dict(DRAFT_FEATURES),
                    },
                ),
            ),
        ]


@recipe("PATCH", "/listings/{id}/")
def _listing_patch(call):
    owner = make_user()
    with category_features():
        return [
            (
                "an empty patch on an untouched draft",
                call(
                    client_for(owner),
                    params={"id": make_draft(owner, ready=False).pk},
                    data={},
                ),
            ),
            (
                "one field patched on a filled draft",
                call(
                    client_for(owner),
                    params={"id": make_draft(owner).pk},
                    data={"title_draft": "Toyota Camry 2019"},
                ),
            ),
        ]


@recipe("GET", "/listings/{id}/validate-draft/")
def _validate_draft(call):
    owner = make_user()
    with category_features():
        return [
            ("an untouched draft (every rule unsatisfied)",
             call(client_for(owner), params={"id": make_draft(owner, ready=False).pk})),
            ("a draft that is ready to publish",
             call(client_for(owner), params={"id": make_draft(owner).pk})),
        ]


@recipe("POST", "/listings/{id}/publish/")
def _publish(call):
    owner = make_user()
    with category_features():
        first = call(client_for(owner), params={"id": make_draft(owner).pk})
        # A listing already live and edited again: the endpoint answers
        # `published` while moderation re-reviews it, which is a different
        # `status` on the same declared body.
        live = make_published(owner)
        live.title_draft = "Toyota Camry 2019"
        live.save(update_fields=["title_draft"])
        again = call(client_for(owner), params={"id": live.pk})
    return [
        ("first publication of a fresh draft", first),
        ("re-publication of a live listing", again),
    ]


@recipe("GET", "/listings/{id}/status/")
def _status_probe(call):
    """Driven for all four audiences on purpose: the route answers the full
    body to two of them and the narrow one to the other two, and the union it
    declares has to hold for both halves."""
    owner = make_user()
    with category_features():
        listing = make_published(owner)
        draft = make_draft(owner, ready=False)
    return [
        ("the owner (full status view)", call(client_for(owner), params={"id": listing.pk})),
        ("the service transport", call(service_client(), params={"id": listing.pk})),
        ("a signed-in stranger", call(client_for(make_user()), params={"id": draft.pk})),
        ("an anonymous reader", call(anonymous(), params={"id": draft.pk})),
    ]


# ── lifecycle ────────────────────────────────────────────────────────────────


@recipe("POST", "/listings/{id}/transition/")
def _transition(call):
    owner = make_user()
    with category_features():
        live = make_published(owner)
        paused = make_published(owner)
        paused.transition_to("paused")
        return [
            ("published -> paused",
             call(client_for(owner), params={"id": live.pk}, data={"to": "paused"})),
            ("paused -> published (back on the window)",
             call(client_for(owner), params={"id": paused.pk}, data={"to": "published"})),
        ]


@recipe("POST", "/listings/{id}/archive/")
def _archive(call):
    owner = make_user()
    with category_features():
        return [
            ("archiving a draft",
             call(client_for(owner), params={"id": make_draft(owner, ready=False).pk})),
            ("archiving a published listing",
             call(client_for(owner), params={"id": make_published(owner).pk})),
        ]


@recipe("POST", "/listings/{id}/complete/")
def _complete(call):
    owner = make_user()
    with category_features():
        return call(client_for(owner), params={"id": make_published(owner).pk})


@recipe("DELETE", "/listings/{id}/")
def _destroy(call):
    owner = make_user()
    with category_features():
        return [
            ("deleting an untouched draft",
             call(client_for(owner), params={"id": make_draft(owner, ready=False).pk})),
            ("deleting a filled draft with photos",
             call(client_for(owner), params={"id": make_draft(owner).pk})),
        ]


# ── favorites ────────────────────────────────────────────────────────────────


@recipe("POST", "/listings/{id}/favorite/")
def _favorite(call):
    reader = make_user()
    with category_features():
        listing = make_published(make_user())
        first = call(client_for(reader), params={"id": listing.pk})
        again = call(client_for(reader), params={"id": listing.pk})
    return [
        ("favorited for the first time", first),
        ("favorited again (already there)", again),
    ]


@recipe("POST", "/listings/{id}/unfavorite/")
def _unfavorite(call):
    from stapel_listings.models import Favorite

    reader = make_user()
    with category_features():
        listing = make_published(make_user())
        Favorite.objects.create(user=reader, listing=listing)
        removed = call(client_for(reader), params={"id": listing.pk})
        never = call(client_for(make_user()), params={"id": listing.pk})
    return [
        ("a favorite removed", removed),
        ("nothing to remove (never favorited)", never),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send, with the defect and
#: its owner. ``strict=True``: a fixed entry fails until it is deleted, so a
#: finding can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES: dict[tuple[str, str], str] = {}


def test_the_status_probe_says_which_of_its_two_bodies_it_sent():
    """The discriminator is a wire value, not a schema decoration.

    ``ListingStatusResponse`` is a union keyed on ``scope``; the operation
    check above proves each answer matches ONE branch, and this proves the
    label on the answer is the branch it actually is. Without it a body could
    satisfy the union while telling the client the wrong half, and a client
    that switches on ``body.scope`` would read the wrong fields.
    """
    owner = make_user()
    with category_features():
        listing = make_published(owner)
        draft = make_draft(owner, ready=False)

    def probe(client, pk):
        response = client.get(f"{V1}/listings/{pk}/status/")
        assert response.status_code == 200, response.content[:400]
        return response.json()

    full = probe(client_for(owner), listing.pk)
    assert full["scope"] == "owner"
    assert full["owner_id"] and full["moderation_status"]

    service = probe(service_client(), listing.pk)
    assert service["scope"] == "owner"

    for who, client in (
        ("a signed-in stranger", client_for(make_user())),
        ("an anonymous reader", anonymous()),
    ):
        narrow = probe(client, draft.pk)
        assert narrow == {"scope": "public", "is_deleted": False}, who


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Three of the first four libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, and one mounted
    less than the emission did. This module is a fourth: ``tests/urls.py``
    mounts ``urls_v1`` straight under ``listings/``, skipping both the host's
    ``api/`` segment and the module's own ``v1/``, so the whole existing
    suite drives ``/listings/listings/…`` and no path in the committed
    document resolves under it.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the contract
    says it goes. A missing recipe already fails loudly; this fails when the
    MOUNT is wrong, which no per-operation check can see, because when the
    mount is wrong every operation is equally and silently unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and a urlconf may use
    # several converters — uuid, int, slug. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    covered = set(RECIPES) | set(UNDRIVABLE)

    missing = sorted(declared - covered)
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - declared)
    assert not stale, (
        "recipes/exclusions for operations the contract no longer declares:\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )
    both = sorted(set(RECIPES) & set(UNDRIVABLE))
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


def _states(produced):
    """A recipe answers with one response, or with ``(state, response)`` pairs."""
    if isinstance(produced, list):
        return produced
    return [("the only state", produced)]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    perform = RECIPES.get((method, path))
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    validator = _validator(body_schema)
    for state, response in _states(perform(Call(method, path, code))):
        assert response.status_code == code, (
            f"{method} {path} [{state}]: expected the declared {code}, got "
            f"{response.status_code}: {response.content[:400]}"
        )

        body = response.json()
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
        assert not errors, (
            f"{method} {path} [{state}] answers a body the contract does not "
            "describe:\n"
            + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
            + f"\n  body: {json.dumps(body)[:600]}"
        )
        # An empty collection validates against any item schema, so a
        # collection response must actually carry a row for the check to have
        # looked at anything — except where the recipe named the state EMPTY
        # on purpose, which is the state the first wave's null findings all
        # lived in.
        if not any(word in state for word in ("empty", "no ids", "nothing", "never")):
            if isinstance(body, list):
                assert body, f"{method} {path} [{state}]: the declared list came back empty"
            if isinstance(body, dict) and isinstance(body.get("items"), list):
                assert body["items"], (
                    f"{method} {path} [{state}]: the declared page came back empty"
                )
