import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

S = Path(sys.argv[1])
ops = json.loads((S / "inventory.json").read_text())
print(f"total operations: {len(ops)}   distinct (method,path): {len({(o['method'], o['path']) for o in ops})}")
print("servers:", Counter(o["server"] for o in ops))

print("\n=== area x method ===")
areas = defaultdict(Counter)
for o in ops:
    areas[o["area"]][o["method"]] += 1
tot = Counter()
for a, c in sorted(areas.items(), key=lambda kv: -sum(kv[1].values())):
    print(f"  {sum(c.values()):4d}  {a:32s} " + "  ".join(f"{m}:{n}" for m, n in sorted(c.items())))
    tot.update(c)
print("  TOTAL by method:", dict(tot))
writes = sum(n for m, n in tot.items() if m in ("POST", "PUT", "DELETE", "PATCH"))
print(f"  reads (GET/HEAD): {tot['GET'] + tot['HEAD']}   writes (POST/PUT/DELETE/PATCH): {writes}")

print("\n=== Family Tree operations grouped by tag ===")
ft = defaultdict(list)
for o in ops:
    if o["area"] == "Family Tree Endpoints":
        ft[(o["tags"] or ["?"])[0]].append(o)
for tag, lst in sorted(ft.items(), key=lambda kv: -len(kv[1])):
    print(f"  {len(lst):3d}  {tag}")

print("\n=== path families (first two segments) ===")
fam = Counter("/".join(o["path"].split("/")[:3]) for o in ops)
for p, n in fam.most_common(40):
    print(f"  {n:3d}  {p}")

print("\n=== small areas in full ===")
for a in ("Authentication Endpoints", "Utilities Endpoints", "User Endpoints", "Standards Endpoints", "Memories Endpoints"):
    print(f"\n-- {a} --")
    for o in ops:
        if o["area"] == a:
            print(f"  {o['method']:6s} {o['path']:62s} {o['summary']}")
