"""HTTP client for the FamilySearch platform API."""

from __future__ import annotations

import json as _json
import time
from urllib.parse import urlsplit

import requests

from familysearch.auth import Environment
from familysearch.errors import FSError, FSHTTPError, TokenExpiredError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FS_JSON = "application/x-fs-v1+json"
DEFAULT_ACCEPT = FS_JSON
DEFAULT_TIMEOUT = 30
METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"})


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

    @property
    def environment(self) -> Environment:
        return self._environment

    def get(
        self, path_or_url, *, accept=None, params=None, timeout=DEFAULT_TIMEOUT
    ) -> requests.Response:
        return self.request(
            "GET", path_or_url, write=False, accept=accept, params=params,
            timeout=timeout,
        )

    def post(
        self, path_or_url, *, reason, json=None, files=None, params=None,
        timeout=DEFAULT_TIMEOUT,
    ) -> requests.Response:
        return self.request(
            "POST", path_or_url, write=True, reason=reason, json=json,
            files=files, params=params, timeout=timeout,
        )

    def delete(
        self, path_or_url, *, reason, timeout=DEFAULT_TIMEOUT
    ) -> requests.Response:
        return self.request(
            "DELETE", path_or_url, write=True, reason=reason, timeout=timeout
        )

    def _throttle(self) -> None:
        if self._last_write is not None:
            remaining = self._min_write_interval - (time.monotonic() - self._last_write)
            if remaining > 0:
                time.sleep(remaining)
        self._last_write = time.monotonic()

    def request(
        self, method, path_or_url, *, write, reason=None, accept=None,
        params=None, json=None, files=None, data=None, content_type=None,
        timeout=DEFAULT_TIMEOUT,
    ) -> requests.Response:
        method = method.upper()
        if method not in METHODS:
            raise ValueError(f"unsupported HTTP method {method!r}")
        if sum(b is not None for b in (json, files, data)) > 1:
            raise ValueError("json, files and data are mutually exclusive")
        if data is not None and not content_type:
            raise ValueError("data requires a content_type")
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
        if json is not None:
            # requests' json= forces application/json (FamilySearch answers 415).
            data = _json.dumps(json).encode()
            headers["Content-Type"] = FS_JSON
        elif data is not None:
            headers["Content-Type"] = content_type
        resp = self._session.request(
            method, url, headers=headers, params=params, data=data, files=files,
            timeout=timeout,
        )
        if resp.status_code == 401:
            raise TokenExpiredError("FamilySearch rejected the token (401).")
        if resp.status_code >= 400:
            raise FSHTTPError(resp.status_code, resp.text)
        return resp
