"""Generic operation tools: list, describe and call any generated API operation.

Every call goes through the generated FamilySearchApi method, so the registry
lock, path quoting and reason rules apply unchanged. On top of the lock, writes
through call_operation are beta-only: on production every non-read operation is
refused (before any network call) in favour of the guarded write tools, which
carry the duplicate, living-person, provenance and upload-root checks this
generic path lacks. allow_production is never a tool parameter.

Errors are deliberately not caught here; the MCP layer turns a raised exception
into an error result.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from familysearch import Environment
from familysearch.api import OPERATIONS, ApiResult
from familysearch.api.registry import (
    OperationLockedError,
    check_call,
    effective_status,
    load_registry,
)
from familysearch.api.spec import OpSpec, is_write

from .context import ContextError
from .validation import clamp

MAX_RESULT_CHARS = 50_000
READ_POST_NOTE = (
    "This read is sent as an HTTP POST (a search-like query with a JSON body); it "
    "changes nothing, needs no reason and runs in any environment."
)
_GUARDED_TOOLS = (
    "create_person, add_fact, create_couple, create_parent_child, upload_memory, "
    "link_memory, create_source, attach_source"
)
_PRODUCTION_REFUSAL = (
    "call_operation writes run on beta only. On production use the guarded tools "
    f"({_GUARDED_TOOLS}), or set FS_ENVIRONMENT=beta."
)
_UPLOAD_REFUSAL = "{} uploads a file; call_operation refuses uploads: use upload_memory."


def _kind(spec: OpSpec) -> str:
    # From is_write, so a summary can never call a write a read.
    return "write" if is_write(spec) else "read"


def operation_summary(spec: OpSpec, registry: dict) -> dict:
    return {
        "operation_id": spec.operation_id,
        "area": spec.area,
        "method": spec.http_method,
        "path": spec.path,
        "kind": _kind(spec),
        "destructive": spec.destructive,
        "status": effective_status(spec.operation_id, spec.fingerprint, registry),
    }


def _tool_refusal(spec: OpSpec, environment: Any) -> str | None:
    """The MCP's own write rules (upload, then beta-only); None for reads."""
    if not is_write(spec):
        return None
    if spec.body == "upload":
        return _UPLOAD_REFUSAL.format(spec.operation_id)
    if environment is not Environment.BETA:  # fails closed: only literal BETA
        return _PRODUCTION_REFUSAL
    return None


def describe(spec: OpSpec, registry: dict, environment: Any, writes_allowed: bool) -> dict:
    """Summary plus parameters and whether a call with no flags would run here.

    can_run_here is informational; call_operation enforces the same rules (the
    lock part by calling the library's check_call, so the two cannot drift).
    """
    out = operation_summary(spec, registry)
    out.update(
        description=spec.summary,
        path_params=list(spec.path_params),
        required_query=list(spec.required_query),
        accepts=list(spec.accepts),
        body=spec.body,
        needs_reason=is_write(spec),
    )
    if not is_write(spec) and spec.http_method not in ("GET", "HEAD", "OPTIONS"):
        out["note"] = READ_POST_NOTE
    blocked = _tool_refusal(spec, environment)
    if blocked is None and is_write(spec) and not writes_allowed:
        blocked = "Writes are disabled (see auth_status)."
    if blocked is None:
        try:
            check_call(spec, environment, registry)
        except OperationLockedError as e:
            blocked = str(e)
    out["can_run_here"] = blocked is None
    out["blocked_reason"] = blocked
    return out


def shape_result(result: ApiResult) -> dict:
    """Status, content type, entity id and a size-capped body; never headers."""
    if result.body is not None:
        body: Any = result.body
        serialized = json.dumps(body)
    elif result.text.strip():
        body = serialized = result.text
    else:
        body, serialized = None, ""
    truncated = len(serialized) > MAX_RESULT_CHARS
    if truncated:
        body = {"truncated_preview": serialized[:MAX_RESULT_CHARS]}
    return {
        "status": result.status,
        "content_type": result.content_type,
        "entity_id": result.entity_id,
        "body": body,
        "truncated": truncated,
    }


def _lookup(operation_id: Any) -> OpSpec:
    """Exact lookup; an unknown id raises with close matches as suggestions only."""
    if isinstance(operation_id, str) and operation_id in OPERATIONS:
        return OPERATIONS[operation_id]
    hint = ""
    if isinstance(operation_id, str) and operation_id.strip():
        by_lower = {k.lower(): k for k in OPERATIONS}
        close = difflib.get_close_matches(
            operation_id.strip().lower(), list(by_lower), n=5, cutoff=0.6)
        if close:
            hint = " Did you mean: " + ", ".join(by_lower[c] for c in close) + "?"
    raise ValueError(
        f"unknown operation {operation_id!r}; list_operations shows the ids.{hint}"
    )


def _path_value(name: str, value: Any) -> str:
    """One path segment: a non-blank string with no separators or dot segments."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or "/" in value
        or "\\" in value
        or ".." in value
        or value.strip() == "."
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(
            f"invalid path parameter {name!r}: {value!r} must be one non-empty id "
            "without '/', '\\' or '..'"
        )
    return value


def _positional_args(spec: OpSpec, path_args: Any) -> list[str]:
    path_args = {} if path_args is None else path_args
    if not isinstance(path_args, dict):
        raise ValueError("path_args must be an object of {name: value}")
    extra = set(path_args) - set(spec.path_params)
    if extra:
        raise ValueError(
            f"unexpected path parameters {sorted(map(str, extra))} for "
            f"{spec.operation_id}; it takes {list(spec.path_params)}"
        )
    missing = [n for n in spec.path_params if n not in path_args]
    if missing:
        raise ValueError(f"missing path parameters {missing} for {spec.operation_id}")
    return [_path_value(n, path_args[n]) for n in spec.path_params]


def register_op_tools(mcp: Any, ctx: Any, registry: dict | None = None) -> None:
    reg = registry if registry is not None else load_registry()

    @mcp.tool()
    def list_operations(
        area: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Read-only; needs no network or authentication. Lists the generated
        FamilySearch API operations (all 203 the library can call; operation_id, area, method,
        path, kind read/write, destructive, status verified/unverified/stale/
        unverifiable/excluded). Filter by area, kind or status (exact, case-insensitive)
        and `query` (substring of id, summary or path). Up to `limit` (1-200) rows,
        sorted by operation_id."""
        n = clamp(limit, 1, 200)
        q = query.lower() if query else None
        rows = []
        for op_id in sorted(OPERATIONS):
            spec = OPERATIONS[op_id]
            row = operation_summary(spec, reg)
            if area is not None and row["area"].lower() != area.lower():
                continue
            if kind is not None and row["kind"] != kind.lower():
                continue
            if status is not None and row["status"] != status.lower():
                continue
            if q and not any(q in s.lower() for s in (op_id, spec.summary, spec.path)):
                continue
            rows.append(row)
        return rows[:n]

    @mcp.tool()
    def describe_operation(operation_id: str) -> dict:
        """Read-only; needs no network or authentication; covers only the 203
        operations the library can call. Describes one API operation: its parameters (path_params, required_query,
        accepts, body none/json/upload), whether it needs a reason, and whether a call
        with no flags can run here (can_run_here, blocked_reason)."""
        spec = _lookup(operation_id)
        return describe(spec, reg, ctx.environment, ctx.writes_allowed())

    @mcp.tool()
    def call_operation(
        operation_id: str,
        path_args: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        accept: str | None = None,
        body: Any = None,
        reason: str | None = None,
        allow_unverified: bool = False,
        confirm_destructive: bool = False,
    ) -> dict:
        """Call one API operation by operation_id (see list_operations and
        describe_operation). Reads run anywhere. Writes run on beta only (on production
        use the guarded tools), need a short `reason`, and pass the verification lock.
        allow_unverified (unverified operations) and confirm_destructive (deletes and
        other destructive operations) are Ben's explicit decisions: NEVER set them on
        your own or to get past a refusal. Uploads are refused: use upload_memory.
        Returns {status, content_type, entity_id, body, truncated}."""
        spec = _lookup(operation_id)
        write = is_write(spec)
        if write:
            refusal = _tool_refusal(spec, ctx.environment)
            if spec.body == "upload":
                raise ValueError(refusal)
            if refusal:
                raise ContextError(refusal)
            ctx.require_writes()
            if not (isinstance(reason, str) and reason.strip()):
                raise ValueError("a non-empty reason (the change message) is required")
        args = _positional_args(spec, path_args)
        if body is not None and spec.body != "json":
            raise ValueError(f"{spec.operation_id} does not accept a body")
        if query is not None:
            if not isinstance(query, dict):
                raise ValueError("query must be an object of {name: value}")
            clash = sorted(set(query) & set(spec.path_params))
            if clash:
                raise ValueError(f"path parameters {clash} belong in path_args, not query")
        kwargs: dict[str, Any] = {"accept": accept, "query": query}
        if spec.body == "json":
            kwargs["body"] = body
        if write:
            kwargs.update(
                reason=reason,
                allow_unverified=allow_unverified,
                allow_production=False,  # never reachable from the MCP
                confirm_destructive=confirm_destructive,
            )
        return ctx.call(
            lambda s: shape_result(getattr(s.api, spec.method_name)(*args, **kwargs))
        )
