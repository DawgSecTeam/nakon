# Changelog

All notable changes to nakon are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.5] — 2026-09-17

### Security
- `render_run_sh`/`render_run_ps1` interpolated a step's raw catalog config name into the
  `##nakon begin` marker line inside a double-quoted `echo`/`Write-Output` string. Catalog
  names have no charset restriction, so a name containing `$(...)` (bash) or `$(...)`
  (PowerShell) executed as a subexpression in the elevated deploy context. The line below it
  already passed the same name through `shlex.quote()`/`ps_quote()` correctly — the marker
  line now does too.

### Fixed
- `render_run_sh`'s live `##nakon rc` marker was space-delimited while `deploy/report.py`
  parses it tab-delimited (matching the PowerShell driver, which already emitted it correctly).
  Only visible on the mid-deploy failure fallback path (SSH channel dies before the final
  `report.tsv` dump), where Linux per-step rc/seconds silently never populated.

### Documented
- `AGENTS.md`'s `build --json` integration contract example named a `built` key that
  `build/builder.py` never emits; corrected to the real key, `cached`.

## [0.1.3] — 2026-08-15

### Fixed
- `catalog.resolve()` emitted a second, vars-less copy of a configuration that was both
  requested directly (with vars) and pulled in by another request's `depends_on` — e.g.
  "Elevate User Account" depends on "Add User Account", so a plan carrying both produced
  step 000 Add (with vars, works) and step 001 Add again (no vars, `New-ADUser` fails on
  null parameters). A bare re-request (empty vars) of an already-emitted configuration is
  now satisfied by the earlier emission; requests carrying their own vars still emit per
  distinct set, so the create-user-for-several-usernames pattern is unchanged. Found live
  in tezcatlipoca's win-linux-practice deploy.

## [0.1.2] — 2026-08-14

### Fixed (vulndb catalog content, not this package's code)
- `ADDS` (promotes a box to a new AD forest) was missing `-SafeModeAdministratorPassword` —
  `Install-ADDSForest` always interactively prompts for it when omitted, which nakon's
  non-interactive transport could never satisfy. Now takes a `$dsrm_password` var. Also fixed:
  a missing Windows client-side domain-join config (new row, "Domain Join"), and several other
  Windows catalog rows that failed under nakon's non-interactive PowerShell session (missing
  `-NoRestart`, missing registry keys, non-idempotent re-runs) — see tezcatlipoca's session notes
  for the full list; none of these are file changes in this repo.

### Documented
- Windows deploy path (`render_bootstrap_ps1`) and the AD/domain-controller path are both
  verified end-to-end against real boxes, including a full domain-join scenario driven through
  tezcatlipoca. Only the `winget`/`choco` package-manager fallback remains untested.

## [0.1.1] — 2026-08-13

### Fixed
- Corrected the setuptools table layout in `pyproject.toml` so editable installs and wheel builds
  work with the dynamically read `__version__`.

## [0.1.0] — 2026-08-13

First versioned release. Resets the version number (the codebase previously carried an un-tagged
`2.0.0` string with no releases or tags); from here on releases are git-tagged `vX.Y.Z`.

### Added
- Non-interactive `nakon randomize` mode: `--platform`/`--os`, `--services`/`--vulns`,
  `--difficulty`, `--exclude`, `--source`, `--json`. Emits `{"platform","services","vulns"}` so
  orchestrators (tezcatlipoca) can pick a selection via the CLI instead of importing
  `nakon.catalog.randomize` internals.
- `AGENTS.md` (agent/integration context) and `CHANGELOG.md`.
- Version is now single-sourced from `nakon/__init__.py:__version__`, read dynamically by
  `pyproject.toml`.

### Changed
- Consumers should integrate via the **CLI** (`python3 -m nakon … --json`), not by importing
  internal modules. The public lazy API (`build`, `deploy`, `summarize`, `Bundle`,
  `load_machines`) remains for embedders.

### Removed
- `randomize_config.py` root compatibility shim (deprecated; no consumer loads it by path anymore).
