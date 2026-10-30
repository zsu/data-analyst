"""Case checker (stdlib only). Prints JSON, exit 0 = pass, 1 = fail.

  verify.py facts OUT TARGET TOL [--rel] [--abs]  some numeric value in OUT/facts.json matches TARGET
  verify.py sens OUT MIN_ROWS                     OUT/exhibits/sensitivity.csv has at least MIN_ROWS rows
  verify.py basis OUT BASIS                       some assumption in OUT/decisions.json has that basis
  verify.py se OUT LO HI                          a standard error in [LO, HI]: a fact's "se" field, or a fact
                                                  named *_se / se_* or labelled "standard error"

A value matches when it, or it times 100, 1e3, 1e6 or 1e9 (fraction vs percent, thousands, millions,
billions), is within TOL of TARGET (absolute, or relative with --rel). --abs compares sizes, so a
coefficient registered as a positive "fall per point" still matches a negative target.
"""
import csv, json, os, sys


def matches(x, target, tol, rel, absval):
    if absval:
        x, target = abs(x), abs(target)
    lim = abs(target) * tol if rel else tol
    return any(abs(x * s - target) <= lim for s in (1, 100, 1e3, 1e6, 1e9))


def leaves(o):
    if isinstance(o, bool):
        return
    if isinstance(o, (int, float)):
        yield float(o)
    elif isinstance(o, dict):
        for v in o.values():
            yield from leaves(v)
    elif isinstance(o, list):
        for v in o:
            yield from leaves(v)


def load(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f), None
    except Exception as e:
        return None, "%s unreadable: %s" % (os.path.basename(path), e)


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    rel, absval = "--rel" in a, "--abs" in a
    a = [x for x in a if x not in ("--rel", "--abs")]
    mode, out = a[0], a[1]
    if mode == "facts":
        target, tol = float(a[2]), float(a[3])
        data, err = load(os.path.join(out, "facts.json"))
        if err:
            print(json.dumps({"passed": False, "why": err}))
            return 1
        hits = [x for x in leaves(data) if matches(x, target, tol, rel, absval)]
        print(json.dumps({"passed": bool(hits), "target": target, "hits": hits[:5]}))
        return 0 if hits else 1
    if mode == "sens":
        p = os.path.join(out, "exhibits", "sensitivity.csv")
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                rows = [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]
        except OSError as e:
            print(json.dumps({"passed": False, "why": str(e)}))
            return 1
        ok = len(rows) >= int(a[2])
        print(json.dumps({"passed": ok, "rows": len(rows), "parameters": [r.get("parameter") for r in rows][:10]}))
        return 0 if ok else 1
    if mode == "basis":
        data, err = load(os.path.join(out, "decisions.json"))
        if err:
            print(json.dumps({"passed": False, "why": err}))
            return 1
        got = [x.get("parameter") for x in (data or {}).get("assumptions", [])
               if str(x.get("basis", "")).lower() == a[2].lower()]
        print(json.dumps({"passed": bool(got), "basis": a[2], "parameters": got[:10]}))
        return 0 if got else 1
    if mode == "se":
        lo, hi = float(a[2]), float(a[3])
        data, err = load(os.path.join(out, "facts.json"))
        if err:
            print(json.dumps({"passed": False, "why": err}))
            return 1
        got = []
        for name, f in (data or {}).items():
            f = f if isinstance(f, dict) else {}
            named = (name.lower().endswith("_se") or name.lower().startswith("se_")
                     or "standard error" in str(f.get("label", "")).lower())
            for v in ([f.get("se")] + ([f.get("value")] if named else [])):
                if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= abs(v) <= hi:
                    got.append((name, v))
        print(json.dumps({"passed": bool(got), "range": [lo, hi], "hits": got[:5]}))
        return 0 if got else 1
    print(json.dumps({"passed": False, "why": "unknown mode " + mode}))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
