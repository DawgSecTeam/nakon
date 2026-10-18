"""Resolve requested configurations into an ordered step list."""

import re

from ..errors import CycleError, NakonError, PlatformMismatchError, UnknownConfigurationError
from ..hashing import normalize_request

# Var names must be valid in both shells; on bash `. vars.env` executes as code.
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_var_names(name, var_values, path):
    bad = sorted(k for k in var_values if not _VAR_NAME.match(str(k)))
    if bad:
        raise NakonError(
            f"configuration '{name}' was given var name(s) {bad} that are not valid shell "
            f"identifiers ([A-Za-z_][A-Za-z0-9_]*). Reached via: {' -> '.join(path + [name])}"
        )

# Which script types each platform can actually execute.
_TYPES_FOR_PLATFORM = {
    "linux": {"bash", "command"},
    "windows": {"powershell"},
}


def _check_platform(row, platform, requested_by):
    allowed = _TYPES_FOR_PLATFORM.get(platform)
    if allowed is None:
        raise PlatformMismatchError(
            f"unknown platform {platform!r} (expected 'linux' or 'windows')"
        )

    cfg_type = (row.get("type") or "bash").lower()
    if cfg_type not in allowed:
        raise PlatformMismatchError(
            f"configuration '{row['name']}' has type={cfg_type!r}, which cannot run on a "
            f"{platform} machine (allowed: {', '.join(sorted(allowed))}). "
            f"Reached via: {' -> '.join(requested_by)}"
        )

    # 'other' matches any platform.
    row_platform = (row.get("platform") or "").lower()
    if row_platform not in ("", "other", platform):
        raise PlatformMismatchError(
            f"configuration '{row['name']}' is platform={row_platform!r} but was requested "
            f"for a {platform} machine. Reached via: {' -> '.join(requested_by)}"
        )


def resolve(source, requested: list, platform: str = "linux") -> list:
    """Walk depends_on dependency-first; dedupe by (name, vars)."""
    ordered = []
    visited = set()
    visiting = set()
    emitted = set()  # config names already in `ordered`

    def visit(name, var_values, path):
        _check_var_names(name, var_values, path)
        key = (name, tuple(sorted(var_values.items())))
        if key in visited:
            return
        if not var_values and name in emitted:
            return
        if key in visiting:
            raise CycleError(
                f"circular dependency involving '{name}': {' -> '.join(path + [name])}"
            )

        row = source.fetch(name)
        if row is None:
            raise UnknownConfigurationError(
                f"'{name}' matches no configuration in the catalog. "
                f"Reached via: {' -> '.join(path + [name])}"
            )

        _check_platform(row, platform, path + [name])

        visiting.add(key)
        for dep in normalize_request(row["depends_on"]):
            visit(dep["name"], dep["vars"], path + [name])
        visiting.discard(key)

        visited.add(key)
        emitted.add(name)
        ordered.append({
            "kind": "config",
            "name": row["name"],
            "config_id": row["id"],
            "script": row["script"],
            "run_as": row["run_as"] or "root",
            "type": (row.get("type") or "bash").lower(),
            "vars": var_values,
            "attachments": row["attachments"],
            "depends_on": row.get("depends_on") or [],
        })

    for item in normalize_request(requested):
        visit(item["name"], item["vars"], [])

    return ordered
