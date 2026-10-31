"""Philadelphia Fed Real-Time Data Research Center: published .xlsx workbooks read by the generic $xlsx reader.

The site answers a file that does not exist with HTTP 200 and an HTML page titled "Error - 404" (verified
2026-10-30, e.g. .../historical-data/doesnotexist.xlsx), so check_error turns that page into a clear message
instead of the reader's "not an .xlsx workbook". normalize adds readable date columns next to the published
ones (never replacing them): SPF `survey` (2026Q3 from YEAR/QUARTER), Livingston `survey_date` (the Date
column is an Excel serial day number), Greenbook `greenbook_date` (GBdate yyyymmdd).
"""
from __future__ import annotations

import datetime
import re

FILES_PAGE = "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research"


def check_error(status, body: bytes):
    head = body[:2048].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        what = "an 'Error - 404' page" if b"error - 404" in body[:4096].lower() else "an HTML page"
        return (f"the Philadelphia Fed answered with {what} (HTTP {status}) instead of the workbook: no file by that "
                "name. Check the endpoint's file/measure value against source.md (file names are fixed, e.g. "
                f"medianLevel, Dispersion_2, ROUTPUTQvQd), or find the file on the data pages under {FILES_PAGE}.")
    return None


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _survey(row: dict) -> str:
    y, q = _num(row.get("YEAR")), _num(row.get("QUARTER"))
    return f"{int(y)}Q{int(q)}" if y is not None and q is not None else ""


def _excel_date(v) -> str:
    n = _num(v)
    if n is None or not 0 < n < 100000:
        return ""
    return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(n))).isoformat()


def _yyyymmdd(v) -> str:
    s = str(v or "").strip().split(".")[0]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else ""


def normalize(req, ep, rows):
    name = req.get("endpoint")
    out = []
    for r in rows:
        if name in ("spf", "spf_prob") and "YEAR" in r:
            r = {"survey": _survey(r), **r}
        elif name == "livingston" and "Date" in r:
            r = {"survey_date": _excel_date(r["Date"]), **r}
        elif name == "greenbook" and "GBdate" in r:
            r = {"greenbook_date": _yyyymmdd(r["GBdate"]), **r}
        out.append(r)
    return out
