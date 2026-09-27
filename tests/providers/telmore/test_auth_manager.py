"""
Test the Telmore Musik login and token refresh flow.

Telmore runs the 24-7 Entertainment platform in `OauthEmbedded` mode, which differs
from the `OauthNavigation` mode YouSee uses: the identity provider takes the
credentials as JSON, and the callback page hands the tokens to its opener via
`postMessage` instead of writing them to `localStorage`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.telmore.auth_manager import TelmoreAuthManager

if TYPE_CHECKING:
    from typing import Any

SESSION_BLOB = "abc+def/ghi=="
LOCATION = "https://id.telmore.dk/?session=abc%2Bdef%2Fghi%3D%3D"
CALLBACK_URL = "https://musik.telmore.dk/delegatedloginresponse?code=xyz"
ACCESS_TOKEN = "Issuer=TDC&ExpiresOn=4102444800&portalId=1387"
REFRESH_TOKEN = "7c286cf821d240ae873889de107865b5"

# The callback page posts the tokens to its opener; this mirrors its real shape.
CALLBACK_BODY = f"""
<script>
    let recipient = window.opener || window.parent;
    recipient.postMessage({{
        type: "tokens",
        accessToken: "{ACCESS_TOKEN}",
        refreshToken: "{REFRESH_TOKEN}"
    }}, "*");
</script>
"""


def _response(
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    text_body: str = "",
) -> Mock:
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


def _login_responses(session: Mock) -> None:
    """Serve a successful delegated login, credential exchange and callback page."""
    session.get = Mock(
        side_effect=[
            _response(status=302, headers={"Location": LOCATION}),
            _response(text_body=CALLBACK_BODY),
        ]
    )
    session.post = Mock(return_value=_response(json_body={"url": CALLBACK_URL}))


@pytest.fixture
def session() -> Mock:
    """Return the fake aiohttp session the auth manager talks to."""
    return Mock()


@pytest.fixture
def auth(session: Mock) -> TelmoreAuthManager:
    """Return an auth manager whose provider serves fixed credentials."""
    provider = Mock()
    provider.logger = logging.getLogger(__name__)
    provider.mass.http_session = session
    provider.get_setup_value = Mock(
        side_effect=lambda key: {"username": "user@example.com", "password": "hunter2"}[key]
    )
    return TelmoreAuthManager(provider)


async def test_full_login_flow(auth: TelmoreAuthManager, session: Mock) -> None:
    """Delegated login, credential exchange and token extraction hang together."""
    _login_responses(session)

    token = await auth.auth_token()

    assert str(token) == ACCESS_TOKEN
    assert auth._refresh_token == REFRESH_TOKEN

    # The credentials go to the identity provider as JSON, together with the
    # url-decoded session blob handed out by the delegated login redirect.
    _, kwargs = session.post.call_args
    assert kwargs["json"] == {
        "session": SESSION_BLOB,
        "username": "user@example.com",
        "password": "hunter2",
    }

    # The second GET must follow the url the identity provider handed back.
    assert session.get.call_args_list[1].args[0] == CALLBACK_URL


@pytest.mark.parametrize(
    ("delegate", "login", "callback", "reason"),
    [
        pytest.param(
            _response(status=302, headers={}),
            None,
            None,
            "did not hand out a login session",
            id="no-session",
        ),
        pytest.param(
            _response(status=302, headers={"Location": LOCATION}),
            _response(status=400),
            None,
            "Invalid Telmore username or password",
            id="bad-credentials",
        ),
        pytest.param(
            _response(status=302, headers={"Location": LOCATION}),
            _response(status=503),
            None,
            "failed with status 503",
            id="login-error-status",
        ),
        pytest.param(
            _response(status=302, headers={"Location": LOCATION}),
            _response(json_body={}),
            None,
            "did not return a callback url",
            id="no-callback-url",
        ),
        pytest.param(
            _response(status=302, headers={"Location": LOCATION}),
            _response(json_body={"url": CALLBACK_URL}),
            _response(text_body="<html></html>"),
            "did not contain the tokens",
            id="no-tokens-in-callback",
        ),
    ],
)
async def test_failed_login_step_raises_with_reason(
    auth: TelmoreAuthManager,
    session: Mock,
    delegate: Mock,
    login: Mock | None,
    callback: Mock | None,
    reason: str,
) -> None:
    """
    Every failed login step raises LoginFailed saying which step failed.

    Returning None instead left the caller to send "Bearer None" once a token had
    expired, ending in an authentication error that pointed nowhere useful.
    """
    session.get = Mock(side_effect=[delegate, callback])
    session.post = Mock(return_value=login)

    with pytest.raises(LoginFailed, match=reason):
        await auth.auth_token()


async def test_refresh_uses_json_body(auth: TelmoreAuthManager, session: Mock) -> None:
    """Telmore's token endpoint rejects form-encoding, so the body must be JSON."""
    auth._refresh_token = "old-refresh"
    session.post = Mock(
        return_value=_response(
            json_body={
                "status": 0,
                "tokenResult": {"access_token": ACCESS_TOKEN, "refresh_token": "new-refresh"},
            }
        )
    )

    token = await auth.auth_token()

    assert str(token) == ACCESS_TOKEN
    assert auth._refresh_token == "new-refresh"
    _, kwargs = session.post.call_args
    assert kwargs["json"] == {"refresh_token": "old-refresh"}
    assert "data" not in kwargs


@pytest.mark.parametrize(
    "refresh",
    [
        pytest.param(_response(json_body={"status": 4}), id="rejected"),
        pytest.param(_response(status=502, text_body="Bad Gateway"), id="error-status"),
    ],
)
async def test_failed_refresh_falls_back_to_full_login(
    auth: TelmoreAuthManager, session: Mock, refresh: Mock
) -> None:
    """A refresh the backend rejects or fails to answer triggers a full login."""
    auth._refresh_token = "stale-refresh"
    _login_responses(session)
    session.post = Mock(side_effect=[refresh, _response(json_body={"url": CALLBACK_URL})])

    token = await auth.auth_token()

    assert str(token) == ACCESS_TOKEN
    assert auth._refresh_token == REFRESH_TOKEN
