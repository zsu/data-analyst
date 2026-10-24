# data-analyst

An AI-assistant skill (Claude, Codex, or any assistant that reads Agent Skills) that answers questions with official U.S. government data from federal sources (BEA, BLS,
FRED, Census, IPUMS, Treasury, OPM, USAspending, CDC, Congress.gov, EPA and more). It picks the sources a
question needs, gets the data in any environment, asks only the questions that change the numbers, and
returns CSV data plus a short answer, or an audited HTML report or paper when asked.

Each source keeps its own rules (IPUMS microdata is never redistributed; FRED forbids persistent copies;
OPM reports disclose redaction coverage; ...). `fetch.py --sources` lists every source.

## Install
Install it as a plugin from this GitHub repo. The skill is then `/data-analyst:data-analyst`, and it also
triggers on its own when you ask a question about U.S. government data.

- **Claude Code (terminal):**
  ```
  /plugin marketplace add zsu/data-analyst
  /plugin install data-analyst@zsu
  ```
  or from a shell: `claude plugin marketplace add zsu/data-analyst` then
  `claude plugin install data-analyst@zsu`.
- **Claude Desktop (Code tab):** click **+**, choose **Add plugins**, add the marketplace `zsu/data-analyst`,
  and install `data-analyst`.
- **Cowork and claude.ai (web):** Customize > Plugins > Add > Add marketplace, enter `zsu/data-analyst`, then
  install `data-analyst`. Or run `python tools/package_skill.py --plugin` and use Add > Upload plugin with
  `dist/data-analyst-plugin.zip`. Keyless sources work when the organization's network settings allow their
  hosts; otherwise the skill hands over a manual-download bundle and ingests the uploads.
- **Skill only on claude.ai:** `python tools/package_skill.py`, then upload `dist/data-analyst.zip` under
  Settings > Capabilities > Skills.
- **A whole team's repo:** commit this to the project's `.claude/settings.json`; collaborators are prompted to
  install it when they trust the folder:
  ```json
  {
    "extraKnownMarketplaces": {"zsu": {"source": {"source": "github", "repo": "zsu/data-analyst"}}},
    "enabledPlugins": {"data-analyst@zsu": true}
  }
  ```
- **Codex (CLI and app):** `codex plugin marketplace add zsu/data-analyst`, then install `data-analyst` from the
  Plugins directory. The skill triggers on its own for U.S. government data questions. Codex's sandbox may block
  network access; the skill says so and how to allow it.
- **Any other assistant that reads Agent Skills:** copy `plugins/data-analyst/skills/data-analyst/` (or unzip
  `dist/data-analyst.zip` from `python tools/package_skill.py`) into that assistant's skills folder.
- **Develop without installing:** from a clone, `claude --plugin-dir plugins/data-analyst`.

The skill never writes into its own folder (the plugin cache): outputs go to `out/` in your working directory
(or `$DATA_ANALYST_OUT`) and downloads are cached in `~/.cache/data-analyst`.

## API keys (optional for most sources)
Run the setup script for a source in your own terminal; it asks for the key with hidden input and stores
it in `~/.config/data-analyst/keys.json` (or `$DATA_ANALYST_CONFIG_DIR`), which the skill reads directly under
any assistant. If the key variable (e.g. `FRED_API_KEY`) is already set in your environment, the script stores
nothing: the skill uses it (`--force` / `-Force` stores one anyway). Keys stored earlier in Claude Code's
`settings.json` keep working (`--claude` / `-Claude` still writes there). When the skill needs a key it prints the
exact command for your install; from a clone, run:
```bash
bash plugins/data-analyst/skills/data-analyst/setup.sh fred
```
```bash
powershell -NoProfile -ExecutionPolicy Bypass -File plugins/data-analyst/skills/data-analyst/setup.ps1 -Source fred
```
`python plugins/data-analyst/skills/data-analyst/scripts/fetch.py --sources` lists every source and its key variable.

## How it is built
| Path | What |
|---|---|
| `plugins/data-analyst/skills/data-analyst/SKILL.md` | the workflow the model follows |
| `.../scripts/fetch.py` | one command line for every source, plans of several sources, routes |
| `.../scripts/core/` | keys, environment, HTTP client, routes, cache, packaging, manual bundles, policy |
| `.../scripts/analysis/`, `.../scripts/report/` | Facts registry, combining helpers; report builder and audit |
| `.../sources/<id>/` | one pack per source: `policy.json` (enforced rules), `source.md`, endpoints, tests |
| `docs/pack_contract.md` | how to add a source |
| `tests/` | offline tests (core, report chain, sandbox) and each pack's tests and fixtures in `tests/sources/<id>/` |
| `tools/` | routing generator and upload packager |

Tests: `python -I tests/run_all.py`.
