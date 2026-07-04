"""Feature projection builders (features / title / badges / search)."""
from stapel_listings.services.features import (
    build_features_list,
    build_features_search,
    filter_empty_headers,
    get_consecutive_header_pairs,
)


def test_empty_header_is_filtered():
    daos = {
        "sec_a": {"type": "header", "order": 0},
        "mileage": {"type": "int", "value": 1, "order": 1},
        "sec_b": {"type": "header", "order": 2},  # trailing, empty -> dropped
    }
    result = build_features_list(daos, set())
    slugs = [d["slug"] for d in result]
    assert slugs == ["sec_a", "mileage"]


def test_intentional_consecutive_headers_kept():
    features = [
        {"slug": "h1", "config": {"type": "header"}},
        {"slug": "h2", "config": {"type": "header"}},
        {"slug": "x", "config": {"type": "int"}},
    ]
    pairs = get_consecutive_header_pairs(features)
    assert ("h1", "h2") in pairs

    daos = [
        {"slug": "h1", "type": "header"},
        {"slug": "h2", "type": "header"},
        {"slug": "x", "type": "int", "value": 5},
    ]
    kept = [d["slug"] for d in filter_empty_headers(daos, pairs)]
    assert kept == ["h1", "h2", "x"]


def test_search_extraction_across_types():
    daos = {
        "count": {"type": "int", "value": 0},          # falsy-but-valid kept
        "flag": {"type": "bool", "value": False},      # falsy-but-valid kept
        "name": {"type": "string", "value": ""},       # empty string dropped
        "when": {"type": "date", "value": "2026-01-01"},
        "tags": {"type": "select", "value": ["a", "b"]},
        "path": {"type": "hierarchical_select", "value": ["root", "leaf"]},
        "color": {"type": "hex_color", "simple": "red", "hex": "#FF0000"},
        "custom": {"type": "weird_type", "value": 7},  # unknown -> fallback
        "sect": {"type": "header"},                    # headers excluded
    }
    search = build_features_search(daos)
    assert search["count"] == [0]
    assert search["flag"] == [False]
    assert "name" not in search
    assert search["when"] == ["2026-01-01"]
    assert search["tags"] == ["a", "b"]
    assert search["path"] == ["root", "leaf"]
    assert search["color"] == ["red", "#FF0000"]
    assert search["custom"] == [7]
    assert "sect" not in search
