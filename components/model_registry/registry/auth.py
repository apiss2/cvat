# SPDX-License-Identifier: MIT
from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request

from .config import Settings, url_origin

REQUEST_HEADER = "x-registry-request"
SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


@dataclass(frozen=True)
class User:
    name: str
    admin: bool = False
    cvat_id: int | None = None


def bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )
    return token


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tls_verify = settings.cvat_tls_verify()
        self.service_token_file = settings.service_token_file

    async def _cvat_request(self, session: str) -> dict:
        # Forward only the existing CVAT session cookie, to one configured origin.
        # Never pass incoming Authorization, Host, forwarded headers or other cookies.
        # A fresh client also prevents Set-Cookie responses from crossing users.
        public = urlsplit(url_origin(self.settings.public_url))
        headers = {
            # CVAT's API renderer requires this media type; application/json
            # returns 406. Omitting version uses the server's default version.
            "Accept": "application/vnd.cvat+json",
            "Cookie": f"{self.settings.cvat_session_cookie}={session}",
            # Django rejects underscores in HTTP Host, including the standard
            # Compose service name cvat_server. Use the configured public host;
            # the connection still goes to cvat_url on the internal network.
            "Host": public.netloc,
            "X-Forwarded-Proto": public.scheme,
        }
        try:
            async with httpx.AsyncClient(
                timeout=10,
                trust_env=False,
                follow_redirects=False,
                verify=self.tls_verify,
            ) as client:
                response = await client.get(
                    self.settings.cvat_url.rstrip("/") + "/api/users/self",
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                503, "CVAT is unavailable; check the registry connection"
            ) from exc
        if response.status_code == 400:
            raise HTTPException(
                502,
                "CVAT rejected the registry request; check MR_PUBLIC_URL and CVAT ALLOWED_HOSTS",
            )
        if response.status_code == 401:
            raise HTTPException(401, "The CVAT session is no longer valid")
        if response.status_code == 403:
            raise HTTPException(403, "CVAT denied access to the current user")
        if response.status_code == 406:
            raise HTTPException(
                502,
                "CVAT rejected the requested response format (HTTP 406); "
                "check that CVAT and its proxy accept application/vnd.cvat+json",
            )
        if response.status_code == 429:
            raise HTTPException(
                429,
                "CVAT is temporarily rate limited; try again later",
                headers={"Retry-After": "60"},
            )
        if response.status_code != 200:
            raise HTTPException(
                503, "CVAT is unavailable; check the registry connection"
            )
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
            return result
        except ValueError as exc:
            raise HTTPException(503, "CVAT returned an invalid user response") from exc

    async def _identity(self, session: str) -> User:
        data = await self._cvat_request(session)
        name, identifier = data.get("username"), data.get("id")
        if type(data.get("is_active")) is not bool:
            raise HTTPException(503, "CVAT returned an invalid user status")
        if data["is_active"] is False:
            raise HTTPException(403, "CVAT account is inactive")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 150
            or name != name.strip()
            or any(ord(char) < 32 for char in name)
            or type(identifier) is not int
            or identifier <= 0
        ):
            raise HTTPException(503, "CVAT returned an invalid user identity")
        groups = data.get("groups", [])
        if not isinstance(groups, list) or not all(
            isinstance(group, str) for group in groups
        ):
            raise HTTPException(503, "CVAT returned invalid user groups")
        # CVAT instance administration is the admin group or superuser flag.
        # Django's is_staff flag alone grants no registry administrator rights.
        return User(
            name, data.get("is_superuser") is True or "admin" in groups, identifier
        )

    def check_origin(self, request: Request) -> None:
        if request.headers.get("origin") != url_origin(self.settings.public_url):
            raise HTTPException(
                403, "A request from the configured registry origin is required"
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site requests are not allowed")

    async def user(self, request: Request) -> User:
        session = request.cookies.get(self.settings.cvat_session_cookie, "")
        # Django session values (including signed-cookie sessions) are ASCII.
        # Reject cookie/header separators before constructing an upstream header.
        if not re.fullmatch(r"[A-Za-z0-9_:.=+-]{1,4096}", session):
            raise HTTPException(
                401, "The CVAT session cookie is not available to the registry"
            )
        if request.method not in SAFE_METHODS:
            self.check_origin(request)
            # A browser cannot add this header cross-origin without a successful
            # CORS preflight; this application deliberately grants no CORS access.
            # This also covers multipart uploads before their body is parsed.
            if request.headers.get(REQUEST_HEADER) != "1":
                raise HTTPException(403, "X-Registry-Request: 1 is required")
        # Revalidate every request: CVAT logout, account disable and role changes
        # apply immediately. There is no registry password, token or session store.
        user = await self._identity(session)
        expected_id = request.headers.get("x-registry-user-id")
        if (
            request.method not in SAFE_METHODS
            and expected_id is not None
            and expected_id != str(user.cvat_id)
        ):
            # The browser supplies the identity currently shown in the registry.
            # It grants no permission: only CVAT decides who is authenticated.
            # Reject a stale screen before reading a potentially large upload.
            raise HTTPException(
                409,
                "CVAT account changed; refresh the registry before continuing",
                headers={"X-Registry-Account-Changed": "1"},
            )
        return user

    def service(self, request: Request) -> None:
        supplied = bearer(request)
        try:
            expected = self.service_token_file.read_text().strip()
        except OSError as exc:
            raise HTTPException(503, "Service token unavailable") from exc
        if len(expected) < 32 or not hmac.compare_digest(
            supplied.encode(), expected.encode()
        ):
            raise HTTPException(401, "Invalid service token")


def owner(user: User, record: dict) -> None:
    if not user.admin and user.name != record["owner"]:
        raise HTTPException(
            403, "Only the owner or an administrator may access this model"
        )
