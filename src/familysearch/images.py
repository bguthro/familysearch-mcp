"""Narrow, read-only reader for FamilySearch historical record images.

A record image ark resolves (on the production www host) to a JSON document
whose links point at FamilySearch image-storage hosts. Credentials are only
ever sent to a URL that passes validate_image_url.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from familysearch.errors import FSError

MAX_IMAGE_BYTES = 25 * 1024 * 1024
SIZES = {"dist": "stream-image-dist", "thumb_p200": "stream-image-thumb_p200"}

_ARK_ID = r"[A-Z0-9]+(?:-[A-Z0-9]+)*"
_ARK_RE = re.compile(rf"^/?ark:/61903/3:1:({_ARK_ID})$")
_HOST_RE = re.compile(r"^sg\d{1,3}p\d{1,3}\.familysearch\.org$")
_PATH_RE = re.compile(
    r"^/service/records/storage/dascloud/das/v2/[A-Za-z0-9-]+/(dist|thumb_p200)\.jpg$"
)
_PROD_HOST = "www.familysearch.org"


def parse_ark(value: str) -> str:
    """Return the canonical '/ark:/61903/3:1:<ID>' for an image ark, else ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("an image ark is required")
    v = value.strip()
    if "://" in v:
        parts = urlsplit(v)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host != _PROD_HOST or parts.username \
                or parts.password or parts.port not in (None, 443):
            raise ValueError("not a https://www.familysearch.org ark URL")
        v = parts.path
    else:
        v = v.split("?", 1)[0].split("#", 1)[0]
    m = _ARK_RE.match(v)
    if not m:
        raise ValueError("not a FamilySearch image ark (expected /ark:/61903/3:1:ID)")
    return f"/ark:/61903/3:1:{m.group(1)}"


def validate_image_url(url: str) -> str:
    """Strictly validate an image-storage URL; return it, or raise FSError."""
    bad = FSError("refusing image URL: not an allowed FamilySearch image location")
    if not isinstance(url, str):
        raise bad
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise bad from None
    host = (parts.hostname or "")
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or port is not None
        or parts.netloc != host  # also rejects uppercase and stray characters
        or not (_HOST_RE.match(host) or host == _PROD_HOST)
        or parts.query or parts.fragment or "?" in url or "#" in url
        or not _PATH_RE.match(parts.path)
    ):
        raise bad
    return url


class ImageReader:
    def __init__(self, client):
        self._client = client

    def _require_production(self) -> None:
        if self._client.base_url != "https://www.familysearch.org":
            raise FSError("record images are only available on production")

    def _resolve(self, ark: str) -> tuple[str, dict]:
        self._require_production()
        canon = parse_ark(ark)
        body = self._client.get(
            "https://www.familysearch.org" + canon, accept="application/json"
        ).json()
        if not isinstance(body, dict):
            raise FSError("unexpected response resolving the image ark")
        return canon, body

    @staticmethod
    def _links(body: dict) -> dict:
        out = {}
        for link in body.get("link") or []:
            if isinstance(link, dict) and isinstance(link.get("rel"), str):
                out.setdefault(link["rel"], link.get("href"))
        return out

    def get_image_info(self, ark: str) -> dict:
        canon, body = self._resolve(ark)
        links = self._links(body)
        return {
            "ark": canon,
            "name": body.get("name"),
            "apid": body.get("apid"),
            "sizes": sorted(links),
            "fetchable_sizes": [s for s, rel in SIZES.items() if rel in links],
        }

    def fetch_image(self, ark: str, size: str = "dist") -> tuple[bytes, str]:
        if size not in SIZES:
            raise ValueError(f"size must be one of {sorted(SIZES)}")
        _, body = self._resolve(ark)
        href = self._links(body).get(SIZES[size])
        if not href:
            raise FSError(f"no {size} image link for this ark")
        validate_image_url(href)
        return self._client.get_image(href)
