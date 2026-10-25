"""Deeper READ-ONLY recon on BETA (writes only labelled ZZSPIKE fixtures, all deleted with
read-back verification). For every documented GET operation, call it with REAL fixture ids
and EVERY documented Accept value; compare against the spec's documented status codes and
media types; record real response shapes. Never prints the token."""
import base64
import json
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from familysearch import (
    Discovery, Environment, FSClient, Memories, Sources, TreeReader, TreeWriter,
    build_fact, build_person, extract_brave_token,
)
from familysearch._urls import join_path
from familysearch.errors import FSHTTPError

S = Path(sys.argv[1])
R = "ZZSPIKE deep recon fixture (will be deleted)"
SUR = "ZZSPIKE" + uuid.uuid4().hex[:8]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

client = FSClient(extract_brave_token(), Environment.BETA)
disc = Discovery(client)
writer, reader, mem, srcs = TreeWriter(client, disc), TreeReader(client, disc), Memories(client, disc), Sources(client, disc)
BASE = client.base_url

ids, notes = {}, []          # fixture ids, and notes about fixture steps
created = {"persons": [], "couples": [], "cps": [], "memories": [], "sources": []}


def step(label, fn):
    try:
        v = fn()
        notes.append(f"OK    {label}")
        return v
    except Exception as e:  # noqa: BLE001
        notes.append(f"FAIL  {label}: {type(e).__name__} {str(getattr(e, 'body', e))[:140]!r}")
        return None


def gone(url):
    try:
        client.get(url)
        return False
    except FSHTTPError as e:
        return e.status in (404, 410)


try:
    # ---------------- fixtures (only calls already proven by read-back) ----------------
    def mk(given, gender, year):
        pid = writer.create_person(build_person(given=given, surname=SUR, gender=gender, living=False,
                                                facts=[build_fact("http://gedcomx.org/Birth", date=year)]), reason=R)
        created["persons"].append(pid)
        return pid

    ids["dad"] = step("create dad", lambda: mk("Dad", "Male", "1850"))
    ids["mom"] = step("create mom", lambda: mk("Mom", "Female", "1852"))
    ids["child"] = step("create child", lambda: mk("Child", "Male", "1875"))
    ids["couple"] = step("create couple dad-mom", lambda: writer.create_couple(ids["dad"], ids["mom"], reason=R))
    if ids["couple"]:
        created["couples"].append(ids["couple"])
    ids["cp"] = step("create child-and-parents", lambda: writer.create_parent_child(child_id=ids["child"], parent_ids=[ids["dad"], ids["mom"]], reason=R))
    if ids["cp"]:
        created["cps"].append(ids["cp"])
    ids["memory"] = step("upload memory to child", lambda: mem.upload_to_person(ids["child"], PNG, filename="zzspike.png", content_type="image/png", title="ZZSPIKE deep recon", reason=R))
    if ids["memory"]:
        created["memories"].append(ids["memory"])
    sd_base = disc.href("source-descriptions").split("?")[0]
    ids["source"] = step("create source description", lambda: srcs.create_description("https://example.org/zzspike-deep", "ZZSPIKE deep source", "ZZSPIKE citation", reason=R))
    if ids["source"]:
        created["sources"].append(ids["source"])
        step("attach source to child", lambda: srcs.attach_to_person(ids["child"], join_path(sd_base, ids["source"]), reason=R))

    def note_create():
        r = client.post(f"{BASE}/platform/tree/persons/{ids['child']}/notes", json={"persons": [{"notes": [{"subject": "ZZSPIKE note", "text": "ZZSPIKE note text"}]}]}, reason=R)
        loc = (r.headers.get("Location") or "").split("?")[0].rstrip("/")
        return loc.rsplit("/", 1)[-1] if loc else r.headers.get("X-ENTITY-ID")
    ids["note"] = step("create person note", note_create)

    # ids discovered by reading the fixtures back
    if ids["memory"]:
        pj = step("read memory personas", lambda: client.get(f"{BASE}/platform/memories/memories/{ids['memory']}/personas").json())
        if pj:
            ids["persona"] = (pj.get("persons") or [{}])[0].get("id")
    sr = step("read child source-references", lambda: client.get(f"{BASE}/platform/tree/persons/{ids['child']}/source-references"))
    if sr is not None and sr.text.strip():
        ids["srid"] = ((sr.json().get("persons") or [{}])[0].get("sources") or [{}])[0].get("id")
    cj = step("read child person", lambda: client.get(f"{BASE}/platform/tree/persons/{ids['child']}").json())
    if cj:
        ids["fact"] = ((cj.get("persons") or [{}])[0].get("facts") or [{}])[0].get("id")
    me = client.get("/platform/users/current").json()["users"][0]
    ids["uid"], ids["myperson"] = me["id"], me.get("personId")
    tc = step("read current tree id", lambda: client.get("/platform/trees/current").json())
    if isinstance(tc, dict):
        ids["tid"] = tc.get("treeId") or tc.get("id")
    print("FIXTURES:", {k: (v if k in ("uid",) else bool(v)) for k, v in ids.items()})
    for n in notes:
        print("  ", n)

    # ---------------- the pass ----------------
    inv = json.loads((S / "inventory.json").read_text())
    todo = [o for o in inv if o["method"] == "GET" and not re.match(r"^(create|update|delete|restore|merge|request)", o["summary"] or "", re.I)
            and not o["path"].startswith("/oauth2")]
    details = {}
    for o in todo:
        text = (S / "ref" / o["url"].rsplit("/", 1)[-1]).read_text()
        spec = json.loads(re.search(r"```json\n(\{.*?\n\})\n```", text, re.S).group(1))
        details[o["path"]] = spec["paths"][o["path"]]["get"]

    def bind(o, variant):
        """Return (url_path, query, real) - real=True if every path param came from a fixture/known id."""
        path, real = o["path"], True
        for p in details[o["path"]].get("parameters", []):
            if p.get("in") != "path":
                continue
            n, val = p["name"], None
            if n == "uid":
                val = ids.get("uid")
            elif n == "pid":
                if "/memories/memories/{mid}/personas/{pid}" in o["path"]:
                    val = ids.get("persona")
                elif o["path"].startswith("/tree/persons/") or o["path"].startswith("/tree/users/"):
                    val = ids.get(variant)
                elif o["path"].startswith("/places/"):
                    val = None
            elif n == "opid":
                val = ids.get("dad")
            elif n == "dpid":
                val = ids.get("mom")
            elif n == "caprid":
                val = ids.get("cp")
            elif n == "crid":
                val = ids.get("couple")
            elif n == "role":
                val = "parent1"
            elif n == "mid":
                val = ids.get("memory")
            elif n == "aid":
                val = "0"
            elif n == "sdid":
                val = ids.get("source")
            elif n == "srid":
                val = ids.get("srid")
            elif n == "nid":
                val = ids.get("note")
            elif n == "cid":
                val = ids.get("fact")
            elif n == "tid":
                val = ids.get("tid")
            elif n == "erid":
                val = ids.get("persona")
            if val is None:
                real, val = False, str(p.get("example", "X"))
            path = path.replace("{" + n + "}", str(val))
        query = {}
        for p in details[o["path"]].get("parameters", []):
            if p.get("in") == "query" and p.get("required"):
                n, ex = p["name"], p.get("example", "")
                if n in ("person",):
                    ex = ids.get("child") if o["path"].endswith("ancestry") else ids.get("dad")
                elif n == "pids":
                    ex = f"{ids.get('child')},{ids.get('dad')}"
                query[n] = ex
        return path, query, real

    results, shapes = [], {}

    def skel(x, d=0):
        if isinstance(x, dict):
            return {k: skel(v, d + 1) for k, v in list(x.items())[:14]} if d < 2 else "{...}"
        if isinstance(x, list):
            return [f"list[{len(x)}]", skel(x[0], d + 1)] if x else "list[0]"
        return type(x).__name__

    for o in todo:
        variants = ["child", "dad"] if (o["path"].startswith("/tree/persons/{pid}") and o["path"].count("{") == 1) else ["child"]
        documented = set(o["responses"])
        accepts = o["accept"] or [None]
        for vi, variant in enumerate(variants):
            path, query, real = bind(o, variant)
            for accept in (accepts[:4] if vi == 0 else accepts[:1]):
                try:
                    r = client.get("/platform" + path, accept=accept, params=query or None)
                    status, ctype, body = r.status_code, r.headers.get("content-type", ""), r
                    note = ""
                except FSHTTPError as e:
                    status, ctype, body = e.status, "", None
                    m = re.search(r'"message"\s*:\s*"([^"]{0,120})', re.sub(r"\s+", " ", str(e.body)))
                    note = m.group(1) if m else ""
                except Exception as e:  # noqa: BLE001
                    status, ctype, body, note = -1, "", None, type(e).__name__
                row = {"area": o["area"], "path": o["path"], "variant": variant, "real_ids": real, "accept": accept,
                       "status": status, "ctype": ctype.split(";")[0], "documented": sorted(documented), "note": note}
                results.append(row)
                if body is not None and body.text.strip() and "json" in ctype and (o["path"], variant) not in shapes:
                    try:
                        shapes[(o["path"], variant)] = skel(body.json())
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.15)
    (S / "deep.json").write_text(json.dumps(results, indent=1))
    (S / "shapes.json").write_text(json.dumps({f"{p} [{v}]": s for (p, v), s in shapes.items()}, indent=1))

    # ---------------- report ----------------
    print(f"\ncalls made: {len(results)} across {len(todo)} read operations")
    ok = [r for r in results if 200 <= r["status"] < 300]
    print("overall:", dict(sorted(Counter(r['status'] for r in results).items())))
    real_rows = [r for r in results if r["real_ids"]]
    print(f"calls with REAL fixture ids: {len(real_rows)}  -> 2xx: {sum(1 for r in real_rows if 200 <= r['status'] < 300)}")
    nofix = sorted({r["path"] for r in results if not r["real_ids"]})
    print(f"\nops with NO usable fixture (not verifiable here): {len(nofix)}")
    by = defaultdict(list)
    for p in nofix:
        by[next(r["area"] for r in results if r["path"] == p)].append(p)
    for a, ps in sorted(by.items()):
        print(f"  {a}: {len(ps)}")

    bad = [r for r in real_rows if not (200 <= r["status"] < 300)]
    print(f"\n=== FAILURES WITH REAL IDS ({len(bad)}) ===")
    seen = set()
    for r in bad:
        key = (r["path"], r["status"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  {r['status']}  [{r['variant']}] {r['path']:60s} accept={r['accept']}  {r['note'][:90]}")

    undoc = [r for r in real_rows if r["status"] not in (-1,) and str(r["status"]) not in r["documented"]]
    print(f"\n=== STATUS NOT IN THE SPEC'S DOCUMENTED CODES ({len(undoc)}) ===")
    seen = set()
    for r in undoc:
        key = (r["path"], r["status"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  got {r['status']}  documented={','.join(r['documented'])}  {r['path']}")

    rej = [r for r in real_rows if r["status"] in (406, 415)]
    print(f"\n=== DOCUMENTED Accept VALUE REJECTED ({len(rej)}) ===")
    for r in rej:
        print(f"  {r['status']}  accept={r['accept']}  {r['path']}")

    mism = [r for r in ok if r["accept"] and r["ctype"] and r["ctype"] != r["accept"]]
    print(f"\n=== 2xx WITH A DIFFERENT CONTENT-TYPE THAN REQUESTED ({len(mism)}) ===")
    seen = set()
    for r in mism:
        key = (r["path"], r["accept"], r["ctype"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  asked {r['accept']}  got {r['ctype']}  {r['path']}")
finally:
    print("\n=== CLEANUP (verified by read-back) ===")
    problems = []

    def verified(label, url, do_delete):
        try:
            do_delete()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{label}: delete error {type(e).__name__} {str(getattr(e, 'body', e))[:100]!r}")
            return
        if not gone(url):
            problems.append(f"{label}: STILL READABLE after delete")

    for cid in created["cps"]:
        verified(f"child-and-parents {cid}", f"{BASE}/platform/tree/child-and-parents-relationships/{cid}", lambda cid=cid: writer.delete_child_and_parents(cid, reason=R))
    for cid in created["couples"]:
        verified(f"couple {cid}", f"{BASE}/platform/tree/couple-relationships/{cid}", lambda cid=cid: writer.delete_couple(cid, reason=R))
    for mid in created["memories"]:
        verified(f"memory {mid}", f"{BASE}/platform/memories/memories/{mid}", lambda mid=mid: mem.delete(mid, reason=R))
    for sid in created["sources"]:
        u = join_path(disc.href("source-descriptions").split("?")[0], sid)
        verified(f"source {sid}", u, lambda u=u: client.delete(u, reason=R))
    for pid in created["persons"]:
        verified(f"person {pid}", f"{BASE}/platform/tree/persons/{pid}", lambda pid=pid: writer.delete_person(pid, reason=R))
    print("leftovers/problems:", problems or "none")
