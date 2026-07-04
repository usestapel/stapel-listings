"""Dataclass DTOs — the API models of stapel-listings (never ORM instances)."""
from dataclasses import dataclass


@dataclass
class PublishResponse:
    """Result of publishing a listing."""

    published: bool
    listing_id: int
    status: str


@dataclass
class ListingActionResponse:
    """Result of a lifecycle action (archive, complete/sold)."""

    success: bool
    status: str


@dataclass
class DeleteResponse:
    """Result of a (soft) delete."""

    success: bool
    deleted: bool


@dataclass
class MyCountersResponse:
    """Listing counts by tab for the current user."""

    active: int
    archived: int
    drafts: int


@dataclass
class FavoriteToggleResponse:
    """Result of favoriting / unfavoriting a listing."""

    favorited: bool
    listing_id: int
