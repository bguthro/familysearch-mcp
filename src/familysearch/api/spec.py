"""The runtime description of one API operation (mirrors the generator's ResolvedOp)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class OpSpec:
    operation_id: str
    method_name: str
    area: str
    http_method: str
    path: str
    summary: str
    kind: str
    destructive: bool
    path_params: tuple[str, ...]
    required_query: tuple[str, ...]
    accepts: tuple[str, ...]
    body: str
    fingerprint: str


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Read-only operations the API happens to expose as POST (search-like queries).
READ_POST_ALLOWLIST = frozenset(
    {"createNameSegments", "getSourceDescriptionChanges", "performPersonMatchesByExample"}
)


def is_write(spec: OpSpec) -> bool:
    """The one definition of "write" shared by the lock and the runtime.

    Fails closed: anything not declared a read is a write, and a read-kind
    operation with a state-changing HTTP method is still a write unless it is
    allow-listed.
    """
    if spec.kind != "read":
        return True
    return (spec.http_method not in SAFE_METHODS
            and spec.operation_id not in READ_POST_ALLOWLIST)
