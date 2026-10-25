"""Snapshot the official FamilySearch OpenAPI definitions.

Each API Reference page of the public developer docs embeds an OpenAPI
definition. This tool fetches the index and every reference page (public docs
only, no token) and writes a deterministic ``snapshot.json`` that later tasks
generate code from.

    python -m tools.spec.harvest [--out PATH] [--check]
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import requests

INDEX_URL = "https://developers.familysearch.org/main/llms.txt"
DEFAULT_OUT = Path(__file__).resolve().parent / "snapshot.json"
SCHEMA_VERSION = 1
METHODS = ("get", "post", "put", "delete", "head", "patch", "options")
_REF_PREFIX = "API Reference: "
_GUIDE_TITLE = "API Reference Guide"
_ENTRY = re.compile(r"\[(.+?)\]\((https://developers\.familysearch\.org/main/reference/[^)]+\.md)\)")
_BLOCK = re.compile(r"# OpenAPI definition\s*```json\n(.*?)\n```", re.S)


def parse_index(llms_txt: str) -> list[tuple[str, str, str]]:
    """Return (area, title, url) for every operation page under an
    ``## API Reference: ...`` heading, excluding the API Reference Guide."""
    area = None
    entries = []
    for line in llms_txt.splitlines():
        if line.startswith("## "):
            area = line[3:].strip()
            continue
        if not area or not area.startswith(_REF_PREFIX):
            continue
        m = _ENTRY.search(line)
        if m and m.group(1) != _GUIDE_TITLE:
            entries.append((area[len(_REF_PREFIX):], m.group(1), m.group(2)))
    return entries


def _resolve(spec: dict, node: dict) -> dict:
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref or not ref.startswith("#/"):
        return node
    for part in ref[2:].split("/"):
        spec = spec[part]
    return spec


def _param(spec: dict, raw: dict) -> dict:
    p = _resolve(spec, raw)
    schema = _resolve(spec, p.get("schema", {}))
    example = p.get("example", schema.get("example"))
    return {
        "name": p["name"],
        "in": p["in"],
        "required": bool(p.get("required", False)),
        "type": schema.get("type"),
        "enum": schema.get("enum"),
        "example": example,
    }


def parse_reference_page(markdown: str, area: str) -> list[dict]:
    """Return one dict per operation in the page's embedded OpenAPI block."""
    m = _BLOCK.search(markdown)
    if not m:
        raise ValueError("no OpenAPI block")
    try:
        spec = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid OpenAPI JSON: {e}") from e
    servers = spec.get("servers") or []
    server = servers[0].get("url") if servers else None
    ops = []
    for path, item in spec.get("paths", {}).items():
        shared = item.get("parameters", [])
        for method, op in item.items():
            if method.lower() not in METHODS:
                continue
            raw_params = shared + op.get("parameters", [])
            body = _resolve(spec, op.get("requestBody", {}))
            tags = op.get("tags") or []
            ops.append({
                "operation_id": op.get("operationId"),
                "area": area,
                "tag": tags[0] if tags else None,
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary"),
                "description": op.get("description"),
                "server": server,
                "params": [_param(spec, p) for p in raw_params],
                "request_media": sorted(body.get("content", {}).keys()),
                "responses": {
                    str(code): _resolve(spec, r).get("description")
                    for code, r in sorted(op.get("responses", {}).items())
                },
            })
    return ops


def normalize(ops: list[dict]) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "source": INDEX_URL,
        "operations": sorted(ops, key=lambda o: (o["area"], o["path"], o["method"])),
    }


def harvest(fetch: Callable[[str], str]) -> dict:
    entries = parse_index(fetch(INDEX_URL))

    def load(entry):
        area, _title, url = entry
        try:
            ops = parse_reference_page(fetch(url), area)
        except ValueError as e:
            raise RuntimeError(f"{url}: {e}") from e
        if not ops:
            raise RuntimeError(f"{url}: no operations in OpenAPI block")
        return ops

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(load, entries))
    return normalize([op for ops in pages for op in ops])


def http_fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.text


def render(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def diff(old: dict, new: dict) -> list[str]:
    def key(o):
        return o["operation_id"]

    a = {key(o): o for o in old.get("operations", [])}
    b = {key(o): o for o in new.get("operations", [])}
    lines = [f"+ {k}" for k in sorted(b.keys() - a.keys())]
    lines += [f"- {k}" for k in sorted(a.keys() - b.keys())]
    lines += [f"~ {k}" for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="re-harvest and diff against the committed snapshot")
    args = ap.parse_args(argv)
    snapshot = harvest(http_fetch)
    if args.check:
        committed = json.loads(args.out.read_text())
        lines = diff(committed, snapshot)
        if lines or committed != snapshot:
            print("\n".join(lines) or "snapshot differs")
            return 1
        print(f"snapshot up to date ({len(snapshot['operations'])} operations)")
        return 0
    args.out.write_text(render(snapshot))
    print(f"wrote {len(snapshot['operations'])} operations to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
