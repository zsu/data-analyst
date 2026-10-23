"""USDA FoodData Central adapter (api.nal.usda.gov/fdc/v1).

layout (request option, default "long"):
  long   one row per food and nutrient: fdcId, description, dataType, brandOwner, foodCategory, ...,
         nutrient_number, nutrient_name, unit, value (+ derivation when given). Search results, abridged
         and full food records name their nutrient fields differently (nutrientNumber/value,
         number/amount, nutrient.number/amount); all three are mapped to the same columns, values as
         published.
  foods  one row per food; foodNutrients stays a JSON text column.
Lists given for dataType or fdcIds are sent as comma-separated text. Keys: DATA_GOV_API_KEY (api.data.gov),
else DEMO_KEY (low hourly and daily limits).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from core import generic_rest as gr  # noqa: E402

FOOD_COLS = ("fdcId", "description", "dataType", "brandOwner", "brandName", "gtinUpc", "foodCategory",
             "foodCategory.description", "ndbNumber", "foodCode", "servingSize", "servingSizeUnit",
             "publishedDate", "publicationDate")
LAYOUTS = ("long", "foods")


def before_request(req, ep, params):
    p = dict(params)
    for k in ("dataType", "fdcIds", "nutrients"):
        if isinstance(p.get(k), list):
            p[k] = ",".join(str(x) for x in p[k])
    return p


def check_spec(spec):
    probs = []
    for r in spec.get("requests") or []:
        name = r.get("name")
        if r.get("layout", "long") not in LAYOUTS:
            probs.append(f"request {name!r}: layout must be one of {LAYOUTS}")
        ids = (r.get("params") or {}).get("fdcIds")
        if ids is not None:
            n = len(ids) if isinstance(ids, list) else len([x for x in str(ids).split(",") if x.strip()])
            if n > 20:
                probs.append(f"request {name!r}: {n} fdcIds; the foods endpoint is documented for at most 20 per request "
                             "and silently drops extra ids (past 50), so split it into requests of 20 or fewer")
    return probs


def _nutrient(n: dict) -> dict:
    nut = n.get("nutrient") if isinstance(n.get("nutrient"), dict) else {}
    der = n.get("foodNutrientDerivation") if isinstance(n.get("foodNutrientDerivation"), dict) else {}
    return {"nutrient_number": n.get("nutrientNumber") or n.get("number") or nut.get("number") or "",
            "nutrient_id": n.get("nutrientId") or nut.get("id") or "",
            "nutrient_name": n.get("nutrientName") or n.get("name") or nut.get("name") or "",
            "unit": n.get("unitName") or nut.get("unitName") or "",
            "value": n.get("value") if n.get("value") is not None else n.get("amount", ""),
            "derivation": n.get("derivationDescription") or der.get("description") or ""}


def normalize(req, ep, rows):
    if req.get("layout", "long") != "long":
        return rows
    out = []
    for r in rows:
        raw = r.get("foodNutrients")
        try:
            nuts = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else []
        except ValueError:
            nuts = []
        base = {c: r.get(c, "") for c in FOOD_COLS if c in r}
        if not nuts:
            out.append({**base, "nutrient_number": "", "nutrient_id": "", "nutrient_name": "", "unit": "",
                        "value": "", "derivation": ""})
            continue
        for n in nuts:
            if isinstance(n, dict):
                out.append({**base, **_nutrient(n)})
    return out


def manual_items(spec, pack):
    items = gr.manual_items(pack, spec)          # core applies before_request to the links
    for it in items:
        it["note"] = ((it["note"].rstrip(".") + ". " if it.get("note") else "") + "DEMO_KEY works in a browser for a few requests an hour; replace "
                      "it with your own api.data.gov key for more. Full datasets (CSV/JSON): "
                      "https://fdc.nal.usda.gov/download-datasets").strip()
    return items


def parse_upload(pack, req, paths):
    rows = gr.parse_upload(pack, {**req, "layout": "foods"}, paths)
    return normalize(req, gr.resolve(pack, req)[0], rows)
