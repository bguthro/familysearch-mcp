# FamilySearch API surface — recon record (2026-10-25)

Read-only reconnaissance of the official FamilySearch API reference and of what a
browser-session token can reach on **beta**. Nothing here touched production. The only
writes were clearly-labelled `ZZSPIKE…` fixtures on beta, all deleted and read back as gone.

## What was done
1. `harvest.py` — read the official index (`https://developers.familysearch.org/main/llms.txt`),
   fetched every API Reference page, and extracted the **OpenAPI 3.0 definition embedded in each
   page** into `inventory.json` (one row per operation: method, path, parameters, accepted media
   types, documented status codes).
2. `analyze.py`, `offline.py` — counts and groupings of that inventory.
3. `sweep.py` — called every safe GET once with the spec's own example ids (+ our own ids) to see
   which areas the token can reach → `sweep.json`.
4. `deep.py` — created fixtures (3 persons, couple, child-and-parents, memory, source, note), called
   every safe GET with **real ids and every documented `Accept` value**, compared with the spec,
   recorded real response shapes (`shapes.json`, per-call results `deep.json`), then deleted the
   fixtures with read-back verification.

Re-run: `python harvest.py <scratch-dir>` (needs `llms.txt` saved in `<scratch-dir>`; `curl` it from
the URL above), then the other scripts take the same `<scratch-dir>` argument. `sweep.py` and
`deep.py` need a valid **beta** session in Brave and `PYTHONPATH=src`.

## The surface (205 operations)
| Area | Ops | | Area | Ops |
|---|---|---|---|---|
| Family Tree | 117 | | User Trees | 14 |
| Standards (places, dates, names, vocab) | 26 | | User | 8 |
| Genealogies (private trees) | 21 | | Authentication | 2 |
| Memories | 16 | | Utilities | 1 |

By method: GET 96, POST 60, DELETE 37, HEAD 9, PUT 2, OPTIONS 1 → ~105 reads, ~99 writes/deletes.
43 operations are high-impact (delete, merge, restore). The spec points at `apibeta`/`identbeta`
hosts; our cookie token works against `beta.familysearch.org` / `www.familysearch.org`.

## Findings
* **Reachability:** with placeholder ids, 54 of 94 safe GETs returned 2xx; **zero 401s** in any area;
  one 403 (`/users/current/partner-eligible`, a partner-only feature). Auth is not what limits us.
* **Fixture-backed pass:** 339 calls over 94 read operations; 251 used real fixture ids → **211 (84%)
  succeeded**. **0** documented `Accept` values were rejected; **0** content-type mismatches.
* **The spec is reliable about requests** (paths, methods, parameters, media types) **but documents no
  response bodies:** only 6 of 96 GET operations carry a response schema and none carries an example.
  Bodies are described in prose linking to separate guides.
* **Spec defects found (verified against the spec text and the live service):**
  * `GET`/`PUT /tree/users/{uid}/preferred-parent-relationships/{pid}` are listed with a **doubled,
    malformed path** (`…/{pid}/tree/users/{uid}/preferred-parent-relationships/{pid}`).
  * `GET /tree/persons` — `pids` is marked optional but the server returns 400 without it.
  * Several list endpoints return **`204 No Content`** when empty; the spec documents only `200`.
  * `current-person` and `sources/collections` document only a redirect (303/307) as their success.
  * Genealogies ids need a special prefix (`Invalid prefix for genealogies: (…)`); undocumented.
  * `GET /tree/search` and `GET /tree/source-references` have only optional parameters but 400/404 with none.
* **28 read operations could not be exercised** (no usable fixture): mostly the private-tree areas
  (Genealogies, User Trees, groups), a few Family Tree and Standards operations.
* **Writes were not tested by the recon** beyond the known-good fixture calls.

## Why this matters
Two writes in our hand-built library once returned success codes while doing the wrong thing
(parent-child created spouse links; memory link did nothing). **A 2xx proves nothing — every write
must be proven by reading the resulting state back.**
