from collections import defaultdict, Counter
import json

d = json.load(open("data/reference/courses-2026-2027.json"))

total = len(d)
types = defaultdict(Counter)
samples = defaultdict(list)

for rec in d:
    for k, v in rec.items():
        types[k][type(v).__name__] += 1
        if len(samples[k]) < 3 and v is not None:
            samples[k].append(v)

print(f"{total} records\n")

for k in sorted(types):
    n = sum(types[k].values())
    kinds = ", ".join(f"{t}×{c}" for t, c in types[k].most_common())
    ex = ", ".join(json.dumps(s)[:30] for s in samples[k])
    print(f"{k:<20} {100*n//total:>3}%  [{kinds}]")
    print(f"{'':<20}       e.g. {ex}")
print(max(c["avgRating"] for c in d), sum(1 for c in d if c["reviewCount"] > 0))