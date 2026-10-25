import json
import re
import sys
from collections import defaultdict
from pathlib import Path

S = Path(sys.argv[1])
ops = json.loads((S / "inventory.json").read_text())

print("=== DESTRUCTIVE / HIGH-IMPACT operations (writes whose name implies merge, restore, delete, replace) ===")
risky = [o for o in ops if o["method"] in ("POST", "PUT", "DELETE") and
         re.search(r"merge|restore|delete|remove|replace|not a match|unmerge", (o["summary"] or ""), re.I)]
for o in risky:
    print(f"  {o['method']:6s} {o['area'][:12]:12s} {o['path']:64s} {o['summary']}")
print(f"  ({len(risky)} operations)")

for area in ("Genealogies Endpoints", "User Tree Endpoints"):
    print(f"\n=== {area} ===")
    for o in ops:
        if o["area"] == area:
            print(f"  {o['method']:6s} {o['path']:58s} {o['summary']}")

print("\n=== Family Tree: Matches / Change History / Discussions / Pedigrees / Search / Source Box (tag -> ops) ===")
ft = defaultdict(list)
for o in ops:
    if o["area"] == "Family Tree Endpoints":
        ft[(o["tags"] or ["?"])[0]].append(o)
for tag in ("Matches", "Change History", "Discussions", "Pedigrees", "Search", "Source Box", "Portraits"):
    print(f"\n-- {tag} --")
    for o in ft[tag]:
        print(f"  {o['method']:6s} {o['path']:62s} {o['summary']}")
