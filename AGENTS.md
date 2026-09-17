# AGENTS.md — nakon

Guidance for agents (and humans) working in or against this repo. If you've landed here
from another repo, this tells you what nakon is, how to drive it, and where it fits.

## What this is

`nakon` turns the **vulndb** configuration catalog into a content-addressed **bundle**, then
applies that bundle to target machines over SSH. Purpose: stand up intentionally-vulnerable
boxes for blue/red team exercises from a central catalog instead of configuring each box by hand.

## Ecosystem position

```
vulndb-ui (catalog: MySQL + HTTP API + MinIO media)
   ▲
   │ read catalog (HTTP/MySQL) — nakon's own data source
   │
 nakon  ◀── submodule + CLI ──  huitzilopochtli, tezcatlipoca
```

nakon is a **shared dependency** (git submodule, invoked as a CLI) of **huitzilopochtli** and
**tezcatlipoca**. Consumers shell out to `python3 -m nakon …` and parse `--json` from stdout;
they do **not** import nakon internals. The catalog itself lives in **vulndb-ui** (the
`vulndb-cli` repo is the sanctioned client for editing it).

## Layout

```
nakon/
  __init__.py        public API (lazy) + __version__ (single source of truth)
  __main__.py        `python3 -m nakon` entry
  cli.py             argparse CLI — all subcommands
  errors.py          NakonError hierarchy
  hashing.py         content-addressing (sha256, short ids)
  build/             BUILD side — needs mysql-connector + requests + python-dotenv
    builder.py       resolve catalog → bundle
    fetch.py         pull attachment bytes from vulndb-ui/MinIO
    tarball.py       export a bundle to .tar.gz
  catalog/           read + vet the catalog (build side, except where noted)
    source.py        MySQLCatalog, HttpCatalog, DictCatalog (test double)
    query.py         list / describe / check_selection; open_source()
    randomize.py     pick_configurations() selection algorithm + interactive main()
    resolve.py       expand depends_on into ordered steps
  deploy/            DEPLOY side — needs paramiko ONLY (never imports build/catalog)
    bundle.py        load + index a saved bundle
    runner.py        apply plans over SSH, summarize
    ssh.py           paramiko transport
    report.py        per-step outcome formatting
  gen/               bash/powershell script generators
config-example.json  sample machine list
```

The build/deploy split is load-bearing: `nakon deploy` runs on the locked-down scoring engine
with only paramiko installed (often from a plain directory copy, no install). Never add a
build-side import (`mysql.connector`, `requests`, `dotenv`) at module scope in anything `deploy`
imports — keep those inside the function that needs them. See `cli.py` and `catalog/source.py`
header comments.

## Run / build / test

```bash
pip install -e '.[build]'    # build host: mysql-connector, requests, python-dotenv
pip install -e '.[deploy]'   # deploy host: paramiko
# Installing is optional — `python3 -m nakon` runs from a plain directory copy.
```

```bash
python3 -m nakon --version
python3 -m nakon build   --config config.json --json
python3 -m nakon deploy  --bundle bundles/<id> --json
python3 -m nakon catalog list --json
```

There is **no automated test suite**. Verify changes by exercising the CLI against a reachable
vulndb (or a `DictCatalog` fixture in an ad-hoc script — see `catalog/source.py`). When you
change the selection algorithm or build output, sanity-check with `catalog check` and `build --json`.

## Conventions & gotchas

- **Output contract:** progress/diagnostics → **stderr**; the command result → **stdout**.
  `--json` prints exactly one JSON object. tezcatlipoca reads `stdout.splitlines()[-1]` from
  `build --json`, so keep build's JSON as the sole stdout line. Don't break this.
- **`name` is the join key** across the whole ecosystem (nakon config.json, `depends_on`,
  tezcatlipoca's box_vulns.json). `id` only appears in URLs.
- **Building blocks** (`install-package`, `create-user`, `enable-service`) are meant to be
  pulled in via `depends_on`, never selected directly — they plant a no-op. `EXCLUDED_NAMES`
  in `catalog/randomize.py` is the canonical list.
- **`build` always uses MySQL**, never the HTTP API: the HTTP API omits attachments'
  `object_key`, which the source fingerprint hashes. `catalog` commands may use HTTP (no DB
  credentials needed) — that's the agent-friendly read path.
- **Bundles pin script content, not config names.** Re-deploying an old bundle applies old
  scripts even after the catalog changes. `nakon diff` reports that drift.
- **Version** lives in `nakon/__init__.py:__version__` and is read dynamically by pyproject. It
  is a static string (not setuptools-scm) on purpose — nakon runs from bare directory copies
  with no git history. See "Releasing" in README.

## Integration contract (for consumers)

Invoke as a subprocess; do not import internals.

```bash
# Build a bundle (run where the vulndb is reachable; cwd must let nakon read its .env)
python3 -m nakon build --config <abs-path>/config.json --out bundles --json
# last stdout line = {"bundle_id","path","cached":bool,"plans","machines"}

# Deploy (run where the boxes are reachable)
python3 -m nakon deploy --bundle bundles/<id> --config <abs-path>/config.json --json

# Non-interactive random selection — replaces the old in-process import
python3 -m nakon randomize --platform linux --services 2 --vulns 3 --source auto --json
# stdout = {"platform":"linux","services":[...],"vulns":[...]}

# Read-only catalog browse (safe to point an agent at)
python3 -m nakon catalog list   --platform linux --json
python3 -m nakon catalog show   <name> --json
python3 -m nakon catalog check  --select a,b,c --json      # exit 0 clean, 1 errors
```

**Env vars** (build side; `.env` auto-loaded when python-dotenv is present):
`host`, `user`, `password`, `database` (MySQL), `VULNDB_UI_URL` (HTTP catalog + media).

Exit codes: `0` success, `1` on `NakonError`, `2` for `diff`/`check` findings, `130` interrupt.

## Choosing a competition's misconfigurations (agent workflow)

**nakon supplies context and verification; the agent supplies judgement.** Nothing here ranks
configurations or calls a model. The loop:

```
nakon catalog list --json   →  read what exists (read the script, not just the description)
        ↓
(you decide)                →  pick a set per box, keyed by box TYPE (not per team)
        ↓
nakon catalog check --box-vulns … --json   →  have nakon vet the selection
        ↓
competitions/<id>/box_vulns.json  (+ box_services.json)   →  write the answer where deploy reads it
```

A good set: keep the box plausible for its role; mix obvious and subtle; don't repeat a
technique; watch what a misconfiguration drags in (`unauthorized-ftp-server` installs vsftpd);
respect the box's scored services; scale difficulty by mix, not just count. `splunk` and
`roundcube` are slow (large installs) — fine to choose deliberately, excluded from random picks.

`check` catches what nothing else does — most importantly the **typo**: an unknown name is not
an error anywhere else (`resolve` treats it as a raw package and `apt-get install`s it, planting
nothing). Error codes: `unknown-name`, `building-block`, `platform-mismatch`/`type-mismatch`,
`unresolvable`. Warning codes: `no-op`, `duplicate`, `implicit-services`, `no-description`.

Both pin files are keyed by box type; every team must defend the identical set so the scoring
engine's wildcard-IP checks work. Either file alone pins a competition; if neither exists the
driver randomises and writes both, so a re-run reproduces it.

A configuration with no description is one nothing can reason about. If you work out what one
does, write it down with `vulndb-cli describe <name> "<prose>" --yes`.
