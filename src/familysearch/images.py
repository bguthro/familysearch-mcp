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


MAX_DAS_COUNT = 2000
MAX_FILM_RANGE = 40
MAX_FILM_IMAGES = 20000
_APID_RE = re.compile(r"[A-Za-z0-9-]+", re.ASCII)
_DAS_HOST = r"sg\d{1,3}p\d{1,3}\.familysearch\.org"
_DAS_PATH = r"/service/records/storage/dascloud/das/v2/"
_DAS_RE = re.compile(
    rf"(https://{_DAS_HOST}){_DAS_PATH}([A-Za-z0-9-]+)"
    r"(?:(/parents)|(/children)(?:\?start=(\d{1,9})&count=(\d{1,4}))?)?",
    re.ASCII,
)


def validate_das_url(url: str) -> str:
    """Strictly validate a das-cloud metadata URL (node, /parents, /children).

    One ASCII fullmatch over the whole string; query only on /children and only
    start=<digits>&count=<digits> with count <= 2000. Raises FSError (no URL).
    """
    bad = FSError("refusing das URL: not an allowed FamilySearch storage location")
    if not isinstance(url, str) or _CTRL_RE.search(url):
        raise bad
    m = _DAS_RE.fullmatch(url)
    if not m:
        raise bad
    count = m.group(6)
    if count is not None and not 1 <= int(count) <= MAX_DAS_COUNT:
        raise bad
    return url


def das_base_of(url: str) -> str:
    """The validated 'https://sgNNpNN.familysearch.org' prefix of a das URL."""
    validate_das_url(url)
    return _DAS_RE.fullmatch(url).group(1)


def check_apid(value) -> str:
    if not isinstance(value, str) or not _APID_RE.fullmatch(value):
        raise FSError("unexpected apid in film metadata")
    return value


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
        self._cache: dict = {}

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

    # ---- film (sibling images in the same microfilm) ----

    def _das_node(self, base: str, apid: str, suffix: str = "") -> dict:
        body = self._client.get_das_json(
            f"{base}{_DAS_PATH}{check_apid(apid)}{suffix}")
        if not isinstance(body, dict):
            raise FSError("unexpected film metadata response")
        return body

    def _film_context(self, ark: str) -> tuple[str, dict, tuple]:
        """Return (das base host, info dict, rows) for the ark's film.

        The image's position is where the ark's own apid appears in the film's
        children list, never a number taken from a name.
        """
        _, body = self._resolve(ark)
        apid = check_apid(body.get("apid"))
        href = self._links(body).get("parents")
        if not isinstance(href, str):
            raise FSError("no film link for this ark")
        base = das_base_of(href)
        parents = self._das_node(base, apid, "/parents").get("parents")
        dgs_parents = [
            p for p in (parents if isinstance(parents, list) else [])
            if isinstance(p, dict) and p.get("nameSpace") == "dgs"
        ]
        if len(dgs_parents) != 1:
            raise FSError("expected exactly one film for this ark")
        dgs = check_apid(dgs_parents[0].get("parentApid"))
        node = self._das_node(base, dgs)
        total = node.get("childCount")
        if not isinstance(total, int) or isinstance(total, bool) \
                or not 1 <= total <= MAX_FILM_IMAGES:
            raise FSError("unexpected image count in film metadata")
        rows = self._children(base, dgs, total)
        positions = [r[0] for r in rows if r[2] == apid]
        if len(positions) != 1:
            raise FSError("this image was not found exactly once in its film")
        film = node.get("name")
        return base, {
            "film": film if isinstance(film, str) else None,
            "image_number": positions[0],
            "total_images": total,
            "dgs_apid": dgs,
        }, rows

    def film_info(self, ark: str) -> dict:
        return self._film_context(ark)[1]

    def _children(self, base: str, dgs: str, total: int) -> tuple:
        """Validated rows (order, name, apid) sorted by order; cached per reader."""
        cached = self._cache.get(dgs)
        if cached is not None and len(cached) == total:
            return cached
        out: list = []
        start = 0
        while start < total:
            count = min(MAX_DAS_COUNT, total - start)
            body = self._client.get_das_json(
                f"{base}{_DAS_PATH}{dgs}/children?start={start}&count={count}")
            kids = body.get("children") if isinstance(body, dict) else None
            if not isinstance(kids, list) or len(kids) != count:
                raise FSError("unexpected film children response")
            out.extend(kids)
            start += count
        if len(out) != total:
            raise FSError("film children count does not match the film")
        rows, seen_orders, seen_apids = [], set(), set()
        for k in out:
            if not isinstance(k, dict):
                raise FSError("unexpected film child entry")
            order, apid = k.get("order"), check_apid(k.get("apid"))
            if not isinstance(order, int) or isinstance(order, bool) \
                    or order in seen_orders or apid in seen_apids:
                raise FSError("film children have duplicate or invalid entries")
            seen_orders.add(order)
            seen_apids.add(apid)
            name = k.get("name")
            rows.append((order, name if isinstance(name, str) else None, apid))
        rows.sort(key=lambda r: r[0])
        if [r[0] for r in rows] != list(range(1, len(rows) + 1)):
            raise FSError("film children have missing orders")
        result = tuple(rows)
        self._cache[dgs] = result
        return result

    def list_film_images(self, ark: str) -> list[dict]:
        rows = self._film_context(ark)[2]
        return [{"order": o, "name": n, "apid": a} for o, n, a in rows]

    def _fetch_apid(self, base: str, apid: str, size: str) -> tuple[bytes, str]:
        name = "dist" if size == "dist" else "thumb_p200"
        url = f"{base}{_DAS_PATH}{check_apid(apid)}/{name}.jpg"
        validate_image_url(url)
        return self._client.get_image(url)

    @staticmethod
    def _check_size(size: str) -> None:
        if size not in SIZES:
            raise ValueError(f"size must be one of {sorted(SIZES)}")

    def fetch_film_image(
        self, ark: str, index: int | None = None, size: str = "dist", *, offset: int = 0
    ) -> tuple[bytes, str]:
        """Fetch image `index` (1-based) of the ark's film, or, when index is
        None, the ark's own image number plus offset."""
        self._check_size(size)
        base, info, rows = self._film_context(ark)
        if index is None:
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise ValueError("offset must be an integer")
            index = info["image_number"] + offset
        if not isinstance(index, int) or isinstance(index, bool) \
                or not 1 <= index <= info["total_images"]:
            raise ValueError(f"index must be between 1 and {info['total_images']}")
        return self._fetch_apid(base, rows[index - 1][2], size)

    def fetch_film_range(self, ark: str, first: int, last: int, size: str = "dist"):
        """Yield (index, bytes, content_type) for first..last (max 40 images)."""
        self._check_size(size)
        if not isinstance(first, int) or not isinstance(last, int) or first > last:
            raise ValueError("first and last must be integers with first <= last")
        if last - first + 1 > MAX_FILM_RANGE:
            raise ValueError(f"at most {MAX_FILM_RANGE} images per call")
        base, info, rows = self._film_context(ark)
        if first < 1 or last > info["total_images"]:
            raise ValueError(f"range must be within 1..{info['total_images']}")
        for i in range(first, last + 1):
            data, ctype = self._fetch_apid(base, rows[i - 1][2], size)
            yield i, data, ctype
