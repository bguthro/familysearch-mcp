"""READ-ONLY reachability sweep on BETA: call every documented GET operation with the
spec's own example values (and our own person/user ids where they apply) and record the
HTTP status. Never calls POST/PUT/DELETE; skips GETs whose summary suggests a mutation.
Never prints the token."""
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from familysearch import Environment, FSClient, extract_brave_token
from familysearch.errors import FSHTTPError

S = Path(sys.argv[1])
ops = json.loads((S / "inventory.json").read_text())
client = FSClient(extract_brave_token(), Environment.BETA)

# our own ids on beta
me = client.get("/platform/users/current").json()["users"][0]
UID, MY_PID = me["id"], me.get("personId")
print(f"(using own user id and person id {MY_PID}; token not printed)")

# per-operation parameter details (example/required) from the cached reference pages
details = {}
for o in ops:
    f = S / "ref" / o["url"].rsplit("/", 1)[-1]
    m = re.search(r"```json\n(\{.*?\n\})\n```", f.read_text(), re.S)
    spec = json.loads(m.group(1))
    op = spec["paths"][o["path"]][o["method"].lower()]
    details[(o["method"], o["path"])] = op.get("parameters", [])

MUTATING = re.compile(r"^(create|update|delete|restore|merge|request)", re.I)
rows = []
todo = [o for o in ops if o["method"] == "GET" and not MUTATING.match(o["summary"] or "")
        and not o["path"].startswith("/oauth2")]
print(f"GET operations to sweep: {len(todo)} (skipped {sum(1 for o in ops if o['method']=='GET') - len(todo)})")

for o in todo:
    params = details[("GET", o["path"])]
    url_path = o["path"]
    for p in params:
        if p.get("in") != "path":
            continue
        name, ex = p["name"], str(p.get("example", "X"))
        if name == "uid":
            ex = UID
        elif name == "pid" and o["area"] in ("Family Tree Endpoints", "Genealogies Endpoints", "User Tree Endpoints") and MY_PID:
            ex = MY_PID
        url_path = url_path.replace("{" + name + "}", ex)
    query = {p["name"]: p.get("example", "") for p in params if p.get("in") == "query" and p.get("required")}
    accept = next((p["schema"]["enum"][0] for p in params if p.get("name") == "Accept" and p.get("schema", {}).get("enum")), None)
    t0 = time.time()
    try:
        r = client.get("/platform" + url_path, accept=accept, params=query or None)
        status, ctype, note = r.status_code, r.headers.get("content-type", ""), ""
    except FSHTTPError as e:
        status, ctype = e.status, ""
        msg = re.sub(r"\s+", " ", str(e.body))
        note = (re.search(r'"message"\s*:\s*"([^"]{0,110})', msg) or re.search(r"(.{0,100})", msg)).group(1)
    except Exception as e:  # noqa: BLE001
        status, ctype, note = -1, "", type(e).__name__
    rows.append({"area": o["area"], "tag": (o["tags"] or ["?"])[0], "path": o["path"], "summary": o["summary"],
                 "status": status, "ctype": ctype, "note": note})
    time.sleep(0.25)

(S / "sweep.json").write_text(json.dumps(rows, indent=1))
print("\n=== status by area ===")
by = defaultdict(Counter)
for r in rows:
    by[r["area"]][r["status"]] += 1
for a, c in sorted(by.items()):
    print(f"  {a:28s} " + "  ".join(f"{s}:{n}" for s, n in sorted(c.items())))
print("\n=== overall ===", dict(sorted(Counter(r["status"] for r in rows).items())))
print("\n=== reachable (2xx) ===", sum(1 for r in rows if 200 <= r["status"] < 300))
for st in (401, 403):
    sel = [r for r in rows if r["status"] == st]
    print(f"\n=== {st} (gated) : {len(sel)} ===")
    for r in sel:
        print(f"  {r['area'][:14]:14s} {r['path']:52s} {r['note']}")
for st in (400, 406, 500, 502, 503, -1):
    sel = [r for r in rows if r["status"] == st]
    if sel:
        print(f"\n=== {st} : {len(sel)} ===")
        for r in sel[:14]:
            print(f"  {r['area'][:14]:14s} {r['path']:52s} {r['note']}")
