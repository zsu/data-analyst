# data-analyst (maintainers)

One Claude skill (`plugins/data-analyst/skills/data-analyst/`, shipped as the `data-analyst` plugin) for U.S. government data. Each source is a pack in
`sources/<id>/` following `docs/pack_contract.md`; its tests are in `tests/sources/<id>/`; shared code is in `scripts/core/`.

- Tests: `python -I tests/run_all.py` (offline; fixtures). One pack: `python -I tests/run_all.py <id>`.
- Record fixtures from a live call: `DATA_ANALYST_RECORD=tests/sources/<id>/fixtures python .../fetch.py --source <id> ...`.
- After adding or changing a pack's policy.json: `python tools/gen_routing.py` (the tests fail when routing.md is stale).
- Plugin layout: `.claude-plugin/marketplace.json` (marketplace `zsu`) -> `plugins/data-analyst/.claude-plugin/plugin.json`
  -> `skills/data-analyst/`. Version lives only in plugin.json: bump it per release (users update on a new version).
  Check with `claude plugin validate .` and `claude plugin validate plugins/data-analyst`. Run it locally without installing:
  `claude --plugin-dir plugins/data-analyst`. No CLAUDE.md or AGENTS.md inside the plugin folder (not loaded from there).
- Upload plugin zip for claude.ai / Cowork (Customize > Plugins > Upload): `python tools/package_skill.py --plugin`.
- Upload skill zip for claude.ai: `python tools/package_skill.py` (excludes tests and fixtures; bundles every
  policy.json/endpoints.json into `sources/packs.json`, so runtime code reads them via `core.packfiles`).
  claude.ai rejects a zip of more than 200 files; the packager and tests/test_structure.py enforce it.
- Hard limits in tests/test_structure.py: SKILL.md description <= 1024 chars (claude.ai rejects longer) and
  body <= 500 lines (Anthropic guidance); fixtures < 200 KB. source.md length is guidance only.
- Never write inside the skill folder at run time; never print or store keys; never invent data.
- Maintainer skill `rsi-data-analyst` lives in its own project, `C:\zsu\Projects\rsi-data-analyst` (not in this repo or the plugin).
  Load it with `claude --plugin-dir C:\zsu\Projects\rsi-data-analyst` and run `/rsi-data-analyst:rsi-data-analyst [ids] [--repo <this repo>] [--test]`
  (it finds this checkout from `--repo`, env `DATA_ANALYST_REPO` or the working directory, else asks). It checks packs live and against
  agency docs, fixes drift, and optionally runs a random multi-source question end to end. Its probe records the last live check of
  each pack in `docs/source_status.json` here, and run artifacts go to `out/rsi/`.
