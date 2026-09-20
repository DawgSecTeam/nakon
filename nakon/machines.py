"""config.json — the machine list — and the one derivation every half of nakon needs from it.

Stdlib only, on purpose. `nakon deploy` reads config.json on the scoring engine (paramiko and
nothing else installed), and `nakon build` / `nakon catalog check` read it on the build host.
Keeping this in its own module is what lets the deploy half honour "never imports build/ or
catalog/": before, `cli.cmd_deploy` pulled `load_machines` out of `build/builder.py`, which
dragged the whole build package (fetch, tarball, gen, catalog.randomize) onto the deploy host.
"""

import json
from pathlib import Path

from .errors import BundleError


def os_to_platform(os_name) -> str:
    """Map a free-text `os` field (ubuntu24.04, debian12, windows2019, ...) to a platform."""
    return "windows" if "win" in str(os_name or "").lower() else "linux"


def _validate_configurations(label: str, requested) -> list:
    """Every entry must be a name or {"name": ..., "vars": {...}} — anything else would only
    surface later as a bare KeyError inside hashing.normalize_request or paramiko."""
    if not isinstance(requested, list):
        raise BundleError(f"{label}: 'configurations' must be a list, got {type(requested).__name__}")
    for position, entry in enumerate(requested):
        if isinstance(entry, str):
            if not entry.strip():
                raise BundleError(f"{label}: configurations[{position}] is an empty name")
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise BundleError(
                f"{label}: configurations[{position}] must be a name or "
                f"{{\"name\": ..., \"vars\": {{...}}}}, got {entry!r}"
            )
        vars_ = entry.get("vars")
        if vars_ is not None and not isinstance(vars_, dict):
            raise BundleError(
                f"{label}: configurations[{position}] ('{entry['name']}') has vars that are not "
                f"an object: {vars_!r}"
            )
    return requested


def load_machines(config_path, warn=None) -> list:
    """Read config.json and normalize the bits nakon cares about.

    Raises BundleError with the machine and field named for anything malformed; `warn`, if
    given, is called for things that are legal but almost certainly not intended (a machine
    with nothing to deploy).
    """
    config_path = Path(config_path)
    try:
        data = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise BundleError(f"no config file at {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise BundleError(f"{config_path} is not valid JSON: {exc}") from exc

    machines = data.get("machines") if isinstance(data, dict) else None
    if not machines or not isinstance(machines, list):
        raise BundleError(f"{config_path} has no 'machines' list")

    normalized = []
    for position, machine in enumerate(machines):
        if not isinstance(machine, dict):
            raise BundleError(f"{config_path}: machines[{position}] is not an object: {machine!r}")
        name = machine.get("name") or machine.get("ip") or f"machines[{position}]"
        label = f"{config_path}: machine '{name}'"

        ip = machine.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            raise BundleError(f"{label} has no 'ip'")
        user = machine.get("user")
        if not isinstance(user, str) or not user.strip():
            raise BundleError(f"{label} has no 'user'")

        os_name = machine.get("os")
        if os_name is None:
            os_name = "linux"
        elif not isinstance(os_name, str):
            raise BundleError(f"{label}: 'os' must be a string, got {os_name!r}")

        requested = _validate_configurations(label, machine.get("configurations") or [])
        if not requested and warn is not None:
            warn(f"machine '{name}' has an empty 'configurations' list — nothing will be planted on it")

        identity_file = machine.get("identity_file")
        if identity_file is not None and not isinstance(identity_file, str):
            raise BundleError(f"{label}: 'identity_file' must be a path string, got {identity_file!r}")

        normalized.append({
            "name": machine.get("name") or ip,
            "ip": ip,
            "os": os_name,
            "user": user,
            "password": machine.get("password"),
            # Optional: an SSH private key path. Carried through so deploy/ssh.connect can use
            # it; it used to be read there but never survived this normalization.
            "identity_file": identity_file,
            "platform": os_to_platform(os_name),
            "configurations": requested,
        })
    return normalized
