"""Enum components in docs/schema.json are named, and follow the models.

drf-spectacular names a choice set after its property; when one property
name carries several sets it falls back to collision names (``StatusD41Enum``,
``TypeE3eEnum``) that change with unrelated edits and generator versions,
renaming the react pair's generated types — and the pair reads the listing
status type by name. The harness pins them
(``_codegen_settings.enum_name_overrides``).
"""
import json
import re
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "schema.json"
COLLISION = re.compile(r"[0-9A-F][0-9a-f]{2}Enum$")


def _schemas():
    return json.loads(SCHEMA.read_text())["components"]["schemas"]


def test_no_enum_component_carries_a_collision_name():
    assert [name for name in _schemas() if COLLISION.search(name)
            and not name.endswith("TypeEnum")] == []


def test_the_listing_status_set_has_one_name_and_is_the_model_choices():
    from stapel_listings.models import ListingStatus, ModerationStatus

    schemas = _schemas()
    assert schemas["ListingStatusEnum"]["enum"] == list(ListingStatus.values)
    assert schemas["ModerationStatusEnum"]["enum"] == list(ModerationStatus.values)
    refs = json.dumps(schemas)
    # `status` and the transition's `to` share the one component.
    assert "ToEnum" not in schemas
    assert refs.count('"#/components/schemas/ListingStatusEnum"') >= 2


def test_the_type_discriminators_are_named_after_their_slugs():
    assert _schemas()["SelectTypeEnum"]["enum"] == ["select"]
