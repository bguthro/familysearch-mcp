# familysearch

Two things live in this repo:

- `familysearch`: a Python client library for the FamilySearch API. The distribution depends on
  `requests`, `browser_cookie3` and `mcp==1.30.0`, so library users also pull in the MCP SDK.
- `familysearch_mcp`: an MCP server (stdio) that exposes 19 tools (8 read tools, 8 guarded
  write tools and 3 generic operation tools) over that library, for use from Claude Code and other MCP clients.

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
  matches, returns them (`matches_found`) instead of creating anything. Candidates
  with confidence >= 3 (or unknown confidence) block creation; weaker ones are
  ignored but counted (`weak_candidates_ignored`).
- **Living vs deceased:** living people are created private; a deceased person needs a
  recognizable past death date or an explicit `confirm_deceased`.
- **Provenance:** `upload_memory` refuses images that appear to come from aggregators
  (Fold3, Ancestry, Newspapers.com, MyHeritage, Findagrave) and refuses documents of
  unknown provenance; supply a `provenance` citation.
- **Upload root:** only files under `FS_UPLOAD_ROOT` can be uploaded (PNG, JPEG, TIFF,
  PDF, up to 15 MB).
- **No guarded delete or merge tools exist.** On production nothing can delete or merge;
  on beta only `call_operation` can, and only with Ben's `confirm_destructive`.
- **Change messages:** every write requires a `reason`, recorded in FamilySearch's
  change history.
- **Write gate:** production writes are off unless `FS_ALLOW_PROD_WRITES` is set.

### Confirmation authority

`create_despite_matches`, `confirm_deceased`, `override_rights`, `allow_unverified` and
`confirm_destructive` are meant to be Ben's
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
| `create_parent_child` | Record a child's 1-2 parents as ONE child-and-parents relationship (the couple link is derived) | write |
| `upload_memory` | Upload a document/image as a public Memory (provenance gated); with `person_id` it is attached in the same step | write |
| `link_memory` | Attach an existing memory to another person (upload once, link to each other person; never re-upload) | write |
| `create_source` | Create a source description | write |
| `attach_source` | Attach an existing source to a person | write |

### Generic operation tools

Three more tools (19 tools in all) reach every operation the library can call (203):

| Tool | What it does | Kind |
|---|---|---|
| `list_operations` | Filter operations by area, kind, status or text (read-only, offline) | read |
| `describe_operation` | One operation's parameters, whether it needs a reason, and whether it can run here (read-only, offline) | read |
| `call_operation` | Call an operation by `operation_id` with `path_args`, `query`, `body`, `reason` | read/write |

Rules:

- Reads run in any environment. Writes through `call_operation` run on beta only; on
  production every write is refused, so use the guarded tools above (they carry the
  duplicate, living-person, provenance and upload-root checks).
- Writes need a `reason` and pass the library's verification lock. A verified write
  (proven on beta: the pilot's `createPerson`, `deletePerson`, `restorePerson` and the
  P3 Family Tree person, note and relationship writes; see Verification status) runs on
  beta as is; any other write needs `allow_unverified`.
- `allow_unverified` and `confirm_destructive` (deletes and other destructive operations)
  are Ben's explicit decisions; the assistant never sets them on its own.
- The 4 unverifiable operations (and the 2 excluded OAuth ones) can never run, even with
  the flags.
- Uploads are refused: use `upload_memory`.

### Residual risks (read these)

- The override flags (`create_despite_matches`, `confirm_deceased`, `override_rights`,
  `allow_unverified`, `confirm_destructive`) are enforced only by the tool descriptions and server instructions: an MCP call cannot
  prove a human approved it. Use your client's per-tool approval for the write tools.
- A hard link inside the upload root to a file outside it is not detected.
- A relative `file_path` resolves against the server's working directory; use absolute
  paths.
- NO production write has ever been exercised. Every endpoint was proven on beta only,
  and several originally "proven" beta endpoints turned out wrong (parent-child, memory
  link, couple delete). Run the first production writes as a supervised pilot, reading
  the result back.
- A true duplicate that FamilySearch scores below 3 passes the match gate; it is visible
  afterwards in the `weak_candidates` list of the `create_person` result.
- Zero-width and other format (Cf) characters are accepted in free text.
- Writes are not idempotent: if a call times out, check the tree before retrying (a
  retry can duplicate a person, relationship or persona).
- Documents are never attached to living persons (policy: deceased persons only);
  `upload_memory(person_id=...)` and `link_memory` answer `living_person_blocked`, with
  no override.
- Tools run inline on the server's event loop, so a 15 MB upload blocks other calls
  while it runs.
- Privacy: `auth_status` returns only id, displayName and personId, nothing else from
  your account.

## Generated API

`familysearch.api.FamilySearchApi` is a typed, generated wrapper over the whole
FamilySearch API (205 operations in a committed spec snapshot, two of them excluded).
Each operation is a method, for example:

    from familysearch import FSClient
    from familysearch.api import FamilySearchApi

    api = FamilySearchApi(client)            # client: an FSClient
    person = api.read_person("ABCD-123")     # reads always run
    # createPerson is verified (on beta only): runs on beta as is, on production only
    # with allow_production=True. An unverified write needs allow_unverified=True (beta only).
    api.create_person(reason="why", body=payload)
    api.create_person(reason="why", body=payload, allow_production=True)  # production

Requires Python >= 3.11. Every method returns an `ApiResult` (status, headers, content
type, parsed body, text) for a 2xx answer. On a 4xx/5xx it raises `FSHTTPError` (or
`TokenExpiredError` for a 401) instead of returning the response.
The source of truth is `src/familysearch/api/` (generated: never edit it by hand) and
`src/familysearch/api/registry.json`.

### The lock

Reads always run. A write (anything that is not a read) is checked against the
registry before any network call and raises `OperationLockedError` unless permitted:

| Registry status | Meaning | Write on beta | Write on production |
|---|---|---|---|
| `verified` | proven on beta by a recipe, and the spec fingerprint still matches | yes | needs `allow_production=True` |
| `stale` (computed) | was verified, but the spec for it has since changed | needs `allow_unverified=True` | refused |
| `unverified` | never proven | needs `allow_unverified=True` | refused |
| `unverifiable` | cannot be proven safely; a reason is recorded | never | never |
| `excluded` | deliberately not offered; a reason is recorded | never | never |
| `unknown` (computed) | unrecognised registry data | never (fails closed) | never |

A destructive operation additionally needs `confirm_destructive=True`. The three flags are
`allow_unverified`, `allow_production` and `confirm_destructive`; each counts only when
it is exactly `True`.

**What the lock does NOT cover.** It guards only `FamilySearchApi`. The raw `FSClient`
(`client.get`, `client.post`, `client.request`, ...) is unlocked, and so are the
hand-written library modules and the MCP server's tools, which have their own guards
(see Guardrails). It also cannot prove a human approved a flag. Treat the flags as
decisions for a person to make. `FamilySearchApi(client, registry=...)` accepts a caller's
own registry, so the lock is a safety net against mistakes, not a security boundary.

### Verified means proven on beta, not production

`verified` means a recipe exercised the operation against FamilySearch **beta** and
read the resulting state back. It says nothing about production: no production write has
ever been exercised, which is why production still needs `allow_production=True` even
for a verified operation. Run the first production writes as a supervised pilot.

### Regenerating

    .venv/bin/python -m tools.spec.generate           # rewrite the generated code from the snapshot
    .venv/bin/python -m tools.spec.generate --check   # fail if the code on disk differs

New endpoints are added by changing the spec inputs (`tools/spec/snapshot.json`,
`tools/spec/overrides.json`) and regenerating, never by editing generated files.

### Verifying an operation

1. Write a recipe in `tools/verify/recipes/` (see `pilot.py`). A recipe lists the
   operations it proves, creates its own `ZZSPIKE` fixtures, and asserts on what a
   later read returns, never on a write's status code. State-change recipes also need
   a negative control.
2. Run it on **beta** with a fresh beta login in Brave (macOS Keychain prompt):

        .venv/bin/python -m tools.verify list
        .venv/bin/python -m tools.verify run --recipe NAME [--dry-run]
        .venv/bin/python -m tools.verify classify OPERATION_ID --status unverifiable --reason "why"

   The runner is beta only: it aborts unless beta accepts the session, never contacts
   production, and requires a clean git tree (apart from `registry.json`) so that the
   commit it records is the code that ran. It marks operations `verified` only when the
   recipe raised nothing, teardown was clean, every claimed operation was called and any
   negative control passed; otherwise `registry.json` is untouched. `classify` records an
   operation that cannot be verified safely as `unverifiable`, with a reason.
3. Commit the changed `registry.json`.

### Reading the coverage report

    .venv/bin/python -m tools.spec.coverage                  # counts per area and status, plus every pending operation
    .venv/bin/python -m tools.spec.coverage --require-done   # exit 1 while anything is pending

Anything that is not `verified`, or `unverifiable` / `excluded` with a non-blank recorded
reason, is pending (a `stale` operation counts as pending). An unreadable or invalid
registry makes the command exit 2 with a message on stderr. `--require-done` is the
completion gate.

### Verification status

As of the P3 tree-write verification (2026-10-26), `python -m tools.spec.coverage`
reports 205 operations: 102 `verified` (all on **beta only**), 97 `unverified`, 4
`unverifiable` and 2 `excluded`. Of the 107 reads, 63 are verified and 44 are deferred;
of the 60 Family Tree writes, 39 are verified. Those numbers change as recipes are run:
regenerate them with `python -m tools.spec.coverage` rather than trusting this paragraph.

The 39 verified Family Tree writes are the pilot's `createPerson`, `deletePerson` and
`restorePerson` (with `readPerson`; recipes `family_tree.person_create_read` and
`family_tree.person_delete_restore`) and the P3 Family Tree writes proven by the
`writes.tree.*` recipes: `updatePerson`, the person conclusion, source reference and
memory writes, the person / couple / child-and-parents note writes,
`createRelationship` with the relationship updates, deletes, couple restore and parent
removal, the couple / child-and-parents source reference create and delete and
conclusion delete, the two ordering operations (`updateCoupleRelationshipSpousesOrder`,
`updateChildAndParentsRelationshipParentsOrder`), and the P4 source writes
(`createSourceDescription`, `updateSourceDescription`, `deleteSourceDescription`,
`createSourceFolder`, `updateUserDefinedCollection`,
`updateSourceDescriptionsToCollection`, `deleteSourceDescriptionsFromCollections`,
`deleteUserDefinedCollection`). Because they are verified they can run on production, but only with
`allow_production=True` (plus `confirm_destructive=True` for the destructive ones),
even though they were proven on beta only. Every other generated write is still
unverified and locked: it needs `allow_unverified=True` and runs on beta only. The
other verified operations are reads, which always run. Each unverified Family Tree
write, with its reason and the tier that unblocks it (recipes awaiting their live run
are P3), is listed in `docs/superpowers/recon/2026-10-25-api-surface/p3-deferred.md`,
generated by `python -m tools.verify.deferred_report`.

### How reads are verified

Reads are proven by the read-case framework (`tools/verify/reads.py`; recipes
`reads_*.py` in `tools/verify/recipes/`), which holds a read to a stricter standard than
"it answered 200":

- **Fixture anchors.** A case names strings from the sealed, read-only `world` of
  `ZZSPIKE` fixtures (ids, names) that the request did not send, and the answer must
  contain them (in the body, or for HEAD in the headers). A case without a fixture
  anchor needs a recorded reason, as for pure standards computations.
- **A not-found control.** Each case is paired with a call that must be refused or
  differ (400/404/410). Where beta answers an unknown id with an empty 204 instead, the
  case must say so with a reason, and the empty 204 counts only because a populated
  positive call in the same recipe answered the anchored fixture.
- **Canary records for reference data.** Standards reads (places, vocabularies, dates)
  anchor on stable real reference records rather than fixtures, because beta search does
  not surface freshly created persons.
- **Eventual polling.** Where beta lags (relationship calculation, source references
  after an attach), the positive call is repeated a bounded number of times; an error
  status is never retried and the control is never polled.
- **HEAD and OPTIONS.** A HEAD is checked against its sibling GET (shared validators, or
  existence semantics where beta sends no validators); OPTIONS must answer a different
  `Allow` for a contrasting case.

A read that no sound case can prove is deferred, not guessed. The 50 deferred reads, each
with its reason and the later tier (P3 to P9) that unblocks it, are listed in
`docs/superpowers/recon/2026-10-25-api-surface/p2-deferred.md`, generated by
`python -m tools.verify.deferred_report` (`--check` fails when it is stale). Private
tree, Genealogies and group reads are deferred to P7: they need fixtures whose creation
is itself an unverified write.

## Running the tests

**Unit suite** (offline, no credentials; includes the MCP server tests):

    .venv/bin/python -m pytest tests/unit

**Integration suite** (live FamilySearch *beta*; needs a session token). It creates
and deletes clearly-labelled `ZZSPIKE` throwaway data. Without a token the whole
module is skipped. Always pass `--tb=short`: pytest's default traceback prints function
arguments, which once showed a session token. Log in to beta.familysearch.org in Brave first.

    FS_USE_BRAVE=1 .venv/bin/python -m pytest tests/integration --tb=short        # reads the fssessionid cookie from Brave (Keychain prompt)
    FS_BETA_TOKEN=<token> .venv/bin/python -m pytest tests/integration --tb=short # or supply the token directly

**MCP beta suite** (`tests/integration/test_mcp_beta.py`; drives the assembled MCP server
through the real FastMCP call path). Needs `FS_USE_BRAVE=1` (reads the Brave session;
macOS Keychain prompt) or `FS_BETA_TOKEN`, and a FRESH login to beta.familysearch.org. It
forces `FS_ENVIRONMENT=beta`, refuses to proceed unless `auth_status` reports beta, creates
`ZZSPIKE` throwaway persons, relationships, memories and sources on beta ONLY, and deletes
them at teardown through the library (there is no dedicated MCP delete tool; call_operation can delete on beta only, with confirm_destructive), failing loudly on any
leftover. Uploads use a temporary `FS_UPLOAD_ROOT` that the test creates itself. Add `-s`
to see the `[OBSERVED]` live-shape lines.

    FS_USE_BRAVE=1 .venv/bin/python -m pytest tests/integration/test_mcp_beta.py -v -s --tb=short

**Manual production read smoke** (no automated prod test): with a production login in
Brave, run the server with `FS_ENVIRONMENT=production` and call `auth_status`,
`search_tree` and `get_person`.
