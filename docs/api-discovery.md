# How the FamilySearch API endpoints were found

This library talks to FamilySearch through the *same* REST API the FamilySearch
website uses, authenticating as a logged-in browser session rather than as a
registered partner application (FamilySearch does not grant API keys for personal
projects). Every endpoint and request shape in this library was discovered and
**verified empirically against the FamilySearch beta environment** before being
coded. This document records the method so future endpoints can be added the same
way, with the same confidence.

> Golden rule: **never add an endpoint to this library that you have not watched
> return the expected status against beta.** The FamilySearch REST surface is
> undocumented for our purposes and can change without notice. Treat every response
> as untrusted data.

## 1. The environments

| Environment | Website | Purpose |
|---|---|---|
| **beta** | `https://beta.familysearch.org` | A separate, isolated copy of the system with its own accounts and an empty tree. **Safe to create/delete test data.** All discovery and the test suite run here. |
| **production** | `https://www.familysearch.org` | The real shared tree. Only touched by the sync engine, behind a dry-run + human approval + a supervised pilot. |

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
4. **Replay create + delete.** Create a clearly-labelled throwaway object, read it
   back, then delete it. Confirm the status codes and that beta is left clean.
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
| Couple relationship | `POST` the `relationships` rel | `{"relationships":[{"type":"http://gedcomx.org/Couple","person1":{resource,resourceId},"person2":{…}}]}` | 201 |
| Parent-child relationship | `POST` the `relationships` rel, one per parent | `{"relationships":[{"type":"http://gedcomx.org/ParentChild","person1":<parent>,"person2":<child>}]}` | 201 |
| Upload memory (document) | `POST` the `artifacts` rel | multipart `files={"artifact":(name,bytes,type)}` + `filename`/`title` params | 201 (id in `X-ENTITY-ID`) |
| Link memory → person | `POST /platform/memories/memories/{memId}/personas` | `{"persons":[{"id": personId}]}` | 204 |
| Create source description | `POST` the `source-descriptions` rel | `{"sourceDescriptions":[{"about":…,"titles":[{"value":…}],"citations":[{"value":…}]}]}` | 201 |
| Attach source → person | `POST /platform/tree/persons/{id}` (the person resource itself) | `{"persons":[{"sources":[{"description": <descUrl>}]}]}` | 200 |
| Delete person | `DELETE /platform/tree/persons/{id}` | — | 204 |

Every **write** carries a change message in the `X-Reason` header, and writes are
throttled (see `FSClient`).

## 6. Gotchas learned the hard way (each cost a wrong guess on beta)

- **Names need `parts`.** Creating a person whose name has only `fullText` and no
  `parts` (Given/Surname) silently fails — no id returned. `build_name` always emits
  parts.
- **Parent-child uses the generic `relationships` rel** with `type` `ParentChild`
  (one per parent). The hyphenated `/platform/tree/child-and-parents-relationships`
  path **404s** — do not use it.
- **Add-fact and attach-source POST to the *person resource itself***
  (`/platform/tree/persons/{id}`), not to a sub-path. `/persons/{id}/sources` is
  GET-only (`405`); `/persons/{id}/source-references` is `404`.
- **Linking a memory uses the memory's `personas` collection**
  (`/memories/{id}/personas`), not the person's `/persons/{id}/memories` link — that
  link is an *upload* endpoint and returns `400` for a link attempt.
- **Record search is not on the `/platform` tree API**; it is the website service
  `/service/search/hr/v2/personas`, with its own `results`/`entries`/`links.next`
  shape.

- **`q.birthLikeDate` accepts only a bare year (`1846`) or an ISO date
  (`1846-12-25`).** Natural-language dates ("25 December 1846", "abt 1850") return
  HTTP 400 ("Failure in upstream call: Search for tree persons"). Places in any form
  are fine. `Search.tree_search` normalizes via `normalize_search_date` (single
  distinct year -> that year; zero or several -> parameter omitted).
- **Tree-search `confidence` is a small integer.** Measured on beta 2026-10-24: junk
  names get 2 on every hit (fuzzy/phonetic noise); real existing people get 3-4. The
  MCP `create_person` match gate blocks only at confidence >= 3 (`MATCH_CONFIDENCE_MIN`).
- **The auth probe must send a browser `User-Agent`**: python-requests' default gets an
  HTTP 403 from the edge on both hosts.

## 7. Adding a new endpoint later — the recipe

1. Lift a fresh **beta** token (`extract_brave_token`, or copy it from DevTools).
2. Find the path from a **link rel** on the relevant collection/resource, or capture
   the beta website performing the action in DevTools → Network.
3. Probe it against beta with a throwaway object; confirm the status and body shape.
   On a 4xx, re-check the link rels rather than guessing paths.
4. Clean up any test object you created.
5. Add the method to the right module (`tree.py`, `memories.py`, `sources.py`, …),
   resolving the path via `Discovery` where a rel exists, and pass `reason` through to
   `X-Reason` for writes.
6. Add a case to the **beta integration suite** so the new endpoint stays proven, and
   record any new gotcha in §6 above.

Do experiments on **beta**. Only the sync engine writes to production, and only behind
a dry-run, explicit approval, and a supervised pilot.
