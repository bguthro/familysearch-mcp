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
