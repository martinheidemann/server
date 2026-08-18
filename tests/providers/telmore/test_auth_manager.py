"""Test the Telmore Musik login and token refresh flow.

Telmore runs the 24-7 Entertainment platform in `OauthEmbedded` mode, which differs
from the `OauthNavigation` mode the sibling yousee provider implements: the identity
provider takes the credentials as JSON, and the callback page hands the tokens to its
opener via `postMessage` instead of writing them to `localStorage`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.telmore.auth_manager import (
    TelmoreAccessToken,
    TelmoreAuthManager,
)

if TYPE_CHECKING:
    from typing import Any

SESSION_BLOB = "abc+def/ghi=="
LOCATION = "https://id.telmore.dk/?session=abc%2Bdef%2Fghi%3D%3D"
CALLBACK_URL = "https://musik.telmore.dk/delegatedloginresponse?code=xyz"

# The callback page posts the tokens to its opener; this mirrors its real shape.
CALLBACK_BODY = """
<script>
    let recipient = window.opener || window.parent;
    recipient.postMessage({
        type: "tokens",
        accessToken: "Issuer=TDC&ExpiresOn=4102444800&portalId=1387",
        refreshToken: "7c286cf821d240ae873889de107865b5"
    }, "*");
</script>
"""


def _response(*, status: int = 200, headers: dict[str, str] | None = None,
              json_body: Any = None, text_body: str = "") -> Mock:
    """Build an async context manager that mimics an aiohttp response."""
    response = Mock()
    response.status = status
    response.headers = headers or {}
    response.json = AsyncMock(return_value=json_body)
    response.text = AsyncMock(return_value=text_body)
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


@pytest.fixture
def auth(provider: Any) -> TelmoreAuthManager:
    """Return an auth manager whose provider serves fixed credentials."""
    provider.get_setup_value = Mock(
        side_effect=lambda key: {"username": "user@example.com", "password": "hunter2"}[key]
    )
    return TelmoreAuthManager(provider)


class TestTelmoreAccessToken:
    """The access token is an &-separated key=value string carrying its own expiry."""

    def test_reads_expiry_from_token(self) -> None:
        """A token expiring in the future is not considered expired."""
        token = TelmoreAccessToken(f"Issuer=TDC&ExpiresOn={int(time.time()) + 3600}")
        assert token.is_expired() is False

    def test_expired_token(self) -> None:
        """A token whose ExpiresOn has passed is expired."""
        token = TelmoreAccessToken(f"Issuer=TDC&ExpiresOn={int(time.time()) - 1}")
        assert token.is_expired() is True

    def test_token_without_expiry_counts_as_expired(self) -> None:
        """A token that carries no ExpiresOn cannot be trusted."""
        assert TelmoreAccessToken("Issuer=TDC").is_expired() is True


async def test_full_login_flow(auth: TelmoreAuthManager) -> None:
    """Delegated login, credential exchange and token extraction hang together."""
    auth.mass.http_session.get = Mock(
        side_effect=[
            _response(status=302, headers={"Location": LOCATION}),
            _response(text_body=CALLBACK_BODY),
        ]
    )
    auth.mass.http_session.post = Mock(return_value=_response(json_body={"url": CALLBACK_URL}))

    token = await auth.auth_token()

    assert token is not None
    assert str(token) == "Issuer=TDC&ExpiresOn=4102444800&portalId=1387"
    assert auth._refresh_token == "7c286cf821d240ae873889de107865b5"

    # The credentials go to the identity provider as JSON, together with the
    # url-decoded session blob handed out by the delegated login redirect.
    _, kwargs = auth.mass.http_session.post.call_args
    assert kwargs["json"] == {
        "session": SESSION_BLOB,
        "username": "user@example.com",
        "password": "hunter2",
    }

    # The second GET must follow the url the identity provider handed back.
    assert auth.mass.http_session.get.call_args_list[1].args[0] == CALLBACK_URL


async def test_bad_credentials_raise_login_failed(auth: TelmoreAuthManager) -> None:
    """The identity provider answers 400 when username or password is wrong."""
    auth.mass.http_session.get = Mock(
        return_value=_response(status=302, headers={"Location": LOCATION})
    )
    auth.mass.http_session.post = Mock(return_value=_response(status=400))

    with pytest.raises(LoginFailed):
        await auth.auth_token()


async def test_missing_session_raises_login_failed(auth: TelmoreAuthManager) -> None:
    """A delegated login that hands out no session blob cannot be completed."""
    auth.mass.http_session.get = Mock(return_value=_response(status=302, headers={}))

    with pytest.raises(LoginFailed):
        await auth.auth_token()


async def test_cached_token_is_reused(auth: TelmoreAuthManager) -> None:
    """A token that has not expired is returned without any http traffic."""
    auth._access_token = TelmoreAccessToken(f"Issuer=TDC&ExpiresOn={int(time.time()) + 3600}")
    auth.mass.http_session.get = Mock()
    auth.mass.http_session.post = Mock()

    assert await auth.auth_token() is auth._access_token
    auth.mass.http_session.get.assert_not_called()
    auth.mass.http_session.post.assert_not_called()


async def test_refresh_uses_json_body(auth: TelmoreAuthManager) -> None:
    """Telmore's token endpoint rejects form-encoding, so the body must be JSON."""
    auth._refresh_token = "old-refresh"
    auth.mass.http_session.post = Mock(
        return_value=_response(
            json_body={
                "status": 0,
                "tokenResult": {
                    "access_token": "Issuer=TDC&ExpiresOn=4102444800",
                    "refresh_token": "new-refresh",
                },
            }
        )
    )

    token = await auth.auth_token()

    assert token is not None
    assert auth._refresh_token == "new-refresh"
    _, kwargs = auth.mass.http_session.post.call_args
    assert kwargs["json"] == {"refresh_token": "old-refresh"}
    assert "data" not in kwargs


async def test_rejected_refresh_falls_back_to_full_login(auth: TelmoreAuthManager) -> None:
    """A refresh token the backend no longer accepts triggers a full login."""
    auth._refresh_token = "stale-refresh"
    auth.mass.http_session.get = Mock(
        side_effect=[
            _response(status=302, headers={"Location": LOCATION}),
            _response(text_body=CALLBACK_BODY),
        ]
    )
    auth.mass.http_session.post = Mock(
        side_effect=[
            _response(json_body={"status": 4}),
            _response(json_body={"url": CALLBACK_URL}),
        ]
    )

    token = await auth.auth_token()

    assert token is not None
    assert auth._refresh_token == "7c286cf821d240ae873889de107865b5"


async def test_invalidate_forces_new_token(auth: TelmoreAuthManager) -> None:
    """Invalidating drops the cached token so the next call authenticates again."""
    auth._access_token = TelmoreAccessToken(f"Issuer=TDC&ExpiresOn={int(time.time()) + 3600}")
    auth.invalidate()
    assert auth._access_token is None
