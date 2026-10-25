# familysearch

Two things live in this repo:

- `familysearch`: a Python client library for the FamilySearch API (`requests` only).
- `familysearch_mcp`: an MCP server (stdio) that exposes 8 read tools and 8 guarded
  write tools over that library, for use from Claude Code and other MCP clients.

**Platform:** macOS and the Brave browser only today. Authentication reads your
FamilySearch session from Brave's cookie store.

How the API endpoints were found, and how to add new ones: see [docs/api-discovery.md](docs/api-discovery.md).

## MCP server

### Install

    python3 -m venv .venv && .venv/bin/pip install -e .

### Claude Code configuration

    claude mcp add familysearch --env FS_ENVIRONMENT=production --env FS_UPLOAD_ROOT=/absolute/path/to/your/research/archive -- /absolute/path/to/.venv/bin/familysearch-mcp

Equivalent `.mcp.json`:

    {
      "mcpServers": {
        "familysearch": {
          "command": "/absolute/path/to/.venv/bin/familysearch-mcp",
          "env": {
            "FS_ENVIRONMENT": "production",
            "FS_UPLOAD_ROOT": "/absolute/path/to/your/research/archive"
          }
        }
      }
    }

`python -m familysearch_mcp.server` also works. A bad `FS_ENVIRONMENT` makes the server
exit with code 2 and a one-line message on stderr.

### Environment variables

| Variable | Meaning |
|---|---|
| `FS_ENVIRONMENT` | `production` (default) or `beta`. |
| `FS_ALLOW_PROD_WRITES` | Opt-in (`1`, `true` or `yes`). Reads always work; writes on production are refused unless this is set. Writes on beta are always allowed. |
| `FS_UPLOAD_ROOT` | REQUIRED for `upload_memory`: an absolute path to a NARROW archive directory. Uploads are refused until it is set. Do not set it to `/`, your home directory, or a parent of your home directory. |

### Authentication

Log in to familysearch.org (or beta.familysearch.org for `FS_ENVIRONMENT=beta`) in Brave
first. The first time a tool runs, macOS shows a Keychain prompt ("Brave Safe Storage")
so the server can read the session cookie. Nothing is read at startup; the token is
lifted lazily when the first tool runs. Tokens last about an hour; when a tool says the
session expired, log in again in Brave and retry. Beta and production tokens are
separate, and the server checks that Brave is signed into the environment you configured.

### Guardrails

- **Match gate:** `create_person` searches the tree first and, if there are plausible
  matches, returns them (`matches_found`) instead of creating anything.
- **Living vs deceased:** living people are created private; a deceased person needs a
  recognizable past death date or an explicit `confirm_deceased`.
- **Provenance:** `upload_memory` refuses images that appear to come from aggregators
  (Fold3, Ancestry, Newspapers.com, MyHeritage, Findagrave) and refuses documents of
  unknown provenance; supply a `provenance` citation.
- **Upload root:** only files under `FS_UPLOAD_ROOT` can be uploaded (PNG, JPEG, TIFF,
  PDF, up to 15 MB).
- **No delete or merge tools exist.**
- **Change messages:** every write requires a `reason`, recorded in FamilySearch's
  change history.
- **Write gate:** production writes are off unless `FS_ALLOW_PROD_WRITES` is set.

### Confirmation authority

`create_despite_matches`, `confirm_deceased` and `override_rights` are meant to be Ben's
explicit decisions. The assistant must surface the evidence and ask, never set them on
its own. The server instructions and tool descriptions say this.

### Tools

| Tool | What it does | Kind |
|---|---|---|
| `auth_status` | Environment, signed-in user (id, displayName, personId), writes enabled | read |
| `search_tree` | Search the Family Tree by name, birth date/place | read |
| `search_records` | Search historical records by name | read |
| `get_person` | One person: name, lifespan, gender, living flag, facts | read |
| `get_relatives` | Parents, children, spouses | read |
| `get_person_sources` | Sources attached to a person | read |
| `get_person_memories` | Memories attached to a person | read |
| `get_matches` | FamilySearch's suggested matches for a person | read |
| `create_person` | Create a person (match gate, living/deceased routing) | write |
| `add_fact` | Add a fact (birth, death, residence, ...) to a person | write |
| `create_couple` | Create a couple relationship | write |
| `create_parent_child` | Link a child to one or more parents | write |
| `upload_memory` | Upload a document/image as a public Memory (provenance gated) | write |
| `link_memory` | Attach an existing memory to a person | write |
| `create_source` | Create a source description | write |
| `attach_source` | Attach an existing source to a person | write |

### Residual risks (read these)

- The override flags (`create_despite_matches`, `confirm_deceased`, `override_rights`)
  are enforced only by the tool descriptions and server instructions: an MCP call cannot
  prove a human approved it. Use your client's per-tool approval for the write tools.
- A hard link inside the upload root to a file outside it is not detected.
- A relative `file_path` resolves against the server's working directory; use absolute
  paths.
- `create_parent_child` can partially succeed. It then fails with a `PartialWriteError`
  that reports which links were created. Do not retry the whole call.
- Privacy: `auth_status` returns only id, displayName and personId, nothing else from
  your account.

## Running the tests

**Unit suite** (offline, no credentials; includes the MCP server tests):

    .venv/bin/python -m pytest tests/unit

**Integration suite** (live FamilySearch *beta*; needs a session token). It creates
and deletes clearly-labelled `ZZSPIKE` throwaway data. Without a token the whole
module is skipped. Log in to beta.familysearch.org in Brave first.

    FS_USE_BRAVE=1 .venv/bin/python -m pytest tests/integration        # reads the fssessionid cookie from Brave (Keychain prompt)
    FS_BETA_TOKEN=<token> .venv/bin/python -m pytest tests/integration # or supply the token directly
