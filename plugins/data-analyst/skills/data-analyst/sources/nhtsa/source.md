# NHTSA Vehicle Safety APIs (recalls, complaints, NCAP ratings, vPIC)
Depth: standard. Key: none. Vehicle safety recalls, owner complaints to NHTSA's Office of Defects
Investigation (ODI), NCAP 5-Star crash ratings, VIN decoding (vPIC), and child car seat inspection stations.

## When to use
- Recalls of a make/model/model year or a campaign; complaints about a model (crashes, fires, injuries,
  components); crash-test star ratings and rollover risk; decoding a VIN; models a make built in a year.
- Not here: crash fatality statistics (FARS/CRSS: not covered by this pack; say so), traffic volumes and
  transport statistics (bts or dot packs if present), product recalls outside vehicles (cpsc, fda).
- Records are text and counts of reports. They are not defect rates: there is no fleet size or sales data.

## Endpoints and datasets
| Endpoint | Returns | Key parameters |
|---|---|---|
| `recalls_by_vehicle` | recall campaigns for one vehicle | `make`, `model`, `modelYear` (all required) |
| `recall_campaign` | one campaign, a row per make/model/year covered | `campaignNumber` (23V838000) |
| `complaints_by_vehicle` | every ODI complaint for one vehicle | `make`, `model`, `modelYear` |
| `complaint_odi` | one complaint | `odinumber` |
| `products_model_years` / `products_makes` / `products_models` | years, makes, models that have recalls (`issueType` r) or complaints (c) | `modelYear`, `make`, `issueType` |
| `ratings_model_years` / `ratings_makes` / `ratings_models` | NCAP-rated years, makes, models | `modelYear`, `make` |
| `ratings_vehicles` | rated variants (VehicleId + body/drive) | `modelYear`, `make`, `model` |
| `ratings_detail` | star ratings, rollover probability, ADAS fitment, complaint/recall/investigation counts | `vehicleId` |
| `vin_decode` | vPIC decode, about 150 fields in one row | `vin`, optional `modelyear` |
| `vpic_models_for_make_year` / `vpic_models_for_make` / `vpic_all_makes` | vPIC make and model lists | `make`, `modelYear` |
| `car_seat_stations_state` / `_zip` | car seat inspection stations | `state` or `zip` (the API's lat/long radius search ignores the point and returns the first 100 stations nationwide, verified 2026-10, so the pack does not offer it) |

Every endpoint answers in one page (no paging). Recalls cover model years 1949 to the coming year, and
`products_model_years` also lists `9999` for records with no model year (recalls and complaints; drop it from
year ranges, verified 2026-10);
NCAP ratings start with model year 1990 (`ratings_model_years` lists them). `--endpoints` lists all.

## Spec examples
```json
{"description": "2019 Honda Civic: recalls and sedan NCAP rating",
 "requests": [{"name": "civic_2019_recalls", "endpoint": "recalls_by_vehicle",
               "params": {"make": "honda", "model": "civic", "modelYear": 2019}},
              {"name": "civic_2019_sedan_rating", "endpoint": "ratings_detail", "params": {"vehicleId": 14009}}]}
```
```json
{"description": "Complaints about the 2023 Tesla Model 3, and the models with complaints that year",
 "requests": [{"name": "m3_2023_complaints", "endpoint": "complaints_by_vehicle",
               "params": {"make": "tesla", "model": "model 3", "modelYear": 2023}},
              {"name": "tesla_2023_models", "endpoint": "products_models",
               "params": {"modelYear": 2023, "make": "tesla", "issueType": "c"}}]}
```
```json
{"description": "Decode a VIN; rated variants of the 2019 Civic",
 "requests": [{"name": "vin", "endpoint": "vin_decode", "params": {"vin": "1HGCM82633A123456"}},
              {"name": "civic_variants", "endpoint": "ratings_vehicles",
               "params": {"modelYear": 2019, "make": "honda", "model": "civic"}}]}
```
A range of model years is one request per year (`modelYear` takes one year).

## Data format and traps
- Dates differ by endpoint (verified 2026-10): recalls `ReportReceivedDate` is **DD/MM/YYYY**
  ("28/05/2020"); complaints `dateOfIncident` and `dateComplaintFiled` are **MM/DD/YYYY** ("07/24/2025").
  The pack keeps the published column and adds `<column>_iso` (YYYY-MM-DD). Use the `_iso` columns.
- A make/model/year that matches nothing answers **HTTP 400 with `Count: 0`**, which the fetch reports as
  a bad request: either the name did not match or there is no such model year (no recalls is not 400).
  `recalls_by_vehicle` matches its own base model names, which are not always the ones `products_models`
  lists: variant suffixes fail ("MODEL Y 5-SEAT", "MUSTANG MACH-E BEV") and hyphens are inconsistent
  ("mustang mach e" matches, "mustang mach-e" does not, while "f-150" does; verified 2026-10). Try the
  plain base name, then the variant without suffixes or hyphens, and report which name matched.
  `ratings_models` gives NCAP names. Matching is case-insensitive.
- Model names differ between databases: NCAP rates variants ("2019 Honda CIVIC 4 DR FWD", one VehicleId
  each), recalls and complaints use the plain model, vPIC has its own spelling ("Civic"). `products_models`
  repeats a model once per matching record; de-duplicate.
- Counts disagree across endpoints: `ratings_detail` RecallsCount (2 for the 2019 Civic sedan) is
  smaller than `recalls_by_vehicle` (4 campaigns), because ratings count campaigns tied to that variant.
  Cite the endpoint a number came from.
- One recall campaign covers many vehicles: `recall_campaign` returns one row per make/model/year, so
  count distinct `NHTSACampaignNumber` values, not rows. `PotentialNumberofUnitsAffected` is per campaign.
- Complaints are unverified consumer reports; `products` (a JSON list of vehicles) and `components`
  (comma-separated) can name several items, so component counts exceed complaint counts. Complaint
  VINs are truncated to 11 characters. A complaint count without fleet size is not a defect rate.
- Booleans are written as `True`/`False` (parkIt, overTheAirUpdate, crash, fire).
- Star ratings are text "1" to "5" or "Not Rated"; `RolloverPossibility` is a probability (0.095 = 9.5%).
  Ratings from different test protocols (model years before and after 2011) are not comparable.
- vPIC: `ErrorCode` "0" means a clean decode; other codes (e.g. "1" check-digit error) mean the decode may
  be wrong; read `ErrorText`. Empty fields mean NHTSA has no data, not that a feature is absent.
- Car seat station rows carry public contact names and phone numbers; report counts and places, not people.

## Access
No key. Route `api` when api.nhtsa.gov answers (vPIC endpoints use vpic.nhtsa.dot.gov). Requests are
small; the pack keeps to 3 a second. NHTSA publishes no numeric limit but says the API is not for bulk VIN
lookups: for many VINs use vPIC's batch decoder (DecodeVINValuesBatch, 50 a call) rather than one `vin_decode` each. Manual route: `manual_download/INSTRUCTIONS.md` lists one JSON link
per request; open each in a browser, save under the name shown, upload, then `--ingest`. Without the
links: https://www.nhtsa.gov/recalls (search by vehicle), https://www.nhtsa.gov/ratings, and
https://vpic.nhtsa.dot.gov/decoder/. For all recalls or complaints at once, NHTSA publishes flat files
(tab-delimited, zipped) at https://www.nhtsa.gov/nhtsa-datasets-and-apis; this pack does not parse them.

## Citation and terms
"National Highway Traffic Safety Administration, <endpoint or dataset>, api.nhtsa.gov, retrieved <date>."
Public domain (U.S. government work); attribute NHTSA, do not imply endorsement. Redistribution allowed.
