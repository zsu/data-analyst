"""Registry of every number cited in an answer or report (pandas). Written to <out>/facts.json; the report
builder fills {{name}} / {{name|fmt}} placeholders from it and the audit traces each value to its cell.

    from facts import Facts, save_exhibit
    F = Facts(OUT)                                      # OUT = the analysis folder (holds data/<id>/)
    save_exhibit(ex1, OUT, "exhibits/exhibit_1.csv", packs=["bls"])   # CSV + its data lineage
    F.add("unemp_latest", ex1, where={"period": "Sep 2026"}, column="rate", source="exhibits/exhibit_1.csv",
          fmt="pct100:1", label="Unemployment rate, September 2026", preliminary=True)
    F.add("gap_ratio", value=a / b, derivation="unemp_latest / payroll_growth", fmt="ratio:2", pack=["bls", "bea"])
    F.save()

A table fact is read back from the saved CSV: the registered value is the cell a reader finds there, and a
table value that differs from the cell beyond its printed precision raises (save the CSV from the same table
first). A derived fact needs value= and derivation=. NaN values (gaps in the data) are refused.

Each record holds label, fmt, value, source = the CSV path, where, column, derivation, se, and
the flags preliminary / vintage / projection, which the audit requires the prose to state) plus:
  pack   the source pack(s) the number comes from (the data/<id>/ folder): the pack= argument; else the
         Facts(out, pack=) default; else the folder when the CSV lies under data/<id>/; else the exhibit's
         lineage (save_exhibit / lineage(), or table.attrs["packs"]); else the only data/<id>/ folder in <out>.
         When <out> holds several sources and none of these says which, add() raises. A CSV under
         published/ (read from an agency page) gets no pack.
  text   (on save) the value as the prose prints it in its default format.
The record key "source" is the CSV path; the source pack is "pack".
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation

import numpy as np
import pandas as pd

csv.field_size_limit(2**31 - 1)   # agency CSV cells can exceed the 128 KiB default (see core/__init__.py)

try:
    from factfmt import format_value
except ImportError:                                  # imported as a package module
    from .factfmt import format_value  # type: ignore

LINEAGE = os.path.join("exhibits", "lineage.json")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _csv_cell(path: str, where: dict, column: str, label: str) -> tuple[float, str]:
    """The one cell of `column` in the row matching `where`, read the way audit_report.py reads it."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        cols = rd.fieldnames or []
        rows = [r for r in rd if all(str(r.get(k)) == str(v) for k, v in where.items())]
    if column not in cols:
        raise ValueError(f"{label}: column {column!r} is not in {os.path.basename(path)}; its columns are {cols}")
    if len(rows) != 1:
        raise ValueError(f"{label}: {len(rows)} rows of {os.path.basename(path)} match {where} (need exactly 1; "
                         f"the CSV's own text must equal each value, e.g. 2026 vs 2026.0)")
    text = (rows[0][column] or "").strip()
    try:
        return float(text), text
    except ValueError:
        raise ValueError(f"{label}: {column} = {text!r} in {os.path.basename(path)} is not a number") from None


def _printed_precision(text: str) -> float:
    """Half a unit of the last printed digit ("1992.47" -> 0.005), the rounding a saved CSV may carry."""
    try:
        return float(0.5 * 10 ** Decimal(text).as_tuple().exponent)
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def rounding_tie(cell: float, text: str, fmt: str) -> bool:
    """Is a value stored exactly one digit past what its format prints, ending in 5 ("21.55" printed as "21.6")?
    Then the printed figure depends on digits the CSV dropped (21.545 rounds the other way) unless the value is
    exact at that precision (a figure published so). audit_answer.py requires one or the other to be said."""
    try:
        shown = re.search(r"-?\d[\d,]*(?:\.(\d+))?", format_value(cell, fmt))
        if not shown or not cell or "." not in text:
            return False
        disp = float(shown.group(0).replace(",", ""))
        k = round(math.log10(abs(disp / cell))) if disp else 0
        return len(text.split(".")[1].rstrip()) - k == len(shown.group(1) or "") + 1 and text.rstrip().endswith("5")
    except (ValueError, ZeroDivisionError, OverflowError):
        return False


def _warn_rounding_tie(name: str, cell: float, text: str, fmt: str) -> None:
    if rounding_tie(cell, text, fmt):
        print(f"note: fact {name}: {text} in the CSV sits on a rounding tie for its format: store full precision "
              "(no .round() before saving), or, when the value is exact at that precision (published so), list it "
              "in decisions.json \"rounding_ties_ok\" with why; the answer audit fails otherwise", file=sys.stderr)


def _packs(x) -> list[str]:
    if x is None or x == "" or x == []:
        return []
    vals = [x] if isinstance(x, str) else list(x)
    bad = [v for v in vals if not _ID.fullmatch(str(v))]
    if bad:
        raise ValueError(f"source pack ids look like 'bls' or 'treasury_yields', not {bad}")
    return list(dict.fromkeys(str(v) for v in vals))


def read_lineage(out_dir: str) -> dict:
    p = os.path.join(out_dir, LINEAGE)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def lineage(out_dir: str, rel_path: str, packs) -> dict:
    """Record which source packs an exhibit CSV was built from (<out>/exhibits/lineage.json)."""
    out_dir = os.path.abspath(out_dir)
    lin = read_lineage(out_dir)
    lin[rel_path.replace("\\", "/")] = _packs(packs)
    os.makedirs(os.path.dirname(os.path.join(out_dir, LINEAGE)), exist_ok=True)
    with open(os.path.join(out_dir, LINEAGE), "w", encoding="utf-8") as f:
        json.dump(dict(sorted(lin.items())), f, indent=2)
        f.write("\n")
    return lin


def save_exhibit(df: pd.DataFrame, out_dir: str, rel_path: str, packs=None, **to_csv) -> str:
    """Write an exhibit CSV (index=False unless given) and record its lineage; returns the full path."""
    path = os.path.join(os.path.abspath(out_dir), rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    to_csv.setdefault("index", False)
    df.to_csv(path, **to_csv)
    packs = packs if packs is not None else (df.attrs.get("packs") or df.attrs.get("pack"))
    if packs:
        lineage(out_dir, rel_path, packs)
    return path


def data_packs(out_dir: str) -> list[str]:
    d = os.path.join(out_dir, "data")
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)) and _ID.fullmatch(x))


class Facts:
    """Registry of every number cited in an answer or report. Written to <out>/facts.json."""

    def __init__(self, out_dir: str, pack=None):
        self.out = os.path.abspath(out_dir)
        self.pack = _packs(pack)
        self.items: dict[str, dict] = {}

    # ------------------------------------------------------------------ which source pack
    def _infer_pack(self, name: str, explicit, table, source: str | None) -> list[str]:
        if explicit is not None and explicit != "" and not _packs(explicit):
            return []                            # pack=[]: no pack, not the default
        rel = (source or "").replace("\\", "/").lstrip("./")
        if not _packs(explicit) and rel.startswith("published/"):
            return []                            # an agency page's figure, even with a Facts(out, pack=) default
        got = _packs(explicit) or self.pack
        if got:
            return got
        if source:
            rel = source.replace("\\", "/").lstrip("./")
            m = re.match(r"data/([^/]+)/", rel)
            if m:
                return [m.group(1)]
            if rel.startswith("published/"):
                return []                        # read from an agency page, not a pack: `source` names the CSV
            lin = read_lineage(self.out).get(rel)
            if lin:
                return _packs(lin)
        if table is not None:
            attrs = getattr(table, "attrs", {}) or {}
            got = _packs(attrs.get("packs") or attrs.get("pack"))
            if got:
                return got
        present = data_packs(self.out)
        if len(present) == 1:
            return present
        if not present:
            return []
        raise ValueError(f"fact {name}: {self.out} holds data from {present}; say which source pack the number "
                         "comes from: pack='<id>' (or a list), Facts(out, pack=...), or save the exhibit with "
                         "save_exhibit(..., packs=[...])")

    # ------------------------------------------------------------------ register
    def add(self, name: str, table: pd.DataFrame | None = None, *, where: dict | None = None,
            column: str | None = None, source: str | None = None, value=None, fmt: str = "raw",
            label: str = "", derivation: str = "", se: float | None = None, preliminary: bool = False,
            vintage: str | None = None, projection: bool = False, pack=None):
        """preliminary: an early estimate its agency will revise; vintage: the vintage the value comes from
        when not the latest; projection: a projection, not data. The audit requires the text to say so where
        such a fact is cited. pack: the source pack id (or ids) the number comes from."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"fact name {name!r}: letters, digits and _ only")
        format_value(1.0, fmt)                       # an unknown format fails here, not in the report build
        rec: dict = {"label": label, "fmt": fmt}
        if table is not None and not hasattr(table, "columns"):
            raise TypeError(f"fact {name}: the second argument is the table (a DataFrame) the number is read from; "
                            "for a computed number use value= with derivation=")
        if table is not None:
            if not (where is not None and column and source):
                raise ValueError("table facts need where=, column= and source= (the CSV path)")
            missing = [c for c in [column, *where] if c not in table.columns]
            if missing:
                raise ValueError(f"fact {name}: {missing} not in the table; its columns are {list(table.columns)}")
            m = pd.Series(True, index=table.index)
            for k, v in where.items():
                m &= table[k].astype(str) == str(v)
            if int(m.sum()) != 1:
                raise ValueError(f"fact {name}: where={where} matches {int(m.sum())} rows (need exactly 1)")
            val = table.loc[m, column].iloc[0]
            if pd.isna(val):
                raise ValueError(f"fact {name}: the cell is empty (a gap or a flagged value); do not cite it")
            path = os.path.join(self.out, source)
            if not os.path.exists(path):
                raise FileNotFoundError(f"fact {name}: save {source} before registering facts from it")
            cell, text = _csv_cell(path, where, column, f"fact {name}")
            if abs(cell - float(val)) > _printed_precision(text) + 1e-9 * abs(cell):
                raise ValueError(f"fact {name}: the table gives {val} but {source} holds {text} in {column}; save the "
                                 "CSV from the same table before registering facts from it")
            # The fact is the cell a reader finds in the CSV (the audit compares the two exactly).
            rec.update(value=cell, source=source.replace("\\", "/"), where={k: str(v) for k, v in where.items()},
                       column=column)
            _warn_rounding_tie(name, cell, text, fmt)
        else:
            if value is None or not derivation:
                raise ValueError(f"fact {name}: give table/where/column/source, or value= with derivation=")
            if isinstance(value, (int, float, np.number)) and not isinstance(value, bool) and math.isnan(float(value)):
                raise ValueError(f"fact {name}: value is NaN (a gap in the data); do not cite it")
            rec.update(value=float(value) if isinstance(value, (int, float, np.number)) and not isinstance(value, bool)
                       else value, derivation=derivation)
        kind = next((k for k in (fmt or "").split(":") if k in ("usd_m", "usd_b", "usd_t", "dollars_m", "dollars_b",
                                                               "dollars_t")), "")
        v = rec.get("value")
        scaled = any(p in (fmt or "") for p in ("from_thousands", "from_millions", "from_billions"))
        if kind and not scaled and isinstance(v, float) and 0 < abs(v) < 1e6:
            word = {"m": "million", "b": "billion", "t": "trillion"}[kind[-1]]
            raise ValueError(f"fact {name}: {fmt} expects a value in dollars, but {v} looks already scaled (it would "
                             f"print as $0.0 {word}). Use usd_{kind[-1]}_scaled for a value already in {word}s of "
                             f"dollars, or a from_millions: / from_billions: prefix.")
        if "pct" in (fmt or "").split(":") and isinstance(v, float) and abs(v) > 5:
            raise ValueError(f"fact {name}: fmt pct takes a share (0.274 = 27.4 percent); {v} would print as "
                             f"{v * 100:,.0f} percent: a value already in percent takes pct100")
        if (fmt or "").split(":")[0] == "pp" and isinstance(v, float) and abs(v) > 1:
            raise ValueError(f"fact {name}: fmt pp takes a difference of shares (0.026 = 2.6 points); {v} is already "
                             "in points: use pp100")
        if fmt and isinstance(v, float) and v != 0:      # any format: a units slip shows in the printed text
            try:
                shown = format_value(v, fmt)
            except (ValueError, KeyError, TypeError):
                shown = ""
            digits = re.sub(r"[^\d]", "", shown.split(" ")[0])
            # a dollar amount printed as $0.0 billion is a units slip (a rate can round to 0.0 legitimately)
            if shown.startswith(("$", "-$")) and digits and set(digits) <= {"0"} and "-" not in fmt.split(":")[-1]:
                raise ValueError(f"fact {name}: {v} prints as {shown!r} with fmt {fmt!r}: the value or the format "
                                 "is in the wrong units (millions given to usd_b?); a real amount that small needs more digits "
                                 "(usd_b:2) or a smaller scale (usd_m)")
            if re.search(r"[\d,]{8,}(\.\d+)? (?:million|billion|trillion)", shown):
                raise ValueError(f"fact {name}: {v} prints as {shown!r} with fmt {fmt!r}: a value already in dollars "
                                 "given to a *_scaled or from_* format? Check the units.")
        if se is not None:
            rec["se"] = float(se)
        if derivation:
            rec["derivation"] = derivation
        if preliminary:
            rec["preliminary"] = True
        if vintage and vintage != "latest":
            rec["vintage"] = str(vintage)
        if projection:
            rec["projection"] = True
        packs = self._infer_pack(name, pack, table, rec.get("source"))
        if packs:
            rec["pack"] = packs[0] if len(packs) == 1 else packs
        self.items[name] = rec
        return rec

    def save(self, filename: str = "facts.json") -> str:
        """Writes each fact with "text", its value as the prose prints it (an answer quotes these)."""
        for rec in self.items.values():
            if isinstance(rec.get("value"), (int, float)):
                try:
                    rec["text"] = format_value(rec["value"], rec.get("fmt", "raw"), mode="answer")
                except (ValueError, TypeError):
                    pass
        path = os.path.join(self.out, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, indent=2, default=str)
        return path


__all__ = ["Facts", "save_exhibit", "lineage", "read_lineage", "data_packs", "format_value"]
