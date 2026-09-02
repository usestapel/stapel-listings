"""What a card says about where a listing is.

`location_label` used to be a writable draft field and nothing more: the
composer's map picker posted the geocoder's `formatted` line into it and the
card printed whatever arrived. Two things were wrong with that at once.

It was the wrong STRING. `formatted` is built for a picker's confirmation
line — POI, then street and house number, then city — because a person
confirming a pin needs to recognize their own doorway. A buyer scanning a
grid of cards needs the opposite: the place, so that twenty rows can be
compared at a glance. The stand printed «ул. Тверская, д. 7, корп. 2,
Москва, Россия» on every card, and no two rows could be told apart by it.

And it was the wrong OWNER. A client-supplied display string on a public
card is a free advertising slot: nothing stopped a seller from posting a
phone number there.

So the published label is DERIVED here, from the coordinates, through
`geo.reverse_geocode` — the Function whose own schema says it is "what a
listings backend calls to stamp a human address onto a row it only has
coordinates for". The draft twin stays writable and stays the picker's
line; it is simply not what the card reads.

Fail-soft, deliberately: the geocoder is a network dependency and
publication is not. A dark provider leaves the client-supplied string in
place — the pre-0.16 behaviour, no worse — instead of making a listing
unpublishable, or blanking the one location line a card has.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def place_label(address) -> str:
    """«City, District» from a geocoder's address components, or ``""``.

    ``county`` backs up ``city`` because photon answers it for a settlement
    that is not inside a city, and a card that printed nothing there would
    be less useful than one naming a district.
    """
    if not isinstance(address, dict):
        return ""
    locality = str(address.get("city") or address.get("county") or "").strip()
    district = str(address.get("district") or "").strip()
    if locality and district:
        return f"{locality}, {district}"
    return locality or district


def resolve_place_label(lat, lon) -> str:
    """The place at *lat*/*lon*, or ``""`` when nothing can be resolved.

    Never raises. Every failure mode of the seam — no provider registered,
    a provider that errors, an answer in an unexpected shape — is the same
    answer here, because the caller's response to all of them is the same:
    keep what it had.
    """
    from ..conf import listings_settings

    if lat is None or lon is None:
        return ""
    name = listings_settings.GEO_REVERSE_FUNCTION
    if not name:
        return ""

    from stapel_core.comm import call

    try:
        answer = call(name, {"lat": float(lat), "lon": float(lon)})
    except Exception as exc:  # noqa: BLE001 - a dark geocoder must not block a publish
        logger.warning("%s unavailable, keeping the supplied label: %s", name, exc)
        return ""
    if not isinstance(answer, dict):
        logger.warning("%s answered a non-mapping; keeping the supplied label", name)
        return ""
    return place_label(answer.get("address"))


def has_place(listing) -> bool:
    """Whether *listing*'s draft carries a usable location.

    The predicate is COORDINATES, not the label. Coordinates are what the
    radius filter, the geohash and the map need; the label is a string a
    client sends, so a listing carrying only the string would satisfy a
    label check and still be invisible on every geographic surface.
    """
    return listing.lat_draft is not None and listing.lon_draft is not None


__all__ = ["has_place", "place_label", "resolve_place_label"]
