"""The committed contract artifacts must describe the API that ships.

``make contract-check`` re-emits docs/{schema,flows,errors}.json via
``stapel_listings._codegen`` and diffs them, but it needs the pinned Python
3.12 interpreter plus stapel-tools, so it is a dev-loop and release gate
rather than something a wider CI matrix can run unpinned. The tests below are
the part that runs everywhere: they read the COMMITTED artifacts and assert
the two properties a stale artifact silently breaks — every route this
module mounts is described, and every error key it can return is declared
(A1, darom-storefront-design.md §3.10 — the contract triad the react codegen
pipeline, ``gen:api``/``gen:errors``/``gen:manifest``, stands on).

docs/capabilities.json remains HAND-AUTHORED for provides/axes/
extension_points/requires (see the Makefile `contract` comment; git log:
"author capabilities.json for the stapel-catalog sweep") — only its
`surface` section is derived. docs/llms.txt and README.md ARE fully
generated and gated below, same as before.
"""
import json
import re
from pathlib import Path

import pytest

from stapel_tools.llms_txt import render

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"


def _inputs() -> dict:
    data = {"capabilities": json.loads((DOCS / "capabilities.json").read_text())}
    for key, name in (
        ("schema", "schema.json"),
        ("errors", "errors.json"),
        ("flows", "flows.json"),
    ):
        path = DOCS / name
        data[key] = json.loads(path.read_text()) if path.is_file() else None
    return data


@pytest.fixture(scope="module")
def errors_artifact():
    return {entry["code"]: entry for entry in json.loads((DOCS / "errors.json").read_text())}


@pytest.fixture(scope="module")
def schema_artifact():
    return json.loads((DOCS / "schema.json").read_text())


# ── errors.json ──────────────────────────────────────────────────────


def test_listings_owned_keys_are_declared(errors_artifact):
    from stapel_listings.errors import STAPEL_LISTINGS_ERRORS

    for code, text in STAPEL_LISTINGS_ERRORS.items():
        assert code in errors_artifact, f"{code} missing from docs/errors.json"
        assert errors_artifact[code]["owner"] == "stapel_listings"
        assert errors_artifact[code]["en"] == text


def test_the_attributes_validation_family_is_declared(errors_artifact):
    """The draft/publish path reaches stapel-attributes' validation codes too.

    Owned by (and translated by) stapel-attributes: this module only forces
    the registration (``import stapel_attributes.errors`` in errors.py) so
    the keys land wherever listings is mounted. Asserting the OWNER too is the
    half that keeps this honest — copying the strings into
    ``STAPEL_LISTINGS_ERRORS`` would take on a catalogue obligation that is
    upstream's.
    """
    from stapel_attributes.errors import ATTRIBUTES_ERRORS

    assert ATTRIBUTES_ERRORS, "the attributes registry came back empty"
    for code in ATTRIBUTES_ERRORS:
        assert code in errors_artifact, f"{code} missing from docs/errors.json"
        assert errors_artifact[code]["owner"] == "stapel_attributes"


def test_the_error_keys_stay_listings_owned():
    """This module's own catalogue must not claim upstream's keys as its own."""
    from stapel_attributes.errors import ATTRIBUTES_ERRORS
    from stapel_listings.errors import STAPEL_LISTINGS_ERRORS

    assert not set(STAPEL_LISTINGS_ERRORS) & set(ATTRIBUTES_ERRORS)


# ── schema.json ──────────────────────────────────────────────────────


def test_every_mounted_route_is_described(schema_artifact):
    """A route added without regenerating the triad fails here.

    The gap this closes is not hypothetical: the pair reads ``schema.json``
    to generate its typed client, so an endpoint missing from the artifact is
    an endpoint the frontend cannot call.
    """
    from stapel_listings import urls_v1

    described = set(schema_artifact["paths"])
    seen_names = set()
    for pattern in urls_v1.urlpatterns:
        name = getattr(pattern, "name", None)
        # DefaultRouter also emits the format-suffix twin of every route
        # (`listings.json`) and the browsable api-root — neither is a real
        # product endpoint.
        if name is None or name in seen_names or name == "api-root":
            continue
        seen_names.add(name)
        route = str(pattern.pattern)
        if "format" in route:
            continue
        # `(?P<pk>[^/.]+)` -> `{id}` (the model's pk field is `id`);
        # any other named group -> `{name}` verbatim.
        route = re.sub(r"\(\?P<pk>[^)]+\)", "{id}", route)
        route = re.sub(r"\(\?P<([a-zA-Z_]+)>[^)]+\)", r"{\1}", route)
        route = route.strip("^$")
        assert f"/listings/api/v1/{route}" in described, (
            f"{route} is mounted but absent from docs/schema.json — "
            "run 'make contract' and commit the artifacts"
        )


def test_llms_txt_committed():
    assert (DOCS / "llms.txt").is_file(), "missing docs/llms.txt — run `make contract`"


def test_llms_txt_has_no_drift():
    """Re-render in-process from the committed capabilities.json; must match byte-for-byte."""
    committed = (DOCS / "llms.txt").read_text()
    regenerated = render(_inputs())
    assert committed == regenerated, (
        "docs/llms.txt drifted — run `make contract` and commit docs/llms.txt"
    )


def test_llms_txt_emission_is_deterministic():
    """Two independent emissions from the same inputs are byte-identical."""
    inputs = _inputs()
    assert render(inputs) == render(inputs)


# --- README.md — the sixth artifact (tracker #257) ---------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what this module is and how to think about it) plus the contract
# documents above (badges, version, surface counts, doc links). Everything a
# hand-written README used to restate — and therefore used to get wrong one
# release later — is generated here and gated below.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs, render, static_languages

    inputs = load_inputs(REPO)
    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render(REPO, inputs, "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published."""
    import tomllib

    from stapel_tools.readme import load_inputs, resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(load_inputs(REPO)) == pyproject["project"]["version"]
