"""Command-line entry point.

Import discipline matters here. `nakon deploy` runs on the scoring engine, which has paramiko
and nothing else — no mysql-connector, no requests, no python-dotenv. So every build-side
import is done inside the subcommand that needs it. A top-level `import mysql.connector` in
this file would make deploy fail on the one host that has to run it.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .errors import BundleError, NakonError
from .hashing import short


def _log(message=""):
    """Progress and diagnostics go to stderr; stdout carries the command's result.

    That separation is what lets a caller do `nakon build --json | jq` — or pipe `diff`'s drift
    lines into something — without having to strip log noise first. It also keeps the older
    contract working: tezcatlipoca reads `stdout.splitlines()[-1]` from `build --json`, and with
    logs on stderr that JSON is simply the only line there.
    """
    print(message, file=sys.stderr)


def _load_env():
    """Load .env if python-dotenv is available. Build-side convenience only."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _default_config(args) -> Path:
    return Path(args.config)


def cmd_build(args) -> int:
    _load_env()
    from .build.builder import build
    from .catalog.source import MySQLCatalog

    source = MySQLCatalog.from_env()
    try:
        result = build(
            source,
            Path(args.config),
            Path(args.out),
            rebuild=args.rebuild,
            log=_log,
        )
    finally:
        source.close()

    if args.export:
        _export(Path(result["path"]), Path(args.export))
        _log(f"[nakon] exported {short(result['bundle_id'])} to {args.export}")

    if args.json:
        print(json.dumps(result))
    return 0


def _export(bundle_dir: Path, dest: Path) -> None:
    """Archive a whole bundle for archival next to a saved competition."""
    from .build.tarball import write_tar_gz

    write_tar_gz(bundle_dir, dest)


def cmd_deploy(args) -> int:
    from .build.builder import load_machines
    from .deploy.bundle import Bundle
    from .deploy.runner import deploy, summarize

    bundle = Bundle.load(args.bundle)
    machines = load_machines(Path(args.config))

    if args.only:
        wanted = set(args.only)
        machines = [m for m in machines if m["name"] in wanted or m["ip"] in wanted]
        if not machines:
            raise NakonError(f"--only {sorted(wanted)} matched no machine in {args.config}")

    _log(f"[nakon] bundle {short(bundle.bundle_id)} "
         f"(built {bundle.manifest.get('built_at')} by {bundle.manifest.get('built_by')})")

    # Pre-flight: every machine must map to a plan before anything is deployed to any of
    # them. A machine the bundle doesn't cover is an operator error — the wrong bundle, or a
    # config.json that drifted since it was built — and it means nothing at all would be
    # applied to that box. That is categorically different from a host being unreachable, so
    # it fails hard rather than being collected as a per-machine failure and reported at the
    # end with exit 0, which would let an orchestrator run `check=True` straight past it.
    uncovered = []
    for machine in machines:
        try:
            bundle.plan_for(machine)
        except BundleError as exc:
            uncovered.append(str(exc))
    if uncovered:
        for message in uncovered:
            _log(f"[nakon] error: {message}")
        _log(f"\n[nakon] {len(uncovered)} machine(s) are not covered by this bundle; "
             f"nothing was deployed.")
        return 1

    _log(f"[nakon] deploying {len(machines)} machine(s)"
         + (f" with {args.jobs} in parallel" if args.jobs > 1 else ""))

    log_dir = None
    if not args.no_logs:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir = Path(bundle.path) / "runs" / stamp

    outcomes = deploy(
        bundle,
        machines,
        keep_remote=args.keep_remote,
        jobs=args.jobs,
        log_dir=log_dir,
        emit=_log,
    )
    failures = summarize(outcomes, emit=_log)

    if log_dir is not None and log_dir.exists():
        _log(f"[nakon] logs: {log_dir}")

    if args.json:
        from .deploy.runner import outcomes_to_dict

        print(json.dumps(outcomes_to_dict(bundle, outcomes, log_dir)))

    strict = args.strict or os.getenv("NAKON_STRICT")
    if failures and strict:
        return 1
    return 0


def cmd_diff(args) -> int:
    _load_env()
    from .catalog.source import MySQLCatalog
    from .deploy.bundle import Bundle
    from .hashing import sha256_text

    bundle = Bundle.load(args.bundle)
    provenance = bundle.manifest.get("provenance", {})
    recorded = provenance.get("configurations", {})
    recorded_attachments = provenance.get("attachments", {})

    source = MySQLCatalog.from_env()
    changes = []
    try:
        for config_id, entry in sorted(recorded.items(), key=lambda kv: kv[1]["name"]):
            live = source.fetch(entry["name"])
            if live is None:
                changes.append(("removed", entry["name"], "no longer in the catalog"))
                continue
            if sha256_text(live["script"]) != entry["script_sha256"]:
                changes.append(("changed", entry["name"], "script differs"))
            if (live.get("run_as") or "root") != entry["run_as"]:
                changes.append(
                    ("changed", entry["name"],
                     f"run_as {entry['run_as']} -> {live.get('run_as')}")
                )
            if (live.get("type") or "bash").lower() != entry["type"]:
                changes.append(
                    ("changed", entry["name"], f"type {entry['type']} -> {live.get('type')}")
                )
            live_ids = {str(a["id"]) for a in live["attachments"]}
            bundled = {
                aid for aid, a in recorded_attachments.items() if a["configuration"] == entry["name"]
            }
            for gone in sorted(bundled - live_ids):
                changes.append(
                    ("removed", entry["name"],
                     f"attachment {recorded_attachments[gone]['original_name']} deleted")
                )
            for added in sorted(live_ids - bundled):
                name = next(a["original_name"] for a in live["attachments"] if str(a["id"]) == added)
                changes.append(("added", entry["name"], f"new attachment {name}"))
    finally:
        source.close()

    if args.json:
        print(json.dumps({
            "bundle_id": bundle.bundle_id,
            "recorded": len(recorded),
            "drift": [{"kind": k, "name": n, "detail": d} for k, n, d in changes],
        }, indent=2, sort_keys=True))
        return 2 if (changes and args.exit_code) else 0

    _log(f"[nakon] bundle {short(bundle.bundle_id)} vs the live catalog "
         f"({len(recorded)} configuration(s) recorded)")
    if not changes:
        _log("[nakon] no drift — the catalog still matches this bundle.")
        return 0

    _log(f"[nakon] {len(changes)} difference(s):")
    for kind, name, detail in changes:
        print(f"{kind:8} {name}: {detail}")
    _log("[nakon] Deploying this bundle still applies the ORIGINAL content above; "
         "run `nakon build` to pick up the changes.")
    return 2 if args.exit_code else 0


def cmd_show(args) -> int:
    from .deploy.bundle import Bundle

    bundle = Bundle.load(args.bundle)
    manifest = bundle.manifest

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    print(f"bundle       {manifest['bundle_id']}")
    print(f"fingerprint  {manifest['source_fingerprint']}")
    print(f"built        {manifest.get('built_at')} by {manifest.get('built_by')}")
    print(f"nakon        {manifest.get('nakon_version')}")
    src = manifest.get("source", {})
    print(f"catalog      {src.get('database')}@{src.get('db_host')}  media {src.get('vulndb_ui_url')}")

    print(f"\nplans ({len(manifest['plans'])})")
    for plan_id, plan in manifest["plans"].items():
        machines = [
            entry["name"]
            for entry in manifest.get("inventory", [])
            if manifest["requests"].get(entry["request_key"], {}).get("plan_id") == plan_id
        ]
        print(f"  {short(plan_id)}  {plan['platform']}  {len(plan['steps'])} step(s)  "
              f"{plan['tarball_bytes']} bytes")
        # NOTE: no backslash inside an f-string expression -- that is only
        # legal from Python 3.12 (PEP 701) and this package supports >=3.9;
        # an escaped quote here was a SyntaxError on 3.9-3.11 (cli import
        # died before argparse ran, breaking EVERY command, not just `plans`).
        _no_inv = "(none in this bundle's inventory)"
        print(f"    machines: {', '.join(machines) or _no_inv}")
        for step in plan["steps"]:
            if step["kind"] == "package":
                print(f"    {step['index']:>3}  package  {step['package']}")
            else:
                extra = f"  vars={step['vars']}" if step["vars"] else ""
                files = (
                    "  files=" + ",".join(a["original_name"] for a in step["attachments"])
                    if step["attachments"] else ""
                )
                print(f"    {step['index']:>3}  {step['type']:<10} {step['name']}"
                      f"  run_as={step['run_as']}{extra}{files}")
    return 0


def cmd_randomize(args) -> int:
    _load_env()

    # Non-interactive selection mode: any selection spec (--platform/--os, --services/--vulns,
    # --difficulty, --exclude, or --json) selects one platform's worth of services+vulns and
    # prints it. This is what an orchestrator (tezcatlipoca's generate_nakon_config) calls
    # instead of importing nakon.catalog.randomize internals — it keeps the selection algorithm
    # in one place behind a stable CLI. With no spec at all, fall through to the original
    # interactive standalone flow (prompts for VMs/difficulty, writes config.json).
    import math

    has_spec = any(
        v is not None
        for v in (args.platform, args.os, args.services, args.vulns, args.difficulty)
    ) or args.exclude or args.json
    if has_spec:
        return _randomize_select(args, math)

    from .catalog.randomize import main as randomize_main

    randomize_main()
    return 0


def _randomize_select(args, math) -> int:
    """Headless `nakon randomize`: read the catalog, pick services+vulns, emit JSON.

    Budgets come either explicitly (--services/--vulns) or derived from --difficulty exactly as
    the standalone interactive main() does (services = ceil(d/3), vulns = d), then handed to
    pick_configurations() uncapped — it simply stops accepting once the budget is met, so a small
    catalog yields fewer rather than erroring.
    """
    from .catalog.query import open_source
    from .catalog.randomize import EXCLUDED_NAMES, os_to_platform, pick_configurations

    if args.os:
        platform = os_to_platform(args.os)
    else:
        platform = args.platform or "linux"

    services = args.services
    vulns = args.vulns
    if args.difficulty is not None:
        if services is None:
            services = max(math.ceil(args.difficulty / 3), 1)
        if vulns is None:
            vulns = max(args.difficulty, 1)
    if services is None or vulns is None:
        raise NakonError(
            "non-interactive randomize needs --services and --vulns "
            "(or --difficulty to derive both)"
        )

    source = open_source(args.source)
    try:
        name_to_row = {}
        for row in source.all_rows():
            name = row["name"]
            if name in EXCLUDED_NAMES:
                continue
            name_to_row[name] = {
                "category": row["category"],
                "platform": row["platform"],
                "depends_on": row.get("depends_on") or [],
            }
        for name in args.exclude or []:
            name_to_row.pop(name, None)
    finally:
        source.close()

    services_picked, vulns_picked, services_in_use = pick_configurations(
        name_to_row, platform, services, vulns
    )

    if args.json:
        print(json.dumps({
            "platform": platform,
            "services": services_picked,
            "vulns": vulns_picked,
        }))
        return 0

    _log(f"[nakon] randomized selection for platform={platform}")
    _log(f"  services: {services_picked}")
    _log(f"  vulns/misconfigs: {vulns_picked}")
    _log(f"  distinct services after dependency resolution: {len(services_in_use)} "
         f"(budget {services})")
    return 0


def _wrap(text: str, width: int, indent: str) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width, initial_indent=indent,
                                   subsequent_indent=indent)) or ""


def cmd_catalog_list(args) -> int:
    _load_env()
    from .catalog.query import list_configurations, open_source

    source = open_source(args.source)
    try:
        rows = list_configurations(
            source,
            platform=args.platform,
            category=args.category,
            search=args.search,
            include_blocks=args.include_blocks,
        )
    finally:
        source.close()

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if not rows:
        print("[nakon] no configurations match those filters.")
        return 0

    for row in rows:
        attachments = row["attachments"]
        suffix = f"  [{len(attachments)} attachment(s)]" if attachments else ""
        print(f"{str(row['id']):>4}  {row['platform']:<8} {row['category']:<17} "
              f"{row['name']}{suffix}")
        if row["description"]:
            print(_wrap(row["description"], 96, " " * 8))
        else:
            print(f"{' ' * 8}(no description)")
    print(f"\n[nakon] {len(rows)} configuration(s)")
    return 0


def cmd_catalog_show(args) -> int:
    _load_env()
    from .catalog.query import describe, open_source

    source = open_source(args.source)
    try:
        entries = describe(source, args.names, args.platform)
    finally:
        source.close()

    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0

    for entry in entries:
        print(f"{entry['name']}  ({entry['category']}, {entry['platform']}, "
              f"type={entry['type']}, run_as={entry['run_as']})")
        print(_wrap(entry["description"] or "(no description)", 96, "    "))
        if entry["depends_on"]:
            print(f"    depends_on: {json.dumps(entry['depends_on'])}")
        if entry["attachments"]:
            names = ", ".join(a["original_name"] for a in entry["attachments"])
            print(f"    attachments: {names}")

        resolved = entry.get("resolved")
        if resolved is None:
            print(f"    does not resolve on {args.platform}: {entry.get('resolve_error')}")
        else:
            print(f"    resolves to {resolved['steps']} step(s) on {args.platform}")
            print(f"      configurations: {', '.join(resolved['configurations'])}")
            if resolved["packages"]:
                print(f"      packages:       {', '.join(resolved['packages'])}")
            if resolved["services"]:
                print(f"      services:       {', '.join(resolved['services'])}")
            if resolved["attachment_bytes"]:
                print(f"      media:          {resolved['attachment_bytes']} bytes")
        print()
    return 0


def cmd_catalog_check(args) -> int:
    _load_env()
    from .catalog.query import (boxes_from_config, boxes_from_pins, check_selection,
                            open_source, platforms_from_boxes_json)

    if args.config:
        boxes = boxes_from_config(args.config)
    elif args.box_vulns:
        platforms = None
        if args.boxes_json:
            platforms = platforms_from_boxes_json(args.boxes_json)
        boxes = boxes_from_pins(args.box_vulns, args.box_services, args.platform,
                                platforms=platforms)
    elif args.select:
        names = [n for chunk in args.select for n in chunk.split(",") if n.strip()]
        boxes = [{"name": "<selection>", "platform": args.platform, "configurations": names}]
    else:
        raise NakonError("give one of --select, --config or --box-vulns")

    source = open_source(args.source)
    try:
        report = check_selection(source, boxes)
    finally:
        source.close()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for box in report["boxes"]:
            print(f"{box['name']}  ({box['platform']}, {len(box['requested'])} selected)")
            for problem in box["problems"]:
                where = f" {problem['config']}:" if problem["config"] else ""
                print(f"  {problem['level']:<7}{where} {problem['message']}")
            resolved = box["resolved"]
            if resolved is not None:
                print(f"  ok      resolves to {resolved['steps']} step(s)"
                      + (f", {len(resolved['packages'])} package(s)" if resolved["packages"] else "")
                      + (f", {resolved['attachment_bytes']} bytes of media"
                         if resolved["attachment_bytes"] else ""))
            print()
        print(f"[nakon] {report['errors']} error(s), {report['warnings']} warning(s)")

    if report["errors"]:
        return 1
    if report["warnings"] and args.strict:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nakon",
        description="Build reproducible deployment bundles from the vulndb catalog, and apply them.",
        epilog="`build` needs the vulndb; `deploy` needs only paramiko and a bundle.",
    )
    parser.add_argument("--version", action="version", version=f"nakon {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="resolve the catalog into a content-addressed bundle")
    p_build.add_argument("--config", default="config.json", help="machine list (default: config.json)")
    p_build.add_argument("--out", default="bundles", help="bundle directory (default: ./bundles)")
    p_build.add_argument("--rebuild", action="store_true",
                         help="build even if a bundle matching the current catalog exists")
    p_build.add_argument("--export", metavar="TARBALL",
                         help="also write the whole bundle to this .tar.gz for archival")
    p_build.add_argument("--json", action="store_true",
                         help="print a JSON summary as the final line, for orchestrators")
    p_build.set_defaults(func=cmd_build)

    p_deploy = sub.add_parser("deploy", help="apply a built bundle to the machines in config.json")
    p_deploy.add_argument("--bundle", required=True, help="path to a bundle directory")
    p_deploy.add_argument("--config", default="config.json",
                          help="machine list, supplying addresses and credentials")
    p_deploy.add_argument("--jobs", type=int, default=1, help="machines to deploy in parallel")
    p_deploy.add_argument("--strict", action="store_true",
                          help="exit non-zero if any configuration failed (same as NAKON_STRICT=1)")
    p_deploy.add_argument("--keep-remote", action="store_true",
                          help="leave the unpacked plan on each target for debugging "
                               "(it lists every vulnerability planted on that box)")
    p_deploy.add_argument("--only", nargs="+", metavar="NAME",
                          help="deploy only these machines, by name or IP")
    p_deploy.add_argument("--no-logs", action="store_true",
                          help="don't save per-machine logs under the bundle's runs/ directory")
    p_deploy.add_argument("--json", action="store_true",
                          help="print per-step results as JSON on stdout, for orchestrators")
    p_deploy.set_defaults(func=cmd_deploy)

    p_diff = sub.add_parser("diff", help="compare a bundle against the live catalog")
    p_diff.add_argument("--bundle", required=True)
    p_diff.add_argument("--exit-code", action="store_true", help="exit 2 when drift is found")
    p_diff.add_argument("--json", action="store_true")
    p_diff.set_defaults(func=cmd_diff)

    p_show = sub.add_parser("show", help="describe a bundle (offline)")
    p_show.add_argument("--bundle", required=True)
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_rand = sub.add_parser(
        "randomize",
        help="pick services+vulns from the catalog (non-interactive), or build a config.json "
             "(no flags = interactive)",
    )
    p_rand.add_argument("--platform", choices=("linux", "windows"),
                        help="target platform (mutually convenient with --os)")
    p_rand.add_argument("--os", metavar="TEXT",
                        help="free-text os field (ubuntu24.04, windows2019, …) mapped to a platform")
    p_rand.add_argument("--services", type=int, metavar="N",
                        help="how many services to pick (a budget; fewer if the catalog is small)")
    p_rand.add_argument("--vulns", type=int, metavar="N",
                        help="how many vulns/misconfigurations to pick")
    p_rand.add_argument("--difficulty", type=int, metavar="N",
                        help="derive --services/--vulns from a 1-10 difficulty (ceil(d/3) and d) "
                             "when either is omitted")
    p_rand.add_argument("--exclude", nargs="+", metavar="NAME",
                        help="configuration names to leave out of the pool before picking")
    p_rand.add_argument("--json", action="store_true",
                        help="emit {platform, services, vulns} as JSON (for orchestrators)")
    p_rand.add_argument("--source", choices=("auto", "http", "mysql"), default="auto",
                        help="where to read the catalog (default: auto)")
    p_rand.set_defaults(func=cmd_randomize)

    # `catalog` is the read/verify half: what is available, and is a proposed selection sane.
    # It never writes and never builds, so it is safe to point an agent at.
    p_cat = sub.add_parser("catalog", help="browse the catalog and vet a selection")
    cat_sub = p_cat.add_subparsers(dest="catalog_command", required=True)

    def _add_source(p):
        p.add_argument("--source", choices=("auto", "http", "mysql"), default="auto",
                       help="where to read the catalog (default: auto — vulndb-ui over HTTP if "
                            "VULNDB_UI_URL is set, otherwise MySQL)")

    p_cat_list = cat_sub.add_parser("list", help="list configurations, with descriptions")
    p_cat_list.add_argument("--platform", choices=("linux", "windows", "other"))
    p_cat_list.add_argument("--category",
                            choices=("misconfiguration", "service", "vulnerability"))
    p_cat_list.add_argument("--search", metavar="TEXT",
                            help="substring match over name, description and script")
    p_cat_list.add_argument("--include-blocks", action="store_true",
                            help="also show reusable building blocks "
                                 "(install-package, create-user, enable-service)")
    p_cat_list.add_argument("--json", action="store_true")
    _add_source(p_cat_list)
    p_cat_list.set_defaults(func=cmd_catalog_list)

    p_cat_show = cat_sub.add_parser("show",
                                    help="one configuration in full, and what it pulls in")
    p_cat_show.add_argument("names", nargs="+", metavar="NAME")
    p_cat_show.add_argument("--platform", default="linux", choices=("linux", "windows"))
    p_cat_show.add_argument("--json", action="store_true")
    _add_source(p_cat_show)
    p_cat_show.set_defaults(func=cmd_catalog_show)

    p_cat_check = cat_sub.add_parser("check", help="vet a selection before it is built")
    p_cat_check.add_argument("--select", nargs="+", metavar="NAME",
                             help="configuration names, comma- or space-separated")
    p_cat_check.add_argument("--config", metavar="FILE",
                             help="check every machine in a nakon config.json")
    p_cat_check.add_argument("--box-vulns", metavar="FILE",
                             help="check a tezcatlipoca box_vulns.json")
    p_cat_check.add_argument("--boxes-json", metavar="FILE",
                             help="tezcatlipoca boxes.json: per-box platform is derived from each "
                                  "template name (contains 'win' = windows), overriding --platform")
    p_cat_check.add_argument("--box-services", metavar="FILE",
                             help="the matching box_services.json, checked alongside it")
    p_cat_check.add_argument("--platform", default="linux", choices=("linux", "windows"),
                             help="platform for --select and --box-vulns (default: linux)")
    p_cat_check.add_argument("--strict", action="store_true",
                             help="exit non-zero on warnings too, not just errors")
    p_cat_check.add_argument("--json", action="store_true")
    _add_source(p_cat_check)
    p_cat_check.set_defaults(func=cmd_catalog_check)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NakonError as exc:
        print(f"[nakon] error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[nakon] interrupted", file=sys.stderr)
        return 130
