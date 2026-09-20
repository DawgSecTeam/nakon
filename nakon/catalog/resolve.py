"""Resolve a machine's requested configurations into one ordered list of steps.

Successor to the old `configurations.resolve()`. Two deliberate changes:

1. It returns a **single ordered list** with each step tagged `kind: "config" | "package"`,
   with package fallbacks emitted at their position in the dependency walk. The old code
   returned packages separately and deploy.py installed all of them up front, which meant a
   package could be installed long before — or long after — the configuration that needed it.

2. It validates platform/type compatibility. The old deploy.py picked an interpreter from the
   configuration's `type` at run time and would have handed a powershell script to bash on a
   Linux box without complaint. Resolution now happens at build time, so this is a hard error
   before anything is deployed.
"""

import re

from ..errors import CycleError, NakonError, PlatformMismatchError, UnknownConfigurationError
from ..hashing import normalize_request

# A var name has to be a valid identifier in both shells: bash sources `KEY=value` lines under
# `set -a`, PowerShell gets `$KEY = 'value'` prepended to the step. Anything else either fails
# to parse on the box or — on bash, where `. vars.env` is executed as root — runs as code.
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
    """Reject a configuration that can't run on this machine's platform."""
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

    # 'other' is the catalog's escape hatch for platform-agnostic rows; don't second-guess it.
    row_platform = (row.get("platform") or "").lower()
    if row_platform not in ("", "other", platform):
        raise PlatformMismatchError(
            f"configuration '{row['name']}' is platform={row_platform!r} but was requested "
            f"for a {platform} machine. Reached via: {' -> '.join(requested_by)}"
        )


def resolve(source, requested: list, platform: str = "linux") -> list:
    """Walk the depends_on graph and return the ordered step list for one machine.

    Steps are dependency-first and deduped by (name, vars) — the same configuration requested
    twice with identical vars runs once; requested with different vars runs once per distinct
    set, which is how `create-user` gets used for several usernames.

    A bare re-request (empty vars) of an already-emitted configuration is satisfied by that
    earlier emission instead of emitting a second, vars-less copy. This is the depends_on
    shape: "Elevate User Account" pulls in "Add User Account" with no vars, so a plan that
    also requests "Add User Account" directly (with its username/password/...) must not get
    a duplicate Add step with every parameter null. Requests that carry vars of their own
    still emit per distinct set — only the empty-vars form dedupes against history.

    Returns a list of dicts, each either:
      {"kind": "package", "package": str}
      {"kind": "config", "name", "config_id", "script", "run_as", "type", "vars",
       "attachments": [...]}
    """
    ordered = []
    visited = set()
    visiting = set()
    emitted = set()  # config names already in `ordered` (packages are deduped by `visited`)

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
            # No configuration by this name — treat it as a raw package for the remote host's
            # package manager. Mark it visited so a package pulled in twice is installed once.
            visited.add(key)
            if var_values:
                raise UnknownConfigurationError(
                    f"'{name}' was given vars {sorted(var_values)} but matches no "
                    f"configuration, so it can only be a raw package name — and packages "
                    f"take no vars. Reached via: {' -> '.join(path + [name])}"
                )
            ordered.append({"kind": "package", "package": name})
            return

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
            # Raw depends_on, for provenance. Not part of any hashed document.
            "depends_on": row.get("depends_on") or [],
        })

    for item in normalize_request(requested):
        visit(item["name"], item["vars"], [])

    return ordered
