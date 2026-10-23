# FamilySearch MCP Server — Design

**Date:** 2026-10-23
**Status:** Draft for approval
**Author:** Ben Guthro (with Claude)
**Builds on:** the `familysearch` client library (this repo, `main`) and
`docs/superpowers/specs/2026-10-23-familysearch-sync-design.md`.

## Intent and why

Expose the `familysearch` library as an **MCP server** so that Claude — running in an
MCP client that already has filesystem access to Ben's Google Drive research archive —
can **interactively** take the research in a local person folder, **correlate** it with
what already exists in FamilySearch, and **create people / add facts / build
relationships / attach documents** where they're missing, with Ben in the loop for every
judgment call.

This deliberately **folds most of the previously-planned batch "sync engine" into
interactive use.** Genealogy matching is a human-judgment activity ("is this the same
August Anderson?"); doing it conversationally — Claude proposes, Ben decides, the tool
executes — fits better than an unattended batch program with a dry-run. A batch sync
engine remains possible later but is no longer the primary path.

### Proven before design
- The library's auth and reads/writes are proven on **beta** (8/8 integration suite).
- **Production is now verified read-only** (2026-10-23): bearer token + User-Agent alone
  authenticate against `www.familysearch.org` — **no bot-protection cookies
  (`reese84`/`cf_clearance`) are needed.** Confirmed by reading the current user and a
  real archive person (`GKLV-DY8`, Paul J. Anderson: 11 facts, 18 sources) and running a
  live tree search. So the server can target production with the library as-is.

## Goals

- A **Python MCP stdio server** (FastMCP / official MCP SDK) that wraps the library and
  exposes these tools:
  - **Read/search:** `auth_status`, `search_tree`, `search_records`, `get_person`,
    `get_relatives`, `get_person_sources`, `get_person_memories`. (`get_matches` —
    FamilySearch's own duplicate candidates for an existing person id — included if the
    library gains it; see "left to the builder".)
  - **Write:** `create_person`, `add_fact`, `create_couple`, `create_parent_child`,
    `upload_memory`, `link_memory`, `create_source`, `attach_source`.
  - **Not exposed:** `delete_person` (too easy to misfire from chat; deletion stays a
    manual/library action).
- **Environment:** targets **production by default**, overridable to **beta** for testing,
  via explicit config/env (`FS_ENVIRONMENT`). Never silently mixes environments (the
  library already binds a client to one environment and refuses off-origin requests); if
  `FS_ENVIRONMENT` and the Brave session disagree, the server **fails with a clear error**
  naming which environment Brave is actually signed into (never auto-switches).
- **Production writes are opt-in (Ben's decision).** Production **reads** are always
  available; production **write** tools stay disabled until Ben explicitly enables them
  for the session (an explicit switch — env/config flag or an `enable_writes`-style
  action), so the live shared tree can't be edited just because the server is running.
  (Beta writes need no such gate.)
- **Client:** **Claude Code** (which already has filesystem access to the archive). The
  deliverable includes its MCP config snippet.
- **Auth:** the server lifts the session token from Brave via the library
  (`extract_brave_token`) and **re-lifts on a 401/expiry**; it surfaces a clear,
  actionable message when the browser session itself has lapsed (and which environment
  Brave is actually signed into, via `detect_environment`).
- **Change messages:** every write carries an `X-Reason`. The tool accepts a `reason`
  and/or auto-composes one citing the archive (e.g. "added from Ben's research archive:
  <folder>"). Required, never blank (the library enforces this).
- **Balanced guardrails, enforced in the write tools** (the agreed design):
  1. **Search-before-create / anti-duplicate:** `create_person` runs a `tree_search` first
     and, whenever any plausible candidate exists, **does not create** — it returns the
     candidates for Ben to look at. Creation proceeds only with an explicit override
     (e.g. `create_despite_matches: true`). Rather than tune a brittle auto-threshold, the
     tool **surfaces candidates and lets Ben decide** — see the confirmation-authority
     note below. Duplicates in the shared tree are the top anti-goal. (`tree_search` is the
     pre-create gate; FamilySearch's `/matches` needs an existing person id, so it can't
     gate a brand-new person.)
  2. **Document provenance:** `upload_memory` **refuses** files whose provenance looks
     like a paywalled aggregator (Fold3, Ancestry, Newspapers.com, MyHeritage, Findagrave
     — detected by filename and/or a provenance hint the caller passes) unless explicitly
     overridden per file. Enforces the rights policy at the tool.
  3. **Living vs deceased routing:** `create_person` sets the `living` flag so living
     people are created **private** (FamilySearch keeps `living` persons out of the shared
     tree automatically) and deceased people go to the shared tree. It **pauses to confirm**
     only when a person is *possibly-living* (born within ~110 years, no recorded death)
     and the inputs don't settle it — never a refusal.
  - Otherwise tools are **thin**: search/read/add-fact/relationships/attach-source just do
    what they say. No dry-run, no batch approval — the human is already in the loop.
  - **Confirmation authority (Ben's decision):** the two consequential overrides —
    *create-despite-a-plausible-match* and *classify-a-possibly-living-person-as-deceased*
    — are **Ben's** calls, not Claude's. The norm Claude follows: surface the evidence (the
    candidate list, or the missing death date) and get Ben's explicit go-ahead **before**
    setting the override/confirmation flag. Claude does not self-approve these two. (Other
    steps need no such pause.) This is a behavioral contract carried in the server's tool
    descriptions / the client-side guidance, since an MCP call can't itself prove a human
    approved; the design intent is explicit and Claude is instructed accordingly.

## Non-goals

- **The batch sync engine** (unattended archive→tree with its own dry-run) — deferred,
  possibly never; interactive MCP use replaces it for now.
- **Reading the archive from the server** — the client has filesystem access; the server
  is purely the FamilySearch surface.
- **Deletion / destructive ops from chat** — `delete_person` and any merge are out.
- **Unattended/bulk automation** — this server is for human-in-the-loop work.

## Anti-goals (failure even if it runs)

- Creating **duplicate** persons in the shared tree.
- **Overwriting/deleting** another contributor's data (the library is additive-only; the
  server adds no overwrite path).
- Uploading **rights-restricted / aggregator-sourced** images.
- Putting a **living** person on the public shared tree.
- Getting Ben's account **flagged** (throttling + change messages are inherited from the
  library; the server adds no unthrottled bulk path).

## Constraints

- Token is short-lived (~60 min idle, 24 h max) and environment-scoped; the server
  re-lifts from Brave on expiry. On macOS the first lift per session may prompt the
  Keychain.
- Depends on the `familysearch` library in this repo; Python ≥ 3.11; stdio transport.
- Writes target **production** by default — real shared-tree data. The guardrails above
  are the safety model; there is no dry-run.
- Responses are untrusted data (the library already parses defensively).

## The "how" that is already decided (Ben's calls)

- The MCP server exposes the **full read + write surface** (not read-only).
- **Claude in the client reads the archive and orchestrates** correlation; the server is
  the FamilySearch tool layer.
- **Balanced guardrails** (search-before-create, aggregator-provenance refusal,
  living/deceased routing with confirm-on-ambiguous).
- **Living people are created private; deceased go to the shared tree.**
- Targets **production**.

## What's left to the builder

1. **Exact tool schemas** — names, parameters, return shapes (what each read tool
   projects: e.g. `get_person` returns name/lifespan/facts in a compact form vs raw
   GEDCOM X), and the JSON the client gets back. Keep returns compact and Claude-friendly.
2. **Search-before-create mechanics** — server-side match search parameters and the
   confidence threshold above which `create_person` refuses; the exact override
   parameter; whether near-misses are returned for Ben to pick from.
3. **Provenance detection for `upload_memory`** — the library already has a 15 MB/type
   guard; the server adds the aggregator check. Decide its inputs: filename patterns, plus
   an explicit caller-supplied provenance/source string (Claude has read the
   `ResearchSummary.md` citation), and the override parameter. Where provenance is
   unknown, return a "needs confirmation" result rather than uploading.
4. **Living/deceased input** — how the caller supplies birth/death (explicit `living`
   flag, or birth/death dates the tool classifies), and the exact "possibly-living"
   confirmation handshake (a structured result the client re-calls with a confirmation).
5. **Change-message composition** — required `reason` vs auto-composed from an archive
   reference the caller passes.
6. **A `get_matches` read tool** — FamilySearch exposes per-person duplicate candidates
   (`/persons/{id}/matches`); the library doesn't wrap it yet. Add it to the library (thin,
   same pattern as the other reads, beta+prod read-only — low risk) and expose it, since
   "does a match already exist?" is central to the correlate workflow.
7. **MCP framework specifics & packaging** — FastMCP vs low-level SDK; the server entry
   point; the client config snippet (Claude Code / Desktop); how `FS_ENVIRONMENT` and the
   token are provided.
8. **Error surfacing** — map the library's `TokenExpiredError` / `FSHTTPError` /
   `ValueError` guards to clear MCP tool errors the client can act on (re-auth prompt,
   match list, provenance refusal, possibly-living confirmation).

## Verification

- Unit tests for each tool's request/guard logic against a faked library (the guardrails —
  especially the create-refuses-on-match, aggregator-refusal, and possibly-living
  confirmation — are the highest-value tests).
- A **beta** end-to-end: drive the server (as an MCP client would) through
  search→create→add-fact→relationship→upload→link→attach against beta, then clean up —
  the same acceptance bar the library used.
- A **read-only production** smoke test (search + read a known person) to confirm the
  server speaks to the live tree.
- No write feature is trusted until its beta run passes; production writes happen only in
  the human-in-the-loop interactive flow.
