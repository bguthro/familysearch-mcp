# How the FamilySearch API endpoints were found

This library talks to FamilySearch through the *same* REST API the FamilySearch
website uses, authenticating as a logged-in browser session rather than as a
registered partner application (FamilySearch does not grant API keys for personal
projects). Every endpoint and request shape in this library was discovered and
**verified empirically against the FamilySearch beta environment** before being
coded. This document records the method so future endpoints can be added the same
way, with the same confidence.

> **Methodology rule: a 2xx proves nothing.** Every write endpoint is proven only by
> READING THE RESULTING STATE BACK (e.g. fetch the child's parents; fetch the person's
> memory list). The original parent-child and memory-link entries returned 2xx and
> were wrong (see "Corrections" below).

> Golden rule: **never add an endpoint to this library that you have not watched
> return the expected status against beta.** The FamilySearch REST surface is
> undocumented for our purposes and can change without notice. Treat every response
> as untrusted data.

> **Generated API.** The whole API surface is now also available as the generated,
> lock-guarded `FamilySearchApi`; see the "Generated API" section of the
> [README](../README.md) for the lock, the registry statuses and how to verify an
> operation. **New endpoints are added by regenerating (`python -m tools.spec.generate`
> after updating the spec inputs), not by hand.** The manual recipe in section 7 applies
> to the hand-written library modules only.

## 1. The environments

| Environment | Website | Purpose |
|---|---|---|
| **beta** | `https://beta.familysearch.org` | A separate, isolated copy of the system with its own accounts and an empty tree. **Safe to create/delete test data.** All discovery and the test suite run here. |
| **production** | `https://www.familysearch.org` | The real shared tree. Written to only by the sync engine (behind a dry-run + human approval + a supervised pilot) or by the MCP server when `FS_ALLOW_PROD_WRITES` is set. **No production write has been exercised yet**; every endpoint here was proven on beta only. |

Environments are isolated: a beta session token authenticates beta only (it returns
`401` against production) and vice-versa. See `familysearch/auth.py`.

## 2. Authentication — lifting the session token

The website stores its session token in the `fssessionid` cookie on
`.familysearch.org`. That value works directly as an OAuth bearer token against the
REST API. We read it straight from the browser's cookie store (Brave) with
`browser_cookie3` — see `familysearch/auth.py::extract_brave_token`. On macOS this
triggers a Keychain prompt ("Brave Safe Storage") the first time.

Caveats, all learned empirically:

- The token is short-lived (~60 min idle, 24 h max). On `401`, re-extract from the
  browser; a refresh token is not available without client credentials.
- Brave holds a **single** `.familysearch.org` `fssessionid`, owned by whichever
  environment was signed into last. There is no cookie-level environment selection —
  we validate at the HTTP layer with `auth.detect_environment`.
- **Every request must send a browser-like `User-Agent`** or the edge rejects it.
  `FSClient` sets one on every call (`familysearch/client.py`).
- Reads use `Accept: application/x-fs-v1+json`; tree search uses
  `application/x-gedcomx-atom+json`.

To lift a token by hand (e.g. for the integration suite or a manual probe): in the
browser's DevTools → Network, open any request to a `familysearch.org` host and copy
the `authorization: Bearer <…>` value, or read the `fssessionid` cookie from
DevTools → Application → Cookies.

## 3. Discovery — the API describes itself (HATEOAS)

The API is hypermedia-driven. Rather than hard-code paths, resolve them from link
rels the server advertises:

- `GET /platform/collections/tree` returns a `links` map (under `collections[0].links`
  or top-level `links`) with rels such as `persons`, `relationships`, `artifacts`
  (→ memories), `source-descriptions`, `person-search`, `matches`, and the OAuth
  `token` endpoint. See `familysearch/discovery.py::Discovery.links`.
- `GET /platform/tree/persons/{id}` returns that person's own `links` map with rels
  such as `artifacts` (→ `/persons/{id}/memories`), `source-descriptions`
  (→ `/persons/{id}/sources`), `parents`, `children`, `spouses`, `change-history`,
  `matches`, `merge`. See `Discovery.person_links`.

A link value may carry `href` or a URI-template `template` (strip `{…}` suffixes
before adding your own query params — see `familysearch/search.py`).

The **only** paths this library hard-codes, by necessity, are:
`/platform/collections/tree` (the discovery root), `/platform/users/current` (the
bootstrap identity/environment check — it cannot be discovered before you are
authenticated), and `/service/search/hr/v2/personas` (the historical-record search
service, which lives outside the HATEOAS tree collection). Everything else is resolved.

## 4. The method used to verify each endpoint

For every capability, on beta, with a freshly lifted token:

1. **Prove the token reads.** `GET /platform/users/current` → `200` with your user.
2. **Try the expected endpoint.** Start from the link rel (§3) or the FamilySearch
   developer docs, and make the real call against beta.
3. **If it 404s / 405s, don't guess repeatedly — ask the API.** Dump the relevant
   `links` map (tree collection, or the resource you're acting on) and use the rel it
   actually advertises. Several of our first guesses were wrong and the link rels gave
   the right path (see "gotchas" below). As a last resort, capture the beta website's
   own request in DevTools → Network while performing the action in the UI, and
   replay it — the website's traffic is the authoritative spec.
4. **Replay create + delete, and READ THE STATE BACK.** Create a clearly-labelled
   throwaway object, then fetch the resulting state through a *different* read
   (the child's `parents`/`spouses` links; the person's memory list) and confirm it
   is what you intended. A 2xx is not proof. Then delete it and confirm the status
   codes and that beta is left clean.
5. **Codify** the proven host/path/body/headers/status into the library, and add a
   case to the beta integration suite (`tests/integration/`) so it stays proven.

## 5. Proven endpoint catalogue (verified on beta)

| Capability | Method + path (resolve the path part via a link rel where possible) | Body (envelope) | Success |
|---|---|---|---|
| Current user | `GET /platform/users/current` | — | 200 |
| Read person | `GET /platform/tree/persons/{id}` | — | 200 |
| Tree search | `GET` the `person-search` rel | `q.givenName`, `q.surname`, `q.birthLikeDate`, `q.birthLikePlace` params; `Accept: …atom+json` | 200 |
| Record (document) search | `GET /service/search/hr/v2/personas` | `q.*` params; paginated via `links.next.href` | 200 |
| Create person | `POST` the `persons` rel | `{"persons":[<person>]}` | 201 (id in `X-ENTITY-ID`) |
| Add fact to existing person | `POST /platform/tree/persons/{id}` (the person resource itself) | `{"persons":[{"facts":[<fact>]}]}` | 200 |
| Couple relationship | `POST` the `relationships` rel (the creation collection ONLY; the created resource is `<base>/platform/tree/couple-relationships/{id}`, per the `Location` header: GET 200, DELETE 204) | `{"relationships":[{"type":"http://gedcomx.org/Couple","person1":{resource,resourceId},"person2":{…}}]}` | 201 |
| Parent-child relationship | `POST` the `relationships` rel, ONE request holding BOTH parents | `{"childAndParentsRelationships":[{"parent1":{resource,resourceId},"parent2":{…},"child":{…}}]}` (omit `parent2` for a single parent; `resource` URIs are clean, no query string) | 201 (id in `X-ENTITY-ID`; `Location` is `<base>/platform/tree/child-and-parents-relationships/{id}`). Read-back: the child's `parents` link lists both parents; its `spouses`/`children` are 204; the two parents list each other as `spouse` (FamilySearch derives the couple from a two-parent relationship) |
| Delete couple relationship | `DELETE <base>/platform/tree/couple-relationships/{id}` (not a discovery rel; `<relationships rel>/{id}` is 404 for GET and DELETE) | — | 204; afterwards the person's `spouses` link is 204 (empty) |
| Delete child-and-parents relationship | `DELETE <base>/platform/tree/child-and-parents-relationships/{id}` (not a discovery rel; learned from the POST's `Location`) | — | 204 (`<relationships rel>/{id}` is 404 for these ids) |
| Upload memory (standalone, "floating") | `POST` the `artifacts` rel | multipart `files={"artifact":(name,bytes,type)}` + `filename`/`title` params | 201 (id in `X-ENTITY-ID`) |
| Upload memory AND attach to a person (one step) | `POST` the PERSON's own `artifacts` link (`person_links(pid)["artifacts"]`, `…/persons/{pid}/memories?flag=fsh`) | same multipart as above | 201 (id in `X-ENTITY-ID`; `Location` is `…/persons/{pid}/memory-references/{memId}-{n}`). Read-back: GET that link lists the memory |
| Attach an EXISTING memory to another person | `POST <base>/platform/memories/memories/{memId}/personas` | `{"persons":[{"media":[{"description":"<base>/platform/memories/memories/{memId}"}],"identifiers":{"http://familysearch.org/v1/FamilyTreePerson":["<base>/ark:/61903/4:1:{PID}"]}}]}` | 201 (persona id `{memId}-{n}` in `X-ENTITY-ID`). Read-back: both persons' memory lists contain the one memory |
| Person matches | `GET` the person's `matches` rel with `Accept: application/x-gedcomx-atom+json` | — | 200 with `entries`, or 204 with no body when none (`x-fs-v1+json` and `x-gedcomx-v1+json` give 406). Entries (verified live on a pre-existing person) carry `confidence` (up to 5), `content.gedcomx.persons[0].display`, `matchInfo` and `score` |
| Create source description | `POST` the `source-descriptions` rel | `{"sourceDescriptions":[{"about":…,"titles":[{"value":…}],"citations":[{"value":…}]}]}` | 201 |
| Attach source → person | `POST /platform/tree/persons/{id}` (the person resource itself) | `{"persons":[{"sources":[{"description": <descUrl>}]}]}` | 200 |
| Delete person | `DELETE /platform/tree/persons/{id}` | — | 204 |

Every **write** carries a change message in the `X-Reason` header, and writes are
throttled (see `FSClient`).

## 6. Gotchas learned the hard way (each cost a wrong guess on beta)

- **`living` is an explicit boolean on persons** (deceased -> `False` with a lifespan
  ending "Deceased"; living -> `True`). The MCP tools use it: documents are NEVER
  attached to living or unknown-status persons (`living_person_blocked`; policy:
  documents tied to deceased persons only).

- **Names need `parts`.** Creating a person whose name has only `fullText` and no
  `parts` (Given/Surname) silently fails — no id returned. `build_name` always emits
  parts.
- **Parent-child is a child-and-parents relationship**, POSTed to the generic
  `relationships` rel with a `childAndParentsRelationships` body (both parents in one
  request). Posting `type` `ParentChild` to that rel returns 201 but creates COUPLE
  links (see Corrections). The hyphenated
  `/platform/tree/child-and-parents-relationships/{id}` path is where such a
  relationship lives (the `Location` header) and is the DELETE path; POSTing to it
  404s.
- **Add-fact and attach-source POST to the *person resource itself***
  (`/platform/tree/persons/{id}`), not to a sub-path. `/persons/{id}/sources` is
  GET-only (`405`); `/persons/{id}/source-references` is `404`.
- **Two ways to attach a memory.** (a) Upload through the PERSON's own `artifacts`
  link: that multipart POST uploads and attaches in one step. (b) Attach an existing
  memory by POSTing a persona (ark + `media.description`) to
  `/memories/{id}/personas`; `{"persons":[{"id": …}]}` there returns 204 and does
  nothing. The person's `/persons/{id}/memories` link is a *list* (GET) and an
  *upload-and-attach* (multipart POST) endpoint; it returns `400` for a JSON link.
- **Record search is not on the `/platform` tree API**; it is the website service
  `/service/search/hr/v2/personas`, with its own `results`/`entries`/`links.next`
  shape.

- **`q.birthLikeDate` accepts only a bare year (`1846`) or an ISO date
  (`1846-12-25`).** Natural-language dates ("25 December 1846", "abt 1850") return
  HTTP 400 ("Failure in upstream call: Search for tree persons"). Places in any form
  are fine. `Search.tree_search` normalizes via `normalize_search_date`: a bare year or
  VALID ISO date passes; bound/range qualifiers (after, before, from, to, until, since,
  between) and decades ("1850s") omit the parameter because a single year would NARROW
  the search; approximations ("abt 1850") use the single year; zero or several years omit
  it; non-ASCII digits are not years.
- **Tree-search `confidence` is a small integer.** Measured on beta 2026-10-24: junk
  names get 2 on every hit (fuzzy/phonetic noise); real existing people get 3-4
  (values from 2 to 5 have been observed). The
  MCP `create_person` match gate blocks only at confidence >= 3 (`MATCH_CONFIDENCE_MIN`).
- **The auth probe must send a browser `User-Agent`**: python-requests' default gets an
  HTTP 403 from the edge on both hosts.

## 6a. Corrections (2026-10-24)

The earlier catalogue entries for **parent-child** (`relationships` + type
`ParentChild`, one per parent) and **memory link** (`{"persons":[{"id":…}]}` to
`/memories/{id}/personas`) were WRONG. Both returned success (201 / 204), but a live
read-back on beta showed the parent-child POST created COUPLE links between the child
and each parent (child: parents `[]`, spouses `[dad, mom]`), and the memory link
created nothing (the person's memory list stayed empty). Both were corrected after
that read-back; `delete_couple` was corrected the same way on 2026-10-24 (the couple
resource is `/platform/tree/couple-relationships/{id}`); `get_matches` (HTTP 406 without
the atom `Accept`) was fixed in the same pass. The unit tests passed throughout because they only asserted the request we
sent, never the resulting state: hence the methodology rule at the top.

## 6b. Teardown must verify deletion

Teardown must VERIFY deletion by read-back, never blanket-tolerate 404: a tolerated 404
hid a wrong delete URL (`delete_couple` hit `<relationships rel>/{id}`, which is 404;
the suites stayed green because deleting the persons cascaded). The live trackers GET
relationship/memory/source/person first (it must be readable), DELETE (non-2xx is an
error), and GET again, requiring 404/410. Source descriptions are verified the same way (GET with the default
Accept works: 200, DELETE 204, then GET 410). A pre-delete 404 on an object created in
the same run is itself an ERROR (wrong URL or vanished), not "already gone".

## 7. Adding a new endpoint later — the recipe

1. Lift a fresh **beta** token (`extract_brave_token`, or copy it from DevTools).
2. Find the path from a **link rel** on the relevant collection/resource, or capture
   the beta website performing the action in DevTools → Network.
3. Probe it against beta with a throwaway object; confirm the status and body shape.
   On a 4xx, re-check the link rels rather than guessing paths.
4. **Read the resulting state back** with an independent GET (parents, spouses,
   memory list, ...) and confirm it matches your intent. A 2xx alone is not proof.
5. Clean up any test object you created (for relationships, use the delete path that
   matches the kind).
6. Add the method to the right module (`tree.py`, `memories.py`, `sources.py`, …),
   resolving the path via `Discovery` where a rel exists, and pass `reason` through to
   `X-Reason` for writes.
7. Add a case to the **beta integration suite**, WITH the read-back assertion, so the
   new endpoint stays proven, and record any new gotcha in §6 above.

Do experiments on **beta**. Production is written to only by the sync engine (behind a
dry-run, explicit approval and a supervised pilot) or by the MCP server when
`FS_ALLOW_PROD_WRITES` is set. No production write has been exercised yet: run the
first ones as a supervised pilot WITH read-back, because several endpoints that looked
proven on beta (parent-child, memory link, couple delete) turned out to be wrong.
