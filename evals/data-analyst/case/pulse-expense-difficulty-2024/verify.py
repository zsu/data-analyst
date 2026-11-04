"""Case checker (stdlib only). Prints JSON, exit 0 = pass, 1 = fail.

  verify.py facts OUT TARGET TOL [--rel]      some numeric value in OUT/facts.json matches TARGET
  verify.py row OUT TARGET TOL TOKEN [--rel]  a line of OUT/exhibits/*.csv contains TOKEN and a matching number
  verify.py anyglob OUT PATTERN [PATTERN...]  at least one PATTERN (relative to OUT) matches a file

A value matches when it, or it times 100, 1e3, 1e6 or 1e9 (fraction vs percent, thousands, millions,
billions), is within TOL of TARGET (absolute, or relative with --rel).
"""
import glob, json, os, re, sys

NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def matches(x, target, tol, rel):
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


def main(a):
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    rel = "--rel" in a
    a = [x for x in a if x != "--rel"]
    mode, out = a[0], a[1]
    if mode == "facts":
        target, tol = float(a[2]), float(a[3])
        try:
            data = json.load(open(os.path.join(out, "facts.json"), encoding="utf-8"))
        except Exception as e:
            print(json.dumps({"passed": False, "why": "facts.json unreadable: %s" % e}))
            return 1
        hits = [x for x in leaves(data) if matches(x, target, tol, rel)]
        print(json.dumps({"passed": bool(hits), "target": target, "hits": hits[:5]}))
        return 0 if hits else 1
    if mode == "row":
        target, tol, token = float(a[2]), float(a[3]), a[4]
        found = []
        for f in glob.glob(os.path.join(out, "exhibits", "**", "*.csv"), recursive=True):
            try:
                lines = open(f, encoding="utf-8-sig", errors="replace").read().splitlines()
            except Exception:
                continue
            for ln in lines:
                if token in ln and any(matches(float(n), target, tol, rel) for n in NUM.findall(ln)):
                    found.append(os.path.basename(f) + ": " + ln[:200])
        print(json.dumps({"passed": bool(found), "target": target, "token": token, "rows": found[:3]}))
        return 0 if found else 1
    if mode == "anyglob":
        hit = {p: sorted(glob.glob(os.path.join(out, p), recursive=True))[:3] for p in a[2:]}
        ok = any(hit.values())
        print(json.dumps({"passed": ok, "matches": hit}))
        return 0 if ok else 1
    print(json.dumps({"passed": False, "why": "unknown mode " + mode}))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
