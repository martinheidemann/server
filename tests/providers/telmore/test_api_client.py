"""Test the Telmore Musik GraphQL result helpers."""

from __future__ import annotations

from music_assistant.providers.telmore.api_client import graphql_path


def test_walks_nested_keys() -> None:
    """A present path returns the value at its end."""
    result = {"data": {"catalog": {"album": {"id": "1"}}}}
    assert graphql_path(result, "data", "catalog", "album") == {"id": "1"}


def test_explicit_null_midway_returns_none() -> None:
    """
    A GraphQL null must not raise.

    This is how the API reports an item that has left the catalog, and
    ``result.get("data", {}).get("catalog", {})`` would hand back None here -
    the default only applies when the key is absent, not when it holds null.
    """
    assert graphql_path({"data": {"catalog": None}}, "data", "catalog", "album") is None


def test_missing_key_returns_none() -> None:
    """An absent key is indistinguishable from a null for callers."""
    assert graphql_path({"data": {}}, "data", "catalog", "album") is None


def test_none_result_returns_none() -> None:
    """No result at all is tolerated."""
    assert graphql_path(None, "data", "catalog") is None


def test_non_dict_midway_returns_none() -> None:
    """A scalar where an object was expected does not raise."""
    assert graphql_path({"data": "oops"}, "data", "catalog") is None


def test_falsy_leaf_is_preserved() -> None:
    """A legitimate False leaf is returned as-is, not swallowed."""
    result = {"data": {"reportPlayback": {"ok": False}}}
    assert graphql_path(result, "data", "reportPlayback", "ok") is False
