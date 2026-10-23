"""HTTP client for the FamilySearch platform API."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import requests

from familysearch.auth import Environment
from familysearch.errors import FSError, FSHTTPError, TokenExpiredError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT = "application/x-fs-v1+json"
DEFAULT_TIMEOUT = 30


class FSClient:
    def __init__(
        self,
        token: str,
        environment: Environment,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_write_interval: float = 1.0,
    ):
        self._token = token
        self._environment = environment
        self._user_agent = user_agent
        self._min_write_interval = min_write_interval
        self._last_write: float | None = None
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._environment.base_url

    def get(
        self, path_or_url, *, accept=None, params=None, timeout=DEFAULT_TIMEOUT
    ) -> requests.Response:
        return self._request(
            "GET", path_or_url, accept=accept, params=params, timeout=timeout
        )

    def post(
        self, path_or_url, *, reason, json=None, files=None, params=None,
        timeout=DEFAULT_TIMEOUT,
    ) -> requests.Response:
        return self._request(
            "POST", path_or_url, json=json, files=files, params=params,
            reason=reason, write=True, timeout=timeout,
        )

    def delete(
        self, path_or_url, *, reason, timeout=DEFAULT_TIMEOUT
    ) -> requests.Response:
        return self._request(
            "DELETE", path_or_url, reason=reason, write=True, timeout=timeout
        )

    def _throttle(self) -> None:
        if self._last_write is not None:
            remaining = self._min_write_interval - (time.monotonic() - self._last_write)
            if remaining > 0:
                time.sleep(remaining)
        self._last_write = time.monotonic()

    def _request(
        self, method, path_or_url, *, accept=None, params=None, json=None,
        files=None, reason=None, write=False, timeout=DEFAULT_TIMEOUT,
    ) -> requests.Response:
        if write and not (reason and reason.strip()):
            raise FSError("a non-empty reason (X-Reason) is required for writes")
        if path_or_url.startswith(("http", "//")):
            url = path_or_url
            want, got = urlsplit(self.base_url), urlsplit(url)
            if (got.scheme, got.netloc.lower()) != (want.scheme, want.netloc.lower()):
                raise FSError(
                    f"refusing to send credentials to {got.scheme}://{got.netloc}; "
                    f"only {want.scheme}://{want.netloc} is allowed"
                )
        else:
            url = self.base_url + path_or_url
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._user_agent,
            "Accept": accept or DEFAULT_ACCEPT,
        }
        if write:
            headers["X-Reason"] = reason
            self._throttle()
        resp = self._session.request(
            method, url, headers=headers, params=params, json=json, files=files,
            timeout=timeout,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("FamilySearch rejected the token (401).")
        if resp.status_code >= 400:
            raise FSHTTPError(resp.status_code, resp.text)
        return resp
