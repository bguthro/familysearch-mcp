"""Runtime for generated API methods: lock check, safe URL building, response parsing."""
import json as _json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .registry import check_call, load_registry
from .spec import OpSpec, is_write

API_PREFIX = "/platform"
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass
class ApiResult:
    status: int
    headers: dict[str, str]
    content_type: str
    body: Any | None
    text: str

    @property
    def entity_id(self) -> str | None:
        lowered = {k.lower(): v for k, v in self.headers.items()}
        if lowered.get("x-entity-id"):
            return lowered["x-entity-id"]
        location = lowered.get("location")
        if location:
            last = location.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            return last or None
        return None


def _quote_segment(name: str, value: Any) -> str:
    text = "" if value is None else str(value)
    if not text.strip():
        raise ValueError(f"path parameter {name!r} must not be empty")
    if text.strip() in (".", ".."):
        raise ValueError(f"path parameter {name!r} must not be {text.strip()!r}")
    return quote(text, safe="")


def _render_query(query: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (query or {}).items():
        if value is None:
            continue
        out[key] = ("true" if value else "false") if isinstance(value, bool) else value
    return out


class ApiBase:
    def __init__(self, client, *, registry: dict | None = None):
        self._client = client
        self._registry = registry if registry is not None else load_registry()

    def _call(
        self,
        spec: OpSpec,
        path_args: Mapping[str, Any],
        *,
        query: Mapping[str, Any] | None = None,
        accept: str | None = None,
        body: Any = None,
        files: Mapping[str, Any] | None = None,
        data: bytes | None = None,
        data_content_type: str | None = None,
        reason: str | None = None,
        allow_unverified: bool = False,
        allow_production: bool = False,
        confirm_destructive: bool = False,
    ) -> ApiResult:
        # The lock first, before anything that could touch the network.
        write = is_write(spec)  # shared with check_call so they cannot disagree
        if write:
            check_call(
                spec, self._client.environment, self._registry,
                allow_unverified=allow_unverified, allow_production=allow_production,
                confirm_destructive=confirm_destructive,
            )
        if body is not None and spec.body == "none":
            raise ValueError(f"{spec.operation_id} does not accept a body")
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("query must be a mapping")
        extra = set(path_args) - set(spec.path_params)
        if extra:
            raise ValueError(f"unexpected path parameters {sorted(extra)}")
        quoted = {}
        for name in spec.path_params:
            if name not in path_args:
                raise ValueError(f"missing path parameter {name!r}")
            quoted[name] = _quote_segment(name, path_args[name])
        path = _PLACEHOLDER.sub(
            lambda m: quoted.get(m.group(1), m.group(0)), spec.path
        )
        if _PLACEHOLDER.search(path):
            raise ValueError(f"unfilled placeholder in path {path!r}")
        if spec.accepts:
            if accept is not None and accept not in spec.accepts:
                raise ValueError(f"accept {accept!r} not one of {list(spec.accepts)}")
            accept = accept or spec.accepts[0]
        for name in spec.required_query:
            if name not in (query or {}):
                raise ValueError(f"missing required query parameter {name!r}")
        if (files is not None or data is not None) and spec.body != "upload":
            raise ValueError(f"{spec.operation_id} does not accept an upload")
        resp = self._client.request(
            spec.http_method, API_PREFIX + path, write=write,
            reason=reason, accept=accept, params=_render_query(query) or None,
            json=body, files=files, data=data, content_type=data_content_type,
        )
        content_type = resp.headers.get("Content-Type", "") or ""
        parsed = None
        if resp.text.strip() and "json" in content_type.lower():
            try:
                parsed = _json.loads(resp.text)
            except ValueError:
                parsed = None
        return ApiResult(resp.status_code, dict(resp.headers), content_type, parsed, resp.text)
