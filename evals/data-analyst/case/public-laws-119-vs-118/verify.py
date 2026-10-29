"""Check that a data-analyst run registered the expected numbers in <out>/facts.json.

    python -I verify.py <out-folder> --group <name>

Each target passes when any registered fact's numeric value (at one of the allowed scales) lies within
the tolerance. Prints JSON; exit 0 when every target of the group passes, 1 otherwise.
Kept in the case folder (never in files/), so the run under test cannot read it.
"""
import argparse
import glob
import json
import os
import re
import sys

# group -> [(label, target, abs_tol, scales)]; a fact value v matches when abs(v * s - target) <= tol
TARGETS = {'counts': [('119th public laws by 2026-09-30', 118, 0.0, [1]),
            ('118th public laws by 2024-09-30', 90, 0.0, [1])],
 'joint': [('119th joint resolutions enacted by 2026-09-30', 25, 0.0, [1]),
           ('118th joint resolutions by 2024-09-30', 5, 0.0, [1])],
 'pct': [('percent change 90 -> 118', 31.1, 0.15, [1, 100])]}
CSV_GROUPS = {'pagination_118': {'glob': 'data/congress/**/*.csv',
                    'label': 'distinct 118th public law numbers in data/congress CSVs',
                    'min': 274,
                    'regex': '\\b118-\\d{1,3}\\b'}}


def fact_values(out):
    path = os.path.join(out, "facts.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.values() if isinstance(data, dict) else data
    vals = []
    for rec in items:
        v = rec.get("value") if isinstance(rec, dict) else rec
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--group", required=True, choices=sorted(set(TARGETS) | set(CSV_GROUPS)))
    a = ap.parse_args()
    result = {"group": a.group, "out": a.out, "passed": [], "missing": []}
    if a.group in CSV_GROUPS:
        spec = CSV_GROUPS[a.group]
        found = set()
        for p in glob.glob(os.path.join(a.out, spec["glob"]), recursive=True):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    found.update(re.findall(spec["regex"], f.read()))
            except OSError:
                pass
        result["distinct"] = len(found)
        ok = len(found) >= spec["min"]
        (result["passed"] if ok else result["missing"]).append(f"{spec['label']}: {len(found)} >= {spec['min']}")
    else:
        try:
            vals = fact_values(a.out)
        except (OSError, ValueError) as e:
            print(json.dumps({"error": f"facts.json unreadable: {e}"}))
            sys.exit(1)
        for label, target, tol, scales in TARGETS[a.group]:
            hit = next((v for v in vals for s in scales if abs(v * s - target) <= tol), None)
            (result["passed"] if hit is not None else result["missing"]).append(
                {"label": label, "target": target, "tol": tol, "matched_value": hit})
    print(json.dumps(result, indent=1))
    sys.exit(0 if not result["missing"] else 1)


if __name__ == "__main__":
    main()
