# Changelog

All notable changes to nakon are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **`catalog check` did not catch a missing-var pin.** A bare-name (or var-incomplete
  `{"name","vars"}`) request of a configuration whose script reads a variable the request never
  supplies now fails with error code `missing-vars`, naming the vars and the exact JSON to pin; it
  previously reported the selection clean and the plant then failed mid-deploy (rc=2). Every
  `catalog list` / `catalog show --json` row now carries `required_vars`, derived from the script
  body because the catalog has no such column — with the same rules as tezcatlipoca's deploy-time
  bundle lint (`nakon_ops._lint_bundle_vars`); keep the two in sync. PowerShell rows are
  deliberately not derived, matching that lint.
## [0.1.7] — 2026-09-23

### Fixed
- `gen/bash.py` wrote a configuration's `run_as` unescaped into a `#` comment line in run.sh. A
  newline in a catalog `run_as` value ended the comment and ran the rest as root on the box.
  Whitespace in it is now collapsed to single spaces, as step labels already are.

### Docs
- README documents `catalog check --boxes-json`.

## [0.1.6] — 2026-09-20

### Fixed
- `catalog/query.py` used a `dict | None` annotation, a `TypeError` at import on Python 3.9 (the
  declared minimum): every `nakon catalog …` command and `nakon randomize --json` died there.
- `identity_file` in config.json was read by `deploy/ssh.connect` but dropped by `load_machines`
  first, so key-based SSH never worked. It is carried through now and documented.
- Windows step scripts were written with `\r\r\n` on every nakon-added line (`render_step_ps1`
  emitted CRLF and the writer translated `\n` again). PowerShell tolerated it; the file on disk now
  matches the hashed body. Existing bundles pick it up on `nakon build --rebuild`.
- Windows `force_cleanup` fired its `Remove-Item` and returned without waiting; the client was
  then closed and the plan directory could survive a failed run. It now waits (bounded).
- `nakon diff` missed `depends_on` drift: it only compared rows the bundle recorded, so a
  dependency added/removed/renamed elsewhere in the graph reported "no drift". It now records
  `depends_on` in provenance and re-resolves every request against the live catalog.
- A log-writing error after a machine finished escaped `deploy_machine` and, under `--jobs`,
  aborted the whole run from `future.result()`. Ctrl-C during a parallel deploy used to keep
  starting every queued machine; it now cancels those and waits only for in-flight ones, which
  finish and remove their own plan directory.
- `NAKON_STRICT=0` counted as strict.
- **Answer key left on the box after an interrupted run.** Found live: after Ctrl-C (or a dropped
  connection) mid-deploy, the unpacked plan directory under `/root` survived, because
  `deploy_machine` only learned its path after `run_streaming` returned normally, so cleanup
  removed the archive and bootstrap but not the plan itself. The path is now tracked as the
  bootstrap announces it, and the Linux cleanup also removes the leftover
  `apt.conf.d/99nakon-lock-timeout` fragment. Verified on Ubuntu 24.04 and Windows Server 2022.
- The end-of-plan marker re-printed the last step's result line in the live deploy output.

### Added
- `nakon/machines.py`: `load_machines` (with config.json validation — missing `ip`/`user`,
  non-string `os`, malformed `configurations` entries are `BundleError`s naming the machine and
  field; an empty `configurations` list is a warning) and `os_to_platform`, stdlib only, so
  `nakon deploy` no longer imports the build package for them. Old import paths still work.
- `resolve()` rejects `vars` keys that are not shell identifiers; bash sourced them under `set -a`
  as root, PowerShell failed to parse them. Reported through `catalog check` as `unresolvable`.
- Step labels collapse whitespace so a tab/newline in a catalog name cannot break the
  tab-delimited `##nakon rc` / report.tsv protocol.
- `nakon build` warns when a Linux catalog script contains carriage returns (bash fails on CRLF).
- The "no output from the remote plan" error names the actual Windows cause (non-admin SSH user).

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
