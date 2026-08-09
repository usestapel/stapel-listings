"""docs/llms.txt drift gate — the fifth contract artifact (badge-canon §3).

Scope note: this module has no schema/flows/errors triad emitter and no
capabilities.json emitter either — docs/capabilities.json here is
HAND-AUTHORED (see the Makefile `contract` comment; git log: "author
capabilities.json for the stapel-catalog sweep") and is committed by hand,
never regenerated. This test file gates ONLY docs/llms.txt, which renders
deterministically from that committed capabilities.json — no Django, no
subprocess, no regeneration of anything else.

Regenerate after any change to docs/capabilities.json:

    make contract        # or: python -m stapel_tools.llms_txt . --out docs

then commit ``docs/llms.txt``. Without regenerating, the drift gate below
fails.
"""
import json
from pathlib import Path

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
