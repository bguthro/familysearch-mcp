"""Read-only recon: fetch every API Reference page from the official FamilySearch
developer docs, extract the embedded OpenAPI definition, and write an inventory.
Public documentation only; polite concurrency; no FamilySearch API calls, no tokens."""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

S = Path(sys.argv[1])
(S / "ref").mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}

# 1. area -> list of (title, url) from llms.txt, only the "API Reference:" sections
area, entries = None, []
for line in (S / "llms.txt").read_text().splitlines():
    if line.startswith("## "):
        area = line[3:].strip()
        continue
    m = re.search(r"\[(.+?)\]\((https://developers\.familysearch\.org/main/reference/[^)]+\.md)\)", line)
    if m and area and area.startswith("API Reference:"):
        entries.append((area.replace("API Reference: ", ""), m.group(1), m.group(2)))
print(f"reference pages listed: {len(entries)}")


def fetch(e):
    area, title, url = e
    slug = url.rsplit("/", 1)[-1]
    f = S / "ref" / slug
    if not f.exists():
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code != 200:
            return e, None, f"HTTP {r.status_code}"
        f.write_text(r.text)
    return e, f.read_text(), None


ops, failures = [], []
with ThreadPoolExecutor(max_workers=4) as ex:
    for e, text, err in ex.map(fetch, entries):
        area, title, url = e
        if err:
            failures.append((title, url, err))
            continue
        m = re.search(r"```json\n(\{.*?\n\})\n```", text, re.S)
        if not m:
            failures.append((title, url, "no openapi block"))
            continue
        try:
            spec = json.loads(m.group(1))
        except Exception as ex2:  # noqa: BLE001
            failures.append((title, url, f"bad json: {ex2}"))
            continue
        servers = [s["url"] for s in spec.get("servers", [])]
        for path, methods in spec.get("paths", {}).items():
            for method, op in methods.items():
                if method.lower() not in ("get", "post", "put", "delete", "head", "patch", "options"):
                    continue
                params = op.get("parameters", [])
                body = op.get("requestBody", {})
                ops.append({
                    "area": area,
                    "title": title,
                    "method": method.upper(),
                    "path": path,
                    "operationId": op.get("operationId"),
                    "tags": op.get("tags"),
                    "summary": op.get("summary"),
                    "server": servers[0] if servers else None,
                    "path_params": [p["name"] for p in params if p.get("in") == "path"],
                    "query_params": [p["name"] for p in params if p.get("in") == "query"],
                    "header_params": [p["name"] for p in params if p.get("in") == "header"],
                    "accept": next((p.get("schema", {}).get("enum") for p in params if p.get("name") == "Accept"), None),
                    "request_media": sorted(body.get("content", {}).keys()) if body else [],
                    "responses": sorted(op.get("responses", {}).keys()),
                    "url": url,
                })

(S / "inventory.json").write_text(json.dumps(ops, indent=1))
print(f"operations extracted: {len(ops)}  failures: {len(failures)}")
for f in failures[:15]:
    print("  FAIL", f)
