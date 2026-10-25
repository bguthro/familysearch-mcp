"""FamilySearch MCP server: assembly and stdio entry point.

A stdio MCP server must never write to stdout (it carries the protocol);
diagnostics go to stderr only.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from .context import Context, ContextError
from .tools_ops import register_op_tools
from .tools_read import register_read_tools
from .tools_write import register_write_tools

SERVER_INSTRUCTIONS = """\
FamilySearch tools for Ben's family tree work.

Start with auth_status: it shows the environment (beta or production) and whether
writes are enabled. Production writes are opt-in; if disabled, tell Ben.

Before creating anything, search (search_tree, search_records) and check get_matches.
Show Ben the evidence. Never create a duplicate.

The flags create_despite_matches, confirm_deceased, override_rights, allow_unverified
and confirm_destructive are Ben's explicit decisions. NEVER set them on your own or to
get past a refusal: report the refusal to Ben and let him say so.

Living people are created private. Documents need a `provenance` citation; aggregator
images (Fold3, Ancestry, Newspapers.com, MyHeritage, Findagrave) are refused.
upload_memory needs the administrator to set FS_UPLOAD_ROOT; if missing, tell Ben, never
set it. upload_memory(person_id=...) uploads and attaches; for several people upload
ONCE, then link_memory (never re-upload). create_parent_child takes both parents in ONE
call. Documents attach to deceased persons only; no override.

list_operations, describe_operation and call_operation reach any API operation.
call_operation writes run on beta only; on production use the guarded tools above.
Uploads always use upload_memory. There is no delete or merge tool on production; on
beta call_operation can run destructive operations only with Ben's confirm_destructive.
Every write needs a short `reason`.
"""


def build_server(ctx: Context | None = None) -> FastMCP:
    """Build the server. Lazy: no token is lifted until a tool runs."""
    if ctx is None:
        ctx = Context()  # raises ContextError on a bad FS_ENVIRONMENT
    mcp = FastMCP("familysearch", instructions=SERVER_INSTRUCTIONS)
    register_read_tools(mcp, ctx)
    register_write_tools(mcp, ctx)
    register_op_tools(mcp, ctx)
    return mcp


def main() -> None:
    try:
        server = build_server()
    except ContextError as e:
        print(f"familysearch-mcp: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    server.run()


if __name__ == "__main__":
    main()
