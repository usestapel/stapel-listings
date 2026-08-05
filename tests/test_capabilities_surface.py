"""Drift gate for the `surface` section of ``docs/capabilities.json``.

The IsNotAnonymousUser incident is why this section exists at all: a product
bolted on its own DRF permission gate, unaware the framework already shipped
one, because the module's contract document could describe what you may
switch on (``axes``) and what you may replace (``extension_points``) and had
no way at all to name a symbol you are supposed to CALL
(discoverability-design.md §1.2).

``surface`` names them, with one curated line each saying when to reach for
them. The entry set is derived by AST from the roots in
``docs/capabilities.meta.json`` — a new public function in a declared root
shows up here by itself and fails emission until somebody explains it.

Honest boundary: the REST of this module's ``capabilities.json`` is still
hand-written (no gate registry, no ``docs/schema.json``), so only
``module``/``version``/``surface`` are gated below.
"""
import json
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401  (probe: the emitter must be importable)
except ImportError as exc:  # pragma: no cover - environment failure, not a branch
    # NOT pytest.importorskip. A drift gate that skips when its emitter is
    # missing reports `1 skipped`, exits 0, and disappears among a hundred
    # green tests — exactly how a surface entry could go unexplained with
    # nothing red anywhere to say so. A gate that cannot run has FAILED; it
    # has not passed.
    raise RuntimeError(
        "capabilities surface drift gate cannot run: stapel-tools is not "
        "importable, and it carries the capabilities emitter this gate "
        "measures drift against. Install it (workspace venv, or `pip install "
        "stapel-tools`) and re-run. This is a hard failure on purpose — a "
        "skipped drift gate is silently no gate."
    ) from exc

from stapel_tools.surface import _stable_json, load_meta, patch_capabilities  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "docs" / "capabilities.json"

SURFACE_ROOTS = (
    "models.py",
    "services/publish.py",
    "services/category_schema.py",
)


def _emitted() -> dict:
    try:
        return patch_capabilities(REPO, load_meta(REPO))
    except SystemExit as exc:  # the LOUD rule — report it, don't bury it
        pytest.fail(f"capabilities emission refused: {exc}", pytrace=False)


def test_no_drift():
    assert COMMITTED.read_text() == _stable_json(_emitted()), (
        "docs/capabilities.json is stale — run `make contract` and commit it"
    )


def test_version_tracks_pyproject():
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert json.loads(COMMITTED.read_text())["version"] == (
        pyproject["project"]["version"]
    )


def test_every_surface_entry_is_named_and_explained():
    surface = json.loads(COMMITTED.read_text())["surface"]
    assert surface, "expected at least one surface entry"
    for entry in surface:
        assert entry["intent"].strip(), entry
        assert entry["kind"], entry


def test_a_new_public_symbol_cannot_slip_in_unexplained():
    """The set is derived, so the gate is not "did somebody remember to list
    it" but "does every public function in the declared roots have a line"."""
    from stapel_tools.surface import scan_functions

    declared = {e["name"] for e in json.loads(COMMITTED.read_text())["surface"]}
    for module in SURFACE_ROOTS:
        assert set(scan_functions(REPO / module)) <= declared
