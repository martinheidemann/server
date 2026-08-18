"""Test how Telmore Musik API payloads are turned into Music Assistant models."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, ItemMapping

from music_assistant.providers.telmore.parsers import parse_track

if TYPE_CHECKING:
    from typing import Any

# What the favorites/playlist queries return: the album is id + title only.
MINIMAL_TRACK = {
    "id": "42",
    "title": "Some Track",
    "duration": 210,
    "artist": {"id": "7", "title": "Some Artist"},
    "album": {"id": "3841864", "title": "Some Album"},
}

# What a single-track catalog lookup returns: the album carries its own artist.
FULL_TRACK = {
    "id": "42",
    "title": "Some Track",
    "duration": 210,
    "artist": {"id": "7", "title": "Some Artist"},
    "album": {
        "id": "3841864",
        "title": "Some Album",
        "artist": {"id": "7", "title": "Some Artist"},
        "type": "REGULAR",
    },
}


async def test_minimal_album_becomes_item_mapping(provider: Any) -> None:
    """A track whose album is id + title must not trigger a catalog lookup.

    Resolving it would cost one request per track, and a track whose album has
    left the catalog would raise and abort the entire library sync.
    """
    provider.get_album = AsyncMock()

    track = await parse_track(provider, MINIMAL_TRACK)

    assert isinstance(track.album, ItemMapping)
    assert track.album.item_id == "3841864"
    assert track.album.name == "Some Album"
    assert track.album.media_type == MediaType.ALBUM
    provider.get_album.assert_not_called()


async def test_full_album_is_parsed(provider: Any) -> None:
    """An album that carries its own artist is parsed into a full Album."""
    provider.get_album = AsyncMock()

    track = await parse_track(provider, FULL_TRACK)

    assert isinstance(track.album, Album)
    assert track.album.name == "Some Album"
    assert [artist.name for artist in track.album.artists] == ["Some Artist"]
    provider.get_album.assert_not_called()


async def test_track_without_album(provider: Any) -> None:
    """A track with no album at all parses without one."""
    track = await parse_track(provider, {k: v for k, v in MINIMAL_TRACK.items() if k != "album"})

    assert track.album is None
    assert track.name == "Some Track"
