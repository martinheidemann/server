"""Telmore Musik authentication manager."""

import re
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import (
    lock,
    try_parse_int,
)
from music_assistant.providers.telmore.api_client import JsonLike

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider

DELEGATED_LOGIN_URL = "https://musik.telmore.dk/api/delegatedlogin"
TOKEN_URL = "https://musik.telmore.dk/api/token"
INTERNAL_LOGIN_URL = "https://id.telmore.dk/internal-login"


class TelmoreAccessToken:
    """Telmore Musik access token wrapper."""

    def __init__(self, access_token: str) -> None:
        """Initialize TelmoreAccessToken."""
        self._access_token = access_token
        self._token_parts = self._parse_access_token(access_token)

    def is_expired(self) -> bool:
        """Return True if token is expired."""
        expires_at = try_parse_int(self._token_parts.get("ExpiresOn", 0))
        return not expires_at or expires_at <= time.time()

    def _parse_access_token(self, token: str) -> JsonLike:
        return dict(part.split("=", 1) for part in token.split("&") if "=" in part)

    def __str__(self) -> str:
        """Return string representation of the access token."""
        return self._access_token


class TelmoreAuthManager:
    """Telmore Musik authentication manager."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize TelmoreAuthManager."""
        self._access_token: TelmoreAccessToken | None = None
        self._refresh_token: str | None = None
        self.mass = provider.mass
        self.provider = provider
        self.logger = provider.logger

    def invalidate(self) -> None:
        """Invalidate current access token."""
        self._access_token = None

    @lock
    async def auth_token(self) -> TelmoreAccessToken | None:
        """Authenticate and return access token."""
        if self._access_token and not self._access_token.is_expired():
            return self._access_token

        # Try refresh token flow first
        if self._refresh_token and (token := await self._refresh()):
            return token

        return await self._login()

    async def _refresh(self) -> TelmoreAccessToken | None:
        """Exchange the refresh token for a new access token."""
        self.logger.debug("Trying to fetch refresh token")

        # Telmore's token endpoint only accepts a JSON body; form-encoding returns
        # a 415. (YouSee, on the same 24-7 Entertainment platform, accepts a form.)
        async with self.mass.http_session.post(
            TOKEN_URL, json={"refresh_token": self._refresh_token}
        ) as refresh_response:
            if refresh_response.status != 200:
                self.logger.debug("Refresh token flow failed: %s", refresh_response.status)
                return None
            refresh_result = await refresh_response.json()

        if refresh_result.get("status", 4) != 0:
            self.logger.debug("Refresh token rejected, falling back to full login")
            return None

        self.logger.debug("Refresh token flow success")
        self._access_token = TelmoreAccessToken(refresh_result["tokenResult"]["access_token"])
        self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
        return self._access_token

    async def _login(self) -> TelmoreAccessToken | None:
        """Perform a full username/password login.

        Telmore runs the 24-7 Entertainment web app in `OauthEmbedded` mode: the
        delegated login redirects to the Telmore identity provider, which takes the
        credentials as JSON and hands back a callback URL. That callback page posts
        the tokens to its opener via `postMessage`, so they are read from its body.
        """
        # 1. Start the delegated login to obtain the opaque session blob
        async with self.mass.http_session.get(
            DELEGATED_LOGIN_URL, allow_redirects=False
        ) as delegate_response:
            location = delegate_response.headers.get("Location", "")

        session = parse_qs(urlparse(location).query).get("session", [None])[0]
        if not session:
            raise LoginFailed("Telmore did not hand out a login session")

        # 2. Exchange credentials for the callback URL (400 means bad credentials)
        async with self.mass.http_session.post(
            INTERNAL_LOGIN_URL,
            json={
                "session": session,
                "username": self.provider.get_setup_value(CONF_USERNAME),
                "password": self.provider.get_setup_value(CONF_PASSWORD),
            },
        ) as login_response:
            if login_response.status == 400:
                raise LoginFailed("Invalid Telmore username or password")
            if login_response.status != 200:
                raise LoginFailed(f"Telmore login failed with status {login_response.status}")
            callback_url = (await login_response.json()).get("url")

        if not callback_url:
            raise LoginFailed("Telmore login did not return a callback url")

        # 3. Read the tokens out of the callback page
        async with self.mass.http_session.get(callback_url) as callback_response:
            callback_body = await callback_response.text()

        access_token_re = re.search(r'accessToken:\s*"([^"]+)"', callback_body)
        refresh_token_re = re.search(r'refreshToken:\s*"([^"]+)"', callback_body)

        if not access_token_re or not refresh_token_re:
            return None

        self._refresh_token = refresh_token_re.group(1)
        self._access_token = TelmoreAccessToken(access_token_re.group(1))
        self.logger.debug("Got new auth token")

        return self._access_token
