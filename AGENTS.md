# data-analyst (maintainers)

One Agent Skills skill (`plugins/data-analyst/skills/data-analyst/`, shipped as the `data-analyst` plugin for Claude and Codex) for U.S. government data. Each source is a pack in
`sources/<id>/` following `docs/pack_contract.md`; its tests are in `tests/sources/<id>/`; shared code is in `scripts/core/`.

- Tests: `python -I tests/run_all.py` (offline; fixtures). One pack: `python -I tests/run_all.py <id>`.
- Record fixtures from a live call: `DATA_ANALYST_RECORD=tests/sources/<id>/fixtures python .../fetch.py --source <id> ...`.
- After adding or changing a pack's policy.json: `python tools/gen_routing.py` (the tests fail when routing.md is stale).
- Plugin layout: one shared `skills/` folder, one thin manifest per assistant (as obra/superpowers does).
  Claude: `plugins/data-analyst/.claude-plugin/plugin.json`; Codex: `plugins/data-analyst/.codex-plugin/plugin.json`
  (`"skills": "./skills/"`). There is no marketplace in this repo: the `zsu` marketplace lives on `main` of
  `zsu/agent-plugins` (Claude `.claude-plugin/marketplace.json` and Codex `.agents/plugins/marketplace.json`).
  It installs the plugin from this repo with a `git-subdir` source (`plugins/data-analyst`, ref `main`).
  Version lives in both plugin.json files (never in a catalog entry) and must match (tests/test_plugin.py):
  bump both per release (users update on a new version). Check with
  `claude plugin validate plugins/data-analyst`. Run it locally without installing:
  `claude --plugin-dir plugins/data-analyst`. No CLAUDE.md or AGENTS.md
  inside the plugin folder (not loaded from there).
- Keep the skill assistant-neutral: SKILL.md names actions ("your question tool"), and
  `references/agent_tools.md` maps them per assistant; tests/test_structure.py rejects tool names in SKILL.md.
  User-facing messages say "the assistant", not "Claude". Keys: the setup scripts write `~/.config/data-analyst/keys.json`
  (`$DATA_ANALYST_CONFIG_DIR`); `core.keys` also reads Claude Code's settings.json for keys stored there earlier.
- Upload plugin zip for claude.ai / Cowork (Customize > Plugins > Upload): `python tools/package_skill.py --plugin`.
- Upload skill zip for claude.ai (also a plain Agent Skills folder any assistant can load): `python tools/package_skill.py` (excludes tests and fixtures; bundles every
  policy.json/endpoints.json into `sources/packs.json`, so runtime code reads them via `core.packfiles`).
  claude.ai rejects a zip of more than 200 files; the packager and tests/test_structure.py enforce it.
- Hard limits in tests/test_structure.py: SKILL.md description <= 1024 chars (claude.ai rejects longer) and
  body <= 500 lines (Anthropic guidance); fixtures < 200 KB. source.md length is guidance only.
- Never write inside the skill folder at run time; never print or store keys; never invent data.
- Prefer a gate to a sentence: when a script can check the substance of a SKILL.md rule (not just that a field
  is filled), enforce it in `scripts/report/` with a FAIL that says what to do, and add a test that the gate
  fails on the real mistake. Evidence a tool writes (`specs/checked.json`, `references/opened.json`, facts
  reproduced by `--rerun`) beats evidence the agent types. Rules only judgment can check stay prose and get an
  eval criterion.
- A behavioral change to SKILL.md or a source.md ships with an eval result: the changed skill vs `main` on the
  affected cases in `evals/data-analyst/evals.json`, at least 3 runs each (rsi-data-analyst `--test`), with a
  deterministic-check pass rate no worse. Each bug fixed adds a case.
- Maintainer skill `rsi-data-analyst` lives in its own project, `C:\zsu\Projects\rsi-data-analyst` (not in this repo or the plugin).
  Load it with `claude --plugin-dir C:\zsu\Projects\rsi-data-analyst` and run `/rsi-data-analyst:rsi-data-analyst [ids] [--repo <this repo>] [--test]`
  (it finds this checkout from `--repo`, env `DATA_ANALYST_REPO` or the working directory, else asks). It checks packs live and against
  agency docs, fixes drift, and optionally runs a random multi-source question end to end. Its probe records the last live check of
  each pack in `docs/source_status.json` here, and run artifacts go to `out/rsi/`.
