"""Read the catalog, and vet a proposed selection against it."""

import json
from pathlib import Path
from typing import Optional

from ..errors import NakonError
from .randomize import EXCLUDED_NAMES
from .resolve import _TYPES_FOR_PLATFORM, resolve

# Severity levels a problem can carry. Errors mean the selection will not deploy as intended;
# warnings mean it will deploy but probably isn't what anyone wanted.
ERROR = "error"
WARNING = "warning"


def open_source(kind: str = "auto"):
    """'auto' prefers HTTP, falling back to MySQL when VULNDB_UI_URL isn't set."""
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
    return {row["name"]: row for row in source.all_rows()}


def list_configurations(source, platform=None, category=None, search=None,
                        include_blocks=False) -> list:
    """Every configuration, filtered. Records include the script."""
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
        "required_vars": row.get("required_vars") or [],
        "attachments": [
            {"original_name": a.get("original_name"), "size_bytes": a.get("size_bytes")}
            for a in (row.get("attachments") or [])
        ],
    }


def _resolution_summary(steps: list, index: dict) -> dict:
    configs = [s["name"] for s in steps if s["kind"] == "config"]
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
    """Same rules as resolve._check_platform, collected instead of raised."""
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
    """Vet boxes shaped {"name", "platform", "configurations"}."""
    index = _index(source)
    report = {"ok": True, "errors": 0, "warnings": 0, "boxes": []}

    for box in boxes:
        platform = box.get("platform") or "linux"
        requested = box.get("configurations") or []
        problems = []

        def add(level, code, message, config=None, **extra):
            problem = {"level": level, "code": code, "config": config, "message": message}
            problem.update(extra)
            problems.append(problem)

        names = [item if isinstance(item, str) else item.get("name") for item in requested]

        seen = set()
        for name in names:
            if name in seen:
                add(WARNING, "duplicate", "selected more than once", name)
            seen.add(name)

        for item in requested:
            if isinstance(item, str):
                name, provided = item, {}
            else:
                name, provided = item.get("name"), item.get("vars") or {}

            if name in EXCLUDED_NAMES:
                add(ERROR, "building-block",
                    "is a building block; requesting it directly plants a no-op",
                    name)
                continue

            row = index.get(name)
            if row is None:
                add(ERROR, "unknown-name",
                    "matches no configuration in the catalog",
                    name)
                continue

            for level, code, message in _platform_problems(row, platform):
                add(level, code, message, name)

            # Unsupplied vars abort the plant (`${VAR:?}` exits rc=2). The catalog declares
            # them per row in `required_vars` (curated via `vulndb-cli update
            # --required-vars`); nakon only reads what the row says.
            required = row.get("required_vars") or []
            missing = [var for var in required if var not in provided]
            if missing:
                fix = {"name": name, "vars": {var: "<value>" for var in missing}}
                where = ("a bare name supplies no vars" if isinstance(item, str)
                         else "the pinned \"vars\" object does not cover them")
                add(ERROR, "missing-vars",
                    f"requires var(s) {', '.join(missing)} ({where}), so the plant would fail "
                    f"mid-deploy. Pin it as {json.dumps(fix)}, or drop it.",
                    name, missing=missing, required_vars=required)

            if not (row.get("script") or "").strip() and not (row.get("depends_on") or []):
                add(WARNING, "no-op",
                    "has an empty script and no dependencies", name)

            if not (row.get("description") or "").strip():
                add(WARNING, "no-description",
                    "has no description", name)

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
    from ..machines import load_machines

    return [
        {"name": m["name"], "platform": m["platform"], "configurations": m["configurations"]}
        for m in load_machines(Path(config_path))
    ]


def boxes_from_pin_files(vulns_path, services_path=None, platform="linux",
                    platforms: Optional[dict] = None) -> list:
    """Pin files as check_selection boxes: JSON objects mapping box name to
    a list of configuration names; an explicit {box: platform} map overrides `platform`."""
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


def load_platforms(path) -> dict:
    """{box_name: platform} from a JSON object file."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError as exc:
        raise NakonError(f"no such file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise NakonError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise NakonError(f"{path} must be a JSON object mapping box name to platform")
    return {str(name): str(platform) for name, platform in data.items()}
