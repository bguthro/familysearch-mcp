"""Token extraction from Brave and environment detection."""

from __future__ import annotations

from enum import Enum
from typing import Callable

import browser_cookie3

from familysearch.errors import FSError

_COOKIE_NAME = "fssessionid"


class Environment(Enum):
    BETA = "https://beta.familysearch.org"
    PRODUCTION = "https://www.familysearch.org"

    @property
    def base_url(self) -> str:
        return self.value

    @property
    def other(self) -> Environment:
        return Environment.PRODUCTION if self is Environment.BETA else Environment.BETA


def extract_brave_token() -> str:
    """Return the fssessionid cookie value from Brave.

    Brave holds a single .familysearch.org fssessionid, owned by whichever
    environment was signed into last; the environment is validated separately
    via detect_environment.
    """
    jar = browser_cookie3.brave(domain_name="familysearch.org")
    for cookie in jar:
        if cookie.name == _COOKIE_NAME and "familysearch.org" in cookie.domain:
            return cookie.value
    raise FSError(
        "No FamilySearch session cookie found in Brave. "
        "Please log in to familysearch.org in Brave and try again."
    )


def detect_environment(token: str, http_get: Callable[[str], int]) -> Environment:
    """Return the Environment whose host accepts the token (HTTP 200).

    http_get(base_url) must return the status of an authenticated
    /platform/users/current request against that host.
    """
    for env in (Environment.PRODUCTION, Environment.BETA):
        if http_get(env.base_url) == 200:
            return env
    raise FSError(
        "Brave's FamilySearch session was rejected by both beta and production. "
        "Please log in to familysearch.org in Brave again."
    )
