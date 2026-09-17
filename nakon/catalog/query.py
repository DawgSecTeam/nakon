"""Read the catalog, and vet a proposed selection against it.

This is the surface an agent uses to choose the misconfigurations for a competition. The split
is deliberate: nakon supplies context and verification, the caller supplies judgement. Nothing
here calls a model, ranks configurations, or decides anything — it answers "what is available?"
and "is what you picked coherent?", and leaves the choosing to whoever asked.

`check` is the part that earns its keep. Several ways of getting a selection wrong currently fail
silently or late:

  * a mistyped name is not an error anywhere — resolve() treats an unknown name as a raw package
    (see resolve.py), so `suid-fnd` becomes `apt-get install suid-fnd` and the box quietly ends up
    with no misconfiguration at all;
  * install-package / create-user / enable-service are building blocks meant to be pulled in
    through depends_on, and requesting one directly plants a no-op;
  * a windows configuration on a linux box only fails once the build reaches it;
  * a few catalog rows have an empty script and no dependencies, so they build into a step that
    does nothing.

All of these are cheap to detect before a bundle is ever built, which is what this module does.
"""

import json
from pathlib import Path

from ..errors import NakonError
from .randomize import EXCLUDED_NAMES
from .resolve import _TYPES_FOR_PLATFORM, resolve

# Severity levels a problem can carry. Errors mean the selection will not deploy as intended;
# warnings mean it will deploy but probably isn't what anyone wanted.
ERROR = "error"
WARNING = "warning"


def open_source(kind: str = "auto"):
    """Open a catalog source. 'http' needs VULNDB_UI_URL; 'mysql' needs the .env credentials.

    'auto' prefers HTTP, because that is the path that works for an agent with no database
    credentials, and falls back to MySQL when VULNDB_UI_URL isn't set.
    """
    import os

    from .source import HttpCatalog, MySQLCatalog

    if kind == "http":
        return HttpCatalog.from_env()
    if kind == "mysql":
        return MySQLCatalog.from_env()
    if kind != "auto":
        raise NakonError(f"unknown catalog source {kind!r} (expected auto, http or mysql)")

    if os.getenv("VULNDB_UI_URL", "").strip():
        return HttpCatalog.from_env()
    return MySQLCatalog.from_env()


def _index(source) -> dict:
    """{name: row} for the whole catalog, in one round trip."""
    return {row["name"]: row for row in source.all_rows()}


def list_configurations(source, platform=None, category=None, search=None,
                        include_blocks=False) -> list:
    """Every configuration, filtered. Records are returned whole — script included.

    An agent choosing a set needs to read the script; the description says what a config is for,
    but the script is the only thing that says exactly what it does.
    """
    rows = source.all_rows()
    needle = (search or "").lower().strip()

    result = []
    for row in rows:
        if not include_blocks and row["name"] in EXCLUDED_NAMES:
            continue
        if platform and row["platform"] not in (platform, "other"):
            continue
        if category and row["category"] != category:
            continue
        if needle:
            haystack = f"{row['name']} {row.get('description') or ''} {row['script']}".lower()
            if needle not in haystack:
                continue
        result.append(_public_row(row))
    return result


def _public_row(row: dict) -> dict:
    """A catalog row as a caller should see it: no object_key, attachments named not keyed."""
    return {
        "id": row.get("id"),
        "name": row["name"],
        "description": row.get("description"),
        "platform": row["platform"],
        "category": row["category"],
        "type": row.get("type") or "bash",
        "run_as": row.get("run_as") or "root",
        "depends_on": row.get("depends_on") or [],
        "script": row.get("script") or "",
        "attachments": [
            {"original_name": a.get("original_name"), "size_bytes": a.get("size_bytes")}
            for a in (row.get("attachments") or [])
        ],
    }


def _resolution_summary(steps: list, index: dict) -> dict:
    """What a resolved step list actually amounts to, for a caller deciding if it's sane."""
    configs = [s["name"] for s in steps if s["kind"] == "config"]
    packages = [s["package"] for s in steps if s["kind"] == "package"]
    services = [n for n in configs if (index.get(n) or {}).get("category") == "service"]

    seen_attachments = {}
    for step in steps:
        for attachment in step.get("attachments") or []:
            seen_attachments[attachment.get("id") or attachment.get("original_name")] = (
                attachment.get("size_bytes") or 0
            )

    return {
        "steps": len(steps),
        "configurations": configs,
        "packages": packages,
        "services": services,
        "attachment_bytes": sum(seen_attachments.values()),
    }


def describe(source, names: list, platform: str = "linux") -> list:
    """Full record for each name, plus what requesting it would actually pull in."""
    index = _index(source)
    out = []
    for name in names:
        row = index.get(name)
        if row is None:
            raise NakonError(
                f"no configuration named {name!r}. `nakon catalog list` shows what there is."
            )
        entry = _public_row(row)
        try:
            steps = resolve(source, [name], platform)
            entry["resolved"] = _resolution_summary(steps, index)
        except NakonError as exc:
            entry["resolved"] = None
            entry["resolve_error"] = str(exc)
        out.append(entry)
    return out


def _platform_problems(row: dict, platform: str) -> list:
    """Same rules as resolve._check_platform, but collected instead of raised.

    check() reports everything wrong with a selection in one pass; raising on the first problem
    would make an agent fix them one round trip at a time.
    """
    problems = []
    allowed = _TYPES_FOR_PLATFORM.get(platform)
    if allowed is None:
        return [(ERROR, "unknown-platform",
                 f"unknown platform {platform!r} (expected 'linux' or 'windows')")]

    cfg_type = (row.get("type") or "bash").lower()
    if cfg_type not in allowed:
        problems.append((
            ERROR, "type-mismatch",
            f"type={cfg_type!r} cannot run on a {platform} machine "
            f"(allowed: {', '.join(sorted(allowed))})",
        ))

    row_platform = (row.get("platform") or "").lower()
    if row_platform not in ("", "other", platform):
        problems.append((
            ERROR, "platform-mismatch",
            f"is platform={row_platform!r} but was selected for a {platform} machine",
        ))
    return problems


def check_selection(source, boxes: list) -> dict:
    """Vet one or more boxes' selections.

    `boxes` is a list of {"name": str, "platform": str, "configurations": [...]}, which is the
    shape both config.json machines and box_vulns.json entries reduce to.
    """
    index = _index(source)
    report = {"ok": True, "errors": 0, "warnings": 0, "boxes": []}

    for box in boxes:
        platform = box.get("platform") or "linux"
        requested = box.get("configurations") or []
        problems = []

        def add(level, code, message, config=None):
            problems.append({"level": level, "code": code, "config": config, "message": message})

        names = [item if isinstance(item, str) else item.get("name") for item in requested]

        seen = set()
        for name in names:
            if name in seen:
                add(WARNING, "duplicate", "selected more than once", name)
            seen.add(name)

        for name in names:
            if name in EXCLUDED_NAMES:
                add(ERROR, "building-block",
                    "is a reusable building block meant to be pulled in through another "
                    "configuration's depends_on; requesting it directly plants a no-op",
                    name)
                continue

            row = index.get(name)
            if row is None:
                add(ERROR, "unknown-name",
                    "matches no configuration, so it would be treated as a raw package name "
                    "and silently installed with apt/dnf instead of planting anything",
                    name)
                continue

            for level, code, message in _platform_problems(row, platform):
                add(level, code, message, name)

            if not (row.get("script") or "").strip() and not (row.get("depends_on") or []):
                add(WARNING, "no-op",
                    "has an empty script and no dependencies, so it builds into a step that "
                    "does nothing", name)

            if not (row.get("description") or "").strip():
                add(WARNING, "no-description",
                    "has no description, so anything choosing a set has only the slug and the "
                    "script to go on", name)

        summary = None
        if not any(p["level"] == ERROR for p in problems):
            try:
                steps = resolve(source, requested, platform)
                summary = _resolution_summary(steps, index)
                pulled_in = [s for s in summary["services"] if s not in names]
                if pulled_in:
                    add(WARNING, "implicit-services",
                        f"dependencies pull in service(s) that were not selected: "
                        f"{', '.join(pulled_in)}")
            except NakonError as exc:
                add(ERROR, "unresolvable", str(exc))

        errors = sum(1 for p in problems if p["level"] == ERROR)
        warnings = sum(1 for p in problems if p["level"] == WARNING)
        report["errors"] += errors
        report["warnings"] += warnings
        if errors:
            report["ok"] = False

        report["boxes"].append({
            "name": box.get("name") or "<unnamed>",
            "platform": platform,
            "requested": names,
            "problems": problems,
            "resolved": summary,
        })

    return report


def boxes_from_config(config_path) -> list:
    """Machines in a nakon config.json, as check_selection boxes."""
    from ..build.builder import load_machines

    return [
        {"name": m["name"], "platform": m["platform"], "configurations": m["configurations"]}
        for m in load_machines(Path(config_path))
    ]


def boxes_from_pins(vulns_path, services_path=None, platform="linux",
                    platforms: dict | None = None) -> list:
    """tezcatlipoca's box_vulns.json (+ optional box_services.json), as check_selection boxes.

    Both files are {box_name: [configuration_name, ...]} keyed by box *type*, not per team.
    Services are checked alongside vulns because the two are concatenated into one request when
    the competition is generated, and a conflict only shows up in the combined list.

    `platforms` optionally maps box name -> "linux"|"windows", overriding `platform` per box
    (mixed-platform competitions otherwise need one check run per platform).
    """
    def _read(path):
        if path is None:
            return {}
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError as exc:
            raise NakonError(f"no such file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise NakonError(f"{path} is not valid JSON: {exc}") from exc

    vulns = _read(vulns_path)
    services = _read(services_path)
    platforms = platforms or {}

    boxes = []
    for name in sorted(set(vulns) | set(services)):
        boxes.append({
            "name": name,
            "platform": platforms.get(name, platform),
            "configurations": list(services.get(name, [])) + list(vulns.get(name, [])),
        })
    return boxes


def platforms_from_boxes_json(boxes_path) -> dict:
    """{box_name: platform} from tezcatlipoca's boxes.json, by template name.

    Mirrors tezcatlipoca's own rule (is_windows_template): a template whose name contains
    "win" (case-insensitive) is a Windows box; everything else is linux.
    """
    try:
        boxes = json.loads(Path(boxes_path).read_text())
    except FileNotFoundError as exc:
        raise NakonError(f"no such file: {boxes_path}") from exc
    except json.JSONDecodeError as exc:
        raise NakonError(f"{boxes_path} is not valid JSON: {exc}") from exc
    return {
        b["name"]: ("windows" if "win" in str(b.get("template", "")).lower() else "linux")
        for b in boxes
    }
