"""Narrow, read-only reader for FamilySearch historical record images.

A record image ark resolves (on the production www host) to a JSON document
whose links point at FamilySearch image-storage hosts. Credentials are only
ever sent to a URL that passes validate_image_url.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from familysearch.errors import FSError

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_META_BYTES = 1024 * 1024
IMAGE_DEADLINE_SECONDS = 60
ALLOWED_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
SIZES = {"dist": "stream-image-dist", "thumb_p200": "stream-image-thumb_p200"}

_ARK_ID = r"[A-Z0-9]+(?:-[A-Z0-9]+)*"
_ARK_RE = re.compile(rf"/?ark:/61903/3:1:({_ARK_ID})", re.ASCII)
_URL_RE = re.compile(
    r"https://(?:sg\d{1,3}p\d{1,3}|www)\.familysearch\.org"
    r"/service/records/storage/dascloud/das/v2/[A-Za-z0-9-]+/(?:dist|thumb_p200)\.jpg",
    re.ASCII,
)
_CTRL_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")
_PROD_HOST = "www.familysearch.org"


def parse_ark(value: str) -> str:
    """Return the canonical '/ark:/61903/3:1:<ID>' for an image ark, else ValueError."""
    if not isinstance(value, str) or not value:
        raise ValueError("an image ark is required")
    v = value
    if "://" in v:
        parts = urlsplit(v)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host != _PROD_HOST or parts.username \
                or parts.password or parts.port not in (None, 443):
            raise ValueError("not a https://www.familysearch.org ark URL")
        v = parts.path
    else:
        v = v.split("?", 1)[0].split("#", 1)[0]
    m = _ARK_RE.fullmatch(v)
    if not m:
        raise ValueError("not a FamilySearch image ark (expected /ark:/61903/3:1:ID)")
    return f"/ark:/61903/3:1:{m.group(1)}"


def validate_image_url(url: str) -> str:
    """Strictly validate an image-storage URL; return it, or raise FSError.

    One ASCII fullmatch over the whole string; the exact string is what is sent.
    """
    bad = FSError("refusing image URL: not an allowed FamilySearch image location")
    if not isinstance(url, str) or _CTRL_RE.search(url) or not _URL_RE.fullmatch(url):
        raise bad
    return url


_S3_RE = re.compile(
    r"https://ps-services-[a-z0-9-]+\.s3\.amazonaws\.com"
    r"/s3/pipe-storage-das-cloud-prod-dasS3/([A-Za-z0-9-]+)/(dist|thumb_p200)\.jpg"
    r"\?[A-Za-z0-9%._~!$&'()*+,;=:@/?-]+",
    re.ASCII,
)
_SRC_RE = re.compile(
    r"https://[^/]+/service/records/storage/dascloud/das/v2/([A-Za-z0-9-]+)/"
    r"(dist|thumb_p200)\.jpg",
    re.ASCII,
)
MAX_LOCATION = 4096


def validate_s3_location(location, source_url: str) -> str:
    """Validate the pre-signed S3 redirect target for an image URL we asked for.

    It must be the same apid and filename in the das-cloud bucket, https, no
    userinfo or port, with a signed query and no fragment. Errors carry no URL.
    """
    bad = FSError("refusing image redirect: not the expected FamilySearch image storage")
    src = _SRC_RE.fullmatch(source_url)
    if (
        not src or not isinstance(location, str) or len(location) > MAX_LOCATION
        or _CTRL_RE.search(location)
    ):
        raise bad
    m = _S3_RE.fullmatch(location)
    if not m or (m.group(1), m.group(2)) != src.groups():
        raise bad
    return location


def sniff_matches(content_type: str, head: bytes) -> bool:
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG")
    if content_type == "image/gif":
        return head.startswith(b"GIF8")
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def normalize_content_type(value: str) -> str:
    base = (value or "").split(";", 1)[0].strip().lower()
    if base not in ALLOWED_TYPES:
        raise FSError("image response has a disallowed content type")
    return base


class ImageReader:
    def __init__(self, client):
        self._client = client

    def _require_production(self) -> None:
        if self._client.base_url != "https://www.familysearch.org":
            raise FSError("record images are only available on production")

    def _resolve(self, ark: str) -> tuple[str, dict]:
        self._require_production()
        canon = parse_ark(ark)
        raw = self._client.get_capped(
            "https://www.familysearch.org" + canon, accept="application/json",
            max_bytes=MAX_META_BYTES,
        )
        try:
            body = json.loads(raw)
        except ValueError:
            raise FSError("unexpected response resolving the image ark") from None
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
