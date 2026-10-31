"""Generate SKILL/references/routing.md from every pack's policy.json plus the curated pairings below.

    python tools/gen_routing.py          # rewrite routing.md
    python tools/gen_routing.py --check  # exit 1 if routing.md is out of date (used by the tests)

The pairings encode which sources answer a kind of question together. Edit them here, never in routing.md.
"""
import os
import sys

sys.dont_write_bytecode = True                   # never write __pycache__ into the skill folder
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from core import packs  # noqa: E402

# (question type, primary sources, cross-check / context sources, note)
PAIRINGS = [
    ("Federal debt, deficit, interest costs", "treasury", "bea, fred, cbo publications via govinfo",
     "Debt and deficits from Treasury (MTS, Debt to the Penny); GDP from BEA (fiscal-year GDP from quarters) for shares."),
    ("Market interest rates, yield curve", "treasury-yields", "fred",
     "Treasury par yields; FRED for long histories and policy rates. Average rates on outstanding debt are in treasury."),
    ("GDP, income, consumption, PCE inflation", "bea", "fred", "BEA is the producer; FRED for vintages (ALFRED)."),
    ("Jobs, unemployment, payrolls, JOLTS", "bls", "dol, fred", "BLS CPS/CES/JOLTS; DOL weekly UI claims; FRED for long series."),
    ("Consumer prices, inflation, real dollars", "bls", "bea, fred", "CPI-U from BLS; PCE price index from BEA; name the deflator."),
    ("Wages and earnings", "bls", "ipums, opm", "BLS ECI, CES, OEWS; IPUMS CPS/ACS for distributions by group (weighted, with SEs)."),
    ("Federal workforce: headcount, hires, separations, pay", "opm", "bls, ipums, usaspending",
     "OPM FWD (coverage gaps: USPS, intelligence agencies; pay withheld for ~45% of records); BLS ECI or IPUMS for private comparisons."),
    ("Demographics, poverty, income by place", "census", "ipums, bea",
     "ACS tables with margins of error; IPUMS microdata for custom tabulations; BEA regional income."),
    ("Custom tabulations from survey microdata", "ipums", "census, bls",
     "IPUMS CPS/ACS/decennial, NHIS, MEPS; weighted estimates with SEs; microdata never redistributed."),
    ("Health insurance, access to care, health care spending by group", "ipums", "census, cdc, cms",
     "IPUMS NHIS (insurance, access, conditions) and MEPS (spending by payer); ACS tables for state coverage; CMS for Medicare program data."),
    ("Federal spending by agency, program, place, recipient", "usaspending", "treasury, census",
     "Obligations vs outlays differ; Treasury MTS for totals; Census population (not USAspending's) for per-person figures."),
    ("Legislation, votes, laws, CRS reports", "congress", "govinfo, federal-register",
     "Congress.gov for bills and actions; GovInfo for text and CBO/committee reports; Federal Register for rules."),
    ("Regulations and rulemaking", "federal-register", "regulations, govinfo", "Rules and notices; Regulations.gov for dockets and comments."),
    ("Campaign finance and lobbying", "fec", "senate-lobbying", "FEC contributor data may not be used to solicit."),
    ("Energy production, prices, consumption", "eia", "bls, fred", "EIA series; CPI energy components from BLS."),
    ("Health outcomes and mortality", "cdc", "census, cms", "CDC counts and rates; age-adjust before comparing places; Census for denominators."),
    ("Health care providers, Medicare", "cms", "open-payments, cdc", "CMS provider data and Medicare datasets; Open Payments shows industry payments, not wrongdoing."),
    ("Drugs, devices, recalls", "fda", "clinical-trials, nih", "Adverse-event reports are not incidence rates."),
    ("Research funding and trials", "nih", "clinical-trials", "NIH RePORTER projects; ClinicalTrials.gov registrations."),
    ("Banks and consumer finance", "fdic", "cfpb, fred", "FDIC institution data; CFPB complaints are not representative."),
    ("Company financials and filings", "sec", "fred", "EDGAR XBRL facts; SEC asks for a contact User-Agent."),
    ("Housing costs and assistance", "hud", "census, fred", "HUD FMRs and income limits; ACS rents and values."),
    ("Agriculture", "usda-nass", "bls, world-bank", "NASS Quick Stats."),
    ("SNAP, WIC and school meals: participation, costs, state policy options", "usda-fna", "census, usaspending",
     "FNA Program Data (monthly, by state); SNAP State Options Report; State Activity Reports (FY 2019-2023 tidy)."),
    ("Characteristics of SNAP households: income, earnings, children, elderly, benefit amounts", "snapqc",
     "usda-fna, ipums",
     "SNAP QC public-use microdata (FY 2014 on), weighted with FYWGT; caseload totals from usda-fna; IPUMS CPS/ACS for SNAP-reporting households."),
    ("Transportation", "bts", "nhtsa", "BTS statistics; NHTSA for vehicle safety."),
    ("Environment, air and water quality", "epa", "epa-aqs, usgs, noaa", "EPA envirofacts/ECHO; AQS monitor data; USGS water."),
    ("Weather and climate", "noaa", "nws", "NOAA NCEI history; NWS is forecasts and the last week of observations only."),
    ("Earthquakes, water, hazards", "usgs", "fema", "USGS events and water; FEMA declarations and claims."),
    ("Disasters and insurance", "fema", "usaspending, census", "Declarations, NFIP, public assistance."),
    ("Crime and justice", "fbi", "doj-news, census", "NIBRS transition: participation changed sharply in 2021."),
    ("Higher education", "college-scorecard", "naep, census", "Scorecard institution data."),
    ("K-12 achievement", "naep", "census", "NAEP estimates have standard errors; test significance."),
    ("Federal contracting labor rates", "gsa-calc", "usaspending", "CALC+ ceiling rates."),
    ("Food composition and nutrients", "usda-fooddata", "cdc", "FoodData Central; one row per food and nutrient."),
    ("Patents and trademarks", "uspto", "sec", "USPTO Open Data Portal (key needs a USPTO.gov account)."),
    ("Renewable energy resources", "nrel", "eia", "NREL resource and incentive data."),
    ("International comparisons", "world-bank", "bea, fred", "World Bank indicators: check currency and base year."),
    ("Long histories, many indicators at once, data vintages", "fred", "the producing agency's pack",
     "FRED re-publishes; cite the original source too; ALFRED for as-of vintages."),
]


def render() -> str:
    ps = {p.id: p.policy for p in packs.all_packs()}
    lines = ["# Routing: which sources answer which questions", "",
             "Generated by tools/gen_routing.py from each pack's policy.json; do not edit by hand.", "",
             "Split the question into measures, give each its source, then read only those packs' source.md.",
             "Use `fetch.py --route \"<question>\"` for a ranked suggestion.", "",
             "## Usual pairings", "", "| Question | Primary | Cross-check / context | Note |", "|---|---|---|---|"]
    for q, prim, other, note in PAIRINGS:
        lines.append(f"| {q} | {prim} | {other} | {note} |")
    lines += ["", "## All sources", "", "| Source | Depth | Key | Domains | Title |", "|---|---|---|---|---|"]
    for pid, P in sorted(ps.items()):
        a = P["auth"]
        key = a["required"] + (f" ({a['env']})" if a.get("env") else "")
        lines.append(f"| {pid} | {P['depth']} | {key} | {', '.join(P['domains'])} | {P['title']} |")
    return "\n".join(lines) + "\n"


def missing_from_pairings() -> list[str]:
    named = {s.strip() for _, a, b, _ in PAIRINGS for s in (a + "," + b).split(",")}
    return sorted(set(packs.pack_ids()) - named)


if __name__ == "__main__":
    unknown = [a for a in sys.argv[1:] if a != "--check"]
    if unknown:                                  # e.g. --help: print usage, never rewrite routing.md
        print(__doc__)
        sys.exit(0 if unknown in (["-h"], ["--help"]) else 2)
    path = os.path.join(SKILL, "references", "routing.md")
    text = render()
    if "--check" in sys.argv:
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        sys.exit(0 if cur == text else 1)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"wrote {path}; packs not in any pairing: {missing_from_pairings() or 'none'}")
