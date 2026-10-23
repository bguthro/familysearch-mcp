"""Shared URL helpers."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

_TEMPLATE = re.compile(r"\{[^}]*\}")


def join_path(href: str, *segments: str) -> str:
    """Append segments to the PATH of href, keeping its query string intact.

    Discovery hrefs often look like ``https://h/platform/tree/persons?flag=fsh``;
    naive ``href + "/" + id`` would put the id inside the query. URI-template
    parts (``{...}``) are stripped; each segment is percent-encoded.
    """
    parts = urlsplit(_TEMPLATE.sub("", href))
    path = parts.path.rstrip("/")
    for seg in segments:
        path += "/" + quote(seg, safe="")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
