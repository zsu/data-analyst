"""Read raw OPM workforce files in chunks, whatever this environment can read.

Formats:
  .parquet   from the API (the only format it serves). Needs one Parquet engine: pyarrow (preferred),
             fastparquet, duckdb or polars - whichever is installed.
  .txt/.csv  from the FWD Data Downloads web page ("Download .TXT"). Delimiter sniffed (comma, pipe,
             tab, semicolon, caret); first row must be the field names. Read with pandas in chunks.
  .json      from the web page ("Download .JSON"): a JSON array of records, JSON Lines, or an object
             wrapping the array (e.g. {"data": [...]}). Arrays and JSON Lines are streamed.
  .gz / .zip of any of these.

Every value is returned as a string (codes keep leading zeros), like the Parquet files.

    from opm_io import capabilities, iter_frames
    caps = capabilities()        # {"pandas": True, "parquet_engine": "pyarrow", "parquet_writer": "pyarrow"}
    for chunk in iter_frames(path, columns=["agency_code", "count"]):
        ...
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import zipfile
from typing import Iterator

CHUNK_ROWS = 250_000
DATASET_MARKERS = {"separations": "separation_category", "accessions": "accession_category",
                   "employment": "snapshot_yyyymm"}


# ---------------------------------------------------------------- capabilities
def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001  (a broken install counts as missing)
        return False


def capabilities() -> dict:
    """What this Python can do. parquet_engine is None when no reader is installed."""
    engines = [e for e in ("pyarrow", "fastparquet", "duckdb", "polars") if _has(e)]
    writers = [e for e in ("pyarrow", "fastparquet") if e in engines]
    return {"pandas": _has("pandas"), "parquet_engine": engines[0] if engines else None,
            "parquet_engines": engines, "parquet_writer": writers[0] if writers else None}


# ---------------------------------------------------------------- parquet
def parquet_columns(path: str, engine: str) -> list[str]:
    if engine == "pyarrow":
        import pyarrow.parquet as pq
        return list(pq.read_schema(path).names)
    if engine == "fastparquet":
        import fastparquet
        return list(fastparquet.ParquetFile(path).columns)
    if engine == "duckdb":
        import duckdb
        return [r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{_q(path)}')").fetchall()]
    if engine == "polars":
        import polars as pl
        return list(pl.read_parquet_schema(path).keys())
    raise RuntimeError("no Parquet engine")


def _q(p: str) -> str:
    return p.replace("\\", "/").replace("'", "''")


def iter_parquet(path: str, columns: list[str] | None, engine: str) -> Iterator:
    import pandas as pd
    if engine == "pyarrow":
        import pyarrow.parquet as pq
        f = pq.ParquetFile(path)
        for b in f.iter_batches(batch_size=CHUNK_ROWS, columns=columns):
            yield b.to_pandas().astype("string")
    elif engine == "fastparquet":
        import fastparquet
        for df in fastparquet.ParquetFile(path).iter_row_groups(columns=columns):
            yield df.astype("string")
    elif engine == "duckdb":
        import duckdb
        cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        rel = duckdb.sql(f"SELECT {cols} FROM read_parquet('{_q(path)}')")
        while True:
            df = rel.fetch_df_chunk(CHUNK_ROWS // 2048 or 1)
            if df is None or not len(df):
                break
            yield df.astype("string")
    elif engine == "polars":
        import polars as pl
        df = pl.read_parquet(path, columns=columns).to_pandas()
        for i in range(0, len(df), CHUNK_ROWS):
            yield df.iloc[i:i + CHUNK_ROWS].astype("string")
    else:
        raise RuntimeError("no Parquet engine")


# ---------------------------------------------------------------- text and json
def _open_text(path: str):
    """Text handle for a plain, .gz or single-member .zip file."""
    low = path.lower()
    if low.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8-sig", errors="replace", newline="")
    if low.endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if not n.endswith("/"))
        return io.TextIOWrapper(z.open(name), encoding="utf-8-sig", errors="replace", newline="")
    return open(path, encoding="utf-8-sig", errors="replace", newline="")


def inner_name(path: str) -> str:
    """File name that tells the format: the member of a .zip, or the name without .gz."""
    low = path.lower()
    if low.endswith(".zip"):
        return next(n for n in zipfile.ZipFile(path).namelist() if not n.endswith("/"))
    return path[:-3] if low.endswith(".gz") else path


def file_kind(path: str) -> str:
    name = inner_name(path).lower()
    if name.endswith(".parquet"):
        return "parquet"
    if name.endswith(".json") or name.endswith(".jsonl"):
        return "json"
    if name.endswith((".txt", ".csv", ".tsv", ".dat")):
        return "text"
    with _open_text(path) as f:                         # unknown extension: look at the content
        head = f.read(2048).lstrip()
    return "json" if head[:1] in "[{" else "text"


def sniff_delimiter(path: str) -> str:
    with _open_text(path) as f:
        sample = "".join(f.readline() for _ in range(20))
    try:
        return csv.Sniffer().sniff(sample, delimiters=",|\t;^").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        return max(",|\t;^", key=first.count)


def text_columns(path: str) -> list[str]:
    d = sniff_delimiter(path)
    with _open_text(path) as f:
        return [c.strip().strip('"').lower() for c in next(csv.reader(f, delimiter=d))]


def iter_text(path: str, columns: list[str] | None) -> Iterator:
    import pandas as pd
    d = sniff_delimiter(path)
    have = text_columns(path)
    use = [c for c in (columns or have) if c in have]
    with _open_text(path) as f:
        reader = pd.read_csv(f, sep=d, dtype=str, keep_default_na=False, chunksize=CHUNK_ROWS,
                             usecols=lambda c: c.strip().strip('"').lower() in set(use), engine="c")
        for df in reader:
            df.columns = [c.strip().strip('"').lower() for c in df.columns]
            yield _blank_to_na(df)


def _json_records(path: str) -> Iterator[dict]:
    """Stream records from a JSON array, JSON Lines, or {"<key>": [records]} file."""
    dec = json.JSONDecoder()
    with _open_text(path) as f:
        buf = f.read(1 << 20)
        s = buf.lstrip()
        if s.startswith("{"):
            # JSON Lines (one object per line) or a wrapper object.
            first_line = s.split("\n", 1)[0].strip()
            try:
                json.loads(first_line)
                is_lines = True
            except ValueError:
                is_lines = False
            if is_lines:
                for line in (buf + f.read()).splitlines() if os.path.getsize(path) < (1 << 27) else _lines(buf, f):
                    line = line.strip()
                    if line:
                        yield json.loads(line)
                return
            obj = json.loads(buf + f.read())               # wrapper object: must fit in memory
            arr = next((v for v in obj.values() if isinstance(v, list)), [])
            yield from arr
            return
        # array of records: decode one object at a time, refilling the buffer as needed
        ws = re.compile(r"[\s,]*")
        i = buf.index("[") + 1
        while True:
            i = ws.match(buf, i).end()
            if i >= len(buf):
                more = f.read(1 << 20)
                if not more:
                    return
                buf, i = more, 0
                continue
            if buf[i] == "]":
                return
            try:
                rec, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                more = f.read(1 << 20)
                if not more:
                    raise
                buf, i = buf[i:] + more, 0
                continue
            yield rec
            i = j
            if i > (1 << 20):
                buf, i = buf[i:], 0


def _lines(buf: str, f) -> Iterator[str]:
    rest = ""
    chunk = buf
    while chunk:
        parts = (rest + chunk).split("\n")
        rest = parts.pop()
        yield from parts
        chunk = f.read(1 << 20)
    if rest:
        yield rest


def json_columns(path: str) -> list[str]:
    for rec in _json_records(path):
        return [str(k).lower() for k in rec]
    return []


def iter_json(path: str, columns: list[str] | None) -> Iterator:
    import pandas as pd
    batch = []
    want = set(columns) if columns else None
    for rec in _json_records(path):
        rec = {str(k).lower(): (None if v is None or v == "" or (isinstance(v, float) and v != v) else str(v)) for k, v in rec.items()}
        batch.append({k: v for k, v in rec.items() if want is None or k in want})
        if len(batch) >= CHUNK_ROWS:
            yield _blank_to_na(pd.DataFrame(batch))
            batch = []
    if batch:
        yield _blank_to_na(pd.DataFrame(batch))


def _blank_to_na(df):
    """Text formats cannot tell an empty field from a null; Parquet stores nulls. Match Parquet."""
    df = df.astype("string")
    return df.mask(df.eq(""))


# ---------------------------------------------------------------- dispatch
def columns_of(path: str, engine: str | None = None) -> list[str]:
    k = file_kind(path)
    if k == "parquet":
        if not engine:
            raise RuntimeError("no Parquet engine installed (pip install pyarrow, or fastparquet / duckdb / polars)")
        return parquet_columns(path, engine)
    return text_columns(path) if k == "text" else json_columns(path)


def iter_frames(path: str, columns: list[str] | None = None, engine: str | None = None) -> Iterator:
    k = file_kind(path)
    if k == "parquet":
        if not engine:
            raise RuntimeError("no Parquet engine installed (pip install pyarrow, or fastparquet / duckdb / polars)")
        yield from iter_parquet(path, columns, engine)
    elif k == "text":
        yield from iter_text(path, columns)
    else:
        yield from iter_json(path, columns)


def identify(path: str, engine: str | None = None) -> dict:
    """Guess dataset, year, month and version of a hand-downloaded file: from its name first
    (employment_202608_1.parquet, 'Federal Separations Raw Data (August 2026).txt'), then from its
    content. Returns {dataset, year, month, version, how} with None where unknown."""
    name = os.path.basename(inner_name(path))
    out = {"dataset": None, "year": None, "month": None, "version": None, "how": "name"}
    m = re.search(r"(employment|accessions|separations)_(\d{4})(\d{2})_(\d+)", name, re.I)
    if m:
        return {"dataset": m.group(1).lower(), "year": int(m.group(2)), "month": int(m.group(3)),
                "version": int(m.group(4)), "how": "name"}
    low = name.lower()
    for ds in DATASET_MARKERS:
        if ds in low:
            out["dataset"] = ds
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
              "october", "november", "december"]
    # Month names as whole words only, after removing the dataset words: "separations" starts with "sep".
    bare = re.sub(r"employment|accessions|separations", " ", low)
    names = months + [mo[:3] for mo in months] + ["sept"]
    m = re.search(r"(?<![a-z])(" + "|".join(names) + r")(?![a-z])[\s_.,-]*(\d{4})(?!\d)", bare)
    if m:
        out["month"] = next(i + 1 for i, mo in enumerate(months) if mo.startswith(m.group(1)[:3]))
        out["year"] = int(m.group(2))
    else:
        m = re.search(r"(?<!\d)(20\d{2})[-_]?(0[1-9]|1[0-2])(?!\d)", bare)
        if m:
            out["year"], out["month"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(?:version|ver|(?<![a-z])v)[\s_.-]*(\d+)(?!\d)", bare)
    if m:
        out["version"] = int(m.group(1))
    if out["dataset"] and out["year"]:
        return out
    # content: which dataset, and the snapshot or latest effective month
    out["how"] = "content"
    cols = set(columns_of(path, engine))
    for ds, marker in DATASET_MARKERS.items():
        if marker in cols and not out["dataset"]:
            out["dataset"] = ds
    pcol = "snapshot_yyyymm" if out["dataset"] == "employment" else "personnel_action_effective_date_yyyymm"
    if pcol in cols and not out["year"]:
        top = None
        for df in iter_frames(path, [pcol], engine):
            v = df[pcol].dropna()
            v = v[v.str.fullmatch(r"\d{6}")]
            if len(v):
                top = max(top or "", v.max())
            if out["dataset"] == "employment" and top:
                break
        if top:
            out["year"], out["month"] = int(top[:4]), int(top[4:])
    return out
