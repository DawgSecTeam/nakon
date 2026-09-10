# nakon

`nakon` turns the **vulndb** configuration catalog into a **content-addressed bundle**, then
applies that bundle to target machines over SSH. It stands up intentionally-vulnerable boxes for
blue/red team exercises from a central catalog instead of configuring each box by hand.

> For agent/integration context — layout, contracts, the ecosystem map, and the misconfiguration
> selection workflow — see **[AGENTS.md](AGENTS.md)**.

## Two commands, two places

```
                  (needs the vulndb)                    (needs the boxes)
config.json ──▶ nakon build ──▶ bundles/<id>/ ──▶ nakon deploy ──▶ targets
.env + MySQL ──┘   + vulndb-ui/MinIO media          paramiko only
```

- **`nakon build`** runs wherever the vulndb is reachable. It resolves every machine's dependency
  graph, downloads attachment media, and writes a self-contained bundle of flat generated scripts.
- **`nakon deploy`** runs wherever the target boxes are reachable (for competitions, the scoring
  engine). It needs `paramiko` and a bundle — **no database, no vulndb-ui, no `.env`**.

Two properties fall out of that split:

1. **Saved competitions replay exactly.** A bundle pins script *content*, not configuration
   *names*. Re-deploying an old bundle applies old scripts even after the catalog changes;
   `nakon diff` reports the drift.
2. **The deploy host never holds vulndb credentials.**

A bundle does **not** pin the internet — `apache`/`nginx`/`ssh`/`install-package` still call
`apt-get install`, and `splunk` still downloads ~500 MB. Reproducible inputs, not offline install.

## Setup

```bash
pip install -e '.[build]'    # build host:  mysql-connector-python, requests, python-dotenv
pip install -e '.[deploy]'   # deploy host: paramiko (only)
```

Installing is optional — `python3 -m nakon` runs from a plain directory copy, which is how the
scoring engine gets it (tezcatlipoca scp's the package directory over).

`.env` in the project root (gitignored) — **build side only**:

```env
host=127.0.0.1
user=nakon
password=your-db-password
database=vulndb
VULNDB_UI_URL=http://10.0.0.119:3000
```

`config.json` lists the target machines (gitignored — live credentials and IPs). See
`config-example.json`. Generate one interactively with `python3 -m nakon randomize`.

## Quickstart

```bash
python3 -m nakon build                       --json   # -> bundles/<id>/ (or reuse a cache hit)
python3 -m nakon show   --bundle bundles/<id>         # what's in it (offline)
python3 -m nakon diff   --bundle bundles/<id>         # has the catalog moved since?
python3 -m nakon deploy --bundle bundles/<id> --json

python3 -m nakon randomize --platform linux --services 2 --vulns 3 --json  # non-interactive pick
python3 -m nakon catalog list   --platform linux --json                   # browse the catalog
python3 -m nakon catalog show   suid-find --json
python3 -m nakon catalog check  --select suid-find,ssh-root-login --json   # vet a selection
```

**Output convention:** progress/diagnostics → **stderr**; the result → **stdout**. `--json`
prints one object, so `nakon build --json | jq` works and pipes stay clean.

## The catalog

A **MySQL database** stores `configurations` (managed by **vulndb-ui**). Each row is one named,
reusable unit: `name` (kebab-case slug, the join key across the whole ecosystem), `description`
(prose — the only place difficulty/realism/couplings are recorded), `platform`
(`linux`/`windows`/`other`), `category` (`misconfiguration`/`service`/`vulnerability`), `type`
(`bash`/`powershell`/`command`), `script`, `run_as`, and `depends_on`. An `attachments` table
(MinIO-backed) holds files a script needs alongside it. A vulnerability and a service install are
both just configurations — list one and it always runs.

`depends_on` entries are either a bare package name (installed via the host package manager) or
`{"name": "create-user", "vars": {"USERNAME": "splunk"}}` — `vars` surface as shell variables in
the script. vulndb-ui ships reusable blocks to depend on rather than duplicate: `install-package`
(`PACKAGE`), `create-user` (`USERNAME`), `enable-service` (`SERVICE`).

Attachments download at **build** time and travel in the bundle; each step gets its own working
directory, so a script refers to them by relative path (`cp ./malicious.conf /etc/vsftpd.conf`).

### `randomize`

`nakon randomize --services N --vulns M --json` picks a platform's worth of services and
vulns/misconfigs from the catalog and prints `{"platform","services","vulns"}`. Budgets are upper
bounds — a small catalog yields fewer. `--difficulty 1-10` derives them (services = ⌈d/3⌉, vulns =
d); `--os ubuntu24.04` maps free text to a platform; `--exclude NAME…` removes entries from the
pool. With no flags, it runs the original interactive flow (prompts for VMs/difficulty, writes
`config.json`). `--source auto|http|mysql` chooses where to read.

### `catalog` — read and vet

Read-only; never builds or writes, so it is safe to point an agent at.

```bash
nakon catalog list  --platform linux --category misconfiguration --json
nakon catalog show  www-data-shell --json
nakon catalog check --box-vulns competitions/x/box_vulns.json \
                    --box-services competitions/x/box_services.json
```

`check` catches what nothing else does. Most importantly the **typo**: an unknown name is not an
error anywhere else — `resolve` treats it as a raw package and `apt-get install`s it, planting
nothing. It also catches building blocks requested directly, windows configs on linux boxes,
dependency cycles, empty-script no-ops, duplicates and implicitly-pulled services. Exit `0` clean,
`1` on errors, `--strict` to fail on warnings too.

`--source auto` (default) reads vulndb-ui over HTTP when `VULNDB_UI_URL` is set — **no database
credentials needed** — and falls back to MySQL. `build` always uses MySQL directly, because the
HTTP API omits attachments' `object_key`, which the source fingerprint hashes.

## What a bundle looks like

```
bundles/
  .cache/attachments/<sha256>       download cache — deletable, never referenced by a manifest
  <bundle-id>/
    manifest.json                   provenance, request→plan map, per-step detail
    blobs/<sha256>                  scripts and media, deduped
    plans/<plan-id>/
      run.sh | run.ps1              generated, straight-line driver
      steps/NNN-<name>.sh           each catalog script, byte-for-byte
      vars/NNN.env                  shell-quoted variables for that step
      files/NNN/                    that step's attachments = its working directory
    plans/<plan-id>.tar.gz          per-plan transport slice
```

Plans are keyed by the **request** (platform + ordered configuration list), never by machine name.
Every team gets the same list per box type, so one team's `config.json` deploys all teams. Nothing
is interpreted on the target — `run.sh` is flat generated bash; script bodies are never inlined,
so one configuration's failure never aborts the rest of the box.

## deploy flags

| flag | effect |
|---|---|
| `--jobs N` | deploy N machines in parallel (default 1) |
| `--strict` | exit non-zero if any configuration failed (same as `NAKON_STRICT=1`) |
| `--only NAME...` | deploy just these machines, by name or IP |
| `--keep-remote` | leave the unpacked plan on a box for debugging |
| `--no-logs` | don't save per-machine logs under `bundles/<id>/runs/` |
| `--json` | per-step results as one JSON object on stdout |

A configuration exiting non-zero is reported, not fatal; one unreachable machine never stops the
others. `--strict` is for an orchestrator that wants to abort. The plan unpacks to a `mktemp -d`
under `/root` (mode 0700) and is **deleted when the run finishes** — don't leave `--keep-remote`
on for a live event.

## Using nakon from another project

The intended integration is a **subprocess CLI**: `python3 -m nakon …` with `--config`/`--out`
taking paths, so a consumer keeps its own files in its own repo. Parse `--json` from stdout; logs
are on stderr. (huitzilopochtli and tezcatlipoca both consume it this way, as a git submodule at
`vendor/nakon`.)

To embed instead: `pip install -e path/to/nakon` and

```python
from nakon import build, deploy, summarize, Bundle, load_machines
```

These resolve lazily, so `import nakon` on a deploy host still doesn't pull in the build half.
`pyproject.toml` declares **no required dependencies** — the two halves are extras
(`nakon[build]`, `nakon[deploy]`) — because the scoring engine often runs nakon from a plain
directory copy with only paramiko installed.

## Windows

Windows configurations generate a `run.ps1` with a real elevation guard — nakon does not
self-elevate over SSH, so the SSH principal must already be an administrator. Transport is
OpenSSH + SFTP with a zip instead of a tarball. Each step self-reports a real exit code (external
command's `$LASTEXITCODE`, else whether `$Error` collected anything), because most catalog scripts
are cmdlet-only and never set `$LASTEXITCODE` on their own.

Verified end-to-end against real Windows Server 2022 boxes, including the full domain-join
scenario: `ADDS` promotes a box to a new AD forest, `Domain Join` (`Add-Computer`) joins another
box to it, and AD-aware catalog rows (`Add User Account`/`Elevate User Account`/`Disable System
Firewall`) correctly create/elevate real AD objects once run against a live DC — driven through
tezcatlipoca's `create-competition.py`/`deploy_windows_domain_configs()`, which handles the
reboot-tolerant sequencing this needs (ADDS and Domain Join each reboot the box, so each has to
run in its own single-config deploy pass, and cloning has to happen before any DC is promoted,
never after). The one remaining untested corner is the `winget`/`choco` package-manager fallback
(`render_package_step`) — no catalog config exercised in this pass used it.

## Releasing

1. Bump `__version__` in `nakon/__init__.py`.
2. Add a `CHANGELOG.md` entry.
3. `git tag vX.Y.Z && git push --tags`.

The version is a single static string (not setuptools-scm): nakon runs from bare directory copies
with no git history, so the version must be readable with nothing installed.

## Security note

This tool intentionally deploys vulnerable configurations and SSHes around with plaintext
passwords from `config.json`/`.env`. Built bundles contain the full list of what gets planted
where. Isolated competition/lab environments only — never point it at production or expose these
credentials.
