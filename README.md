# familysearch

Python client library for the FamilySearch API (`requests` only).

How the API endpoints were found, and how to add new ones: see [docs/api-discovery.md](docs/api-discovery.md).

## Running the tests

**Unit suite** (offline, safe for CI; no credentials):

    pytest tests/unit

**Integration suite** (live FamilySearch *beta*; needs a session token). It creates
and deletes clearly-labelled `ZZSPIKE` throwaway data. Without a token the whole
module is skipped.

    FS_USE_BRAVE=1 pytest tests/integration        # reads the fssessionid cookie from Brave (Keychain prompt)
    FS_BETA_TOKEN=<token> pytest tests/integration # or supply the token directly
