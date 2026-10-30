"""Case checker (stdlib only). Prints JSON, exit 0 = pass, 1 = fail.

  verify.py OUT --group estimated     a price elasticity of gasoline demand in [-1, 0) is a registered fact with
                                      its standard error (se), and analysis.py fits a model (a regression call)
  verify.py OUT --group compared      at least two opened references are used for the elasticity: their
                                      used_for names it, or an elasticity assumption or sensitivity row cites them
  verify.py OUT --group alternatives  exhibits/sensitivity.csv runs at least two alternatives for the demand
                                      response: an elasticity value, or a published estimate named by the
                                      first author of an opened reference

The checks read only what any run writes (facts.json, analysis.py, decisions.json, sensitivity.csv), not a
field that a later version of the skill may add, so the skill on main and a changed skill are graded alike.
"""
import csv, json, os, re, sys

ELASTIC = re.compile(r"(?i)elastic|tax.based")
RESPONSE = re.compile(r"(?i)elastic|tax.based|respon|salien")       # a sensitivity row about the demand response
FIT = re.compile(r"(?i)\b(?:polyfit|lstsq|linregress|WLS|IV2SLS|regress\w*|curve_fit|LinearRegression)\b"
                 r"|(?:\b|_)ols\b|linalg\.(?:solve|inv|pinv)\b")         # nw_ols, fit_ols, OLS; a hand-written fit
CITE = re.compile(r"\[@([A-Za-z0-9_-]+)")
NUM = re.compile(r"-?\d*\.\d+|-?\d+")


def load(path):
    try:
        return json.load(open(path, encoding="utf-8-sig"))
    except Exception:
        return None


def estimated(out):
    facts = load(os.path.join(out, "facts.json")) or {}
    hits = [k for k, f in facts.items() if isinstance(f, dict)
            and ELASTIC.search(k + " " + str(f.get("label") or ""))
            and isinstance(f.get("value"), (int, float)) and -1 <= f["value"] < 0
            and isinstance(f.get("se"), (int, float))]
    try:
        code = open(os.path.join(out, "analysis.py"), encoding="utf-8", errors="replace").read()
    except OSError:
        code = ""
    fit = sorted(set(FIT.findall(code)))
    return bool(hits) and bool(fit), {"elasticity_facts": hits[:5], "fit_calls": fit[:5]}


def compared(out):
    D = load(os.path.join(out, "decisions.json")) or {}
    refs = [r for r in D.get("references", []) if r.get("opened") is True]
    opened = {r.get("id") for r in refs}
    used = {r.get("id") for r in refs if ELASTIC.search(str(r.get("used_for") or ""))}
    for a in D.get("assumptions", []):
        if ELASTIC.search(str(a.get("parameter") or "") + " " + str(a.get("value") or "")):
            used |= set(CITE.findall(json.dumps(a, ensure_ascii=False))) & opened
    for r in _sens_rows(out):
        if ELASTIC.search(" ".join(str(v) for v in r.values())):
            used |= set(CITE.findall(" ".join(str(v) for v in r.values()))) & opened
    return len(used) >= 2, {"opened_refs_used_for_elasticity": sorted(used)}


def _sens_rows(out):
    try:
        return list(csv.DictReader(open(os.path.join(out, "exhibits", "sensitivity.csv"),
                                        encoding="utf-8-sig", newline="")))
    except OSError:
        return []


def alternatives(out):
    rows = _sens_rows(out)
    if not rows:
        return False, {"why": "no rows in exhibits/sensitivity.csv"}
    D = load(os.path.join(out, "decisions.json")) or {}
    authors = {m.group(1) for r in D.get("references", []) if r.get("opened") is True
               for m in [re.match(r"([A-Z][A-Za-z'-]+),", str(r.get("citation") or ""))] if m}
    alts = set()             # an elasticity value, or a published estimate named by its first author's surname
    for r in rows:
        text = " ".join(str(r.get(k) or "") for k in ("parameter", "base", "alternative"))
        if not RESPONSE.search(text):
            continue
        alt = str(r.get("alternative") or "")
        vals = {round(float(x), 3) for x in NUM.findall(alt) if -2 <= float(x) < 0}   # not a year or a count
        names = {a for a in authors if re.search(r"\b%s\b" % re.escape(a), alt)}
        alts |= vals or names
    return len(alts) >= 2, {"published_or_estimated_alternatives": sorted(map(str, alts))[:8]}


def main(a):
    if len(a) < 3 or a[1] != "--group":
        print(__doc__)
        return 1
    fn = {"estimated": estimated, "compared": compared, "alternatives": alternatives}.get(a[2])
    if fn is None:
        print(json.dumps({"passed": False, "why": "unknown group " + a[2]}))
        return 1
    ok, detail = fn(a[0])
    print(json.dumps({"passed": ok, **detail}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
