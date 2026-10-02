"""Which variables a configuration's script needs, and whether a request supplies them.

The catalog carries **no required-vars metadata** — no column, and vulndb-ui's HTTP API
exposes none — so a configuration's required vars are *derived from its script body*: the
uppercase names the script reads but neither assigns, guards, self-defaults, nor receives
through a `depends_on` `vars` object.

The derivation deliberately mirrors the bundle lint that tezcatlipoca runs at deploy time
(`nakon_ops._lint_bundle_vars`): strip full-line comments and single-quoted spans, drop
`${VAR:-default}`-style self-defaults, treat `[ -z "$VAR" ]` / `[ -n "$VAR" ]` guards and
`VAR=` assignments as providing the name, and ignore a small list of shell builtins. The two
scanners must agree — whatever that lint would reject, this must report, or `randomize` and
`catalog check` would bless a selection the plant then fails. Keep them in sync (the driver
side is the copy that runs on the box; this is the copy the catalog commands can run without
a database).

A row *may* also carry a `required_vars` (or `requires`) key — a list of names, or a mapping
`{"VAR": "literal" | "ip" | "ip:<box>"}`. vulndb's schema has no such column today, so this is
forward-compatibility only: when it appears it is **unioned** with the derived set, never
substituted for it, because the derived set is what the deploy lint actually enforces.

Only `bash`/`command` payloads are scanned. PowerShell reads `$env:FOO`/`$FOO` under different
rules and tezcatlipoca's lint skips windows payloads entirely, so deriving there would invent
exclusions nothing enforces.
"""

import re

# Shell/env names a payload may reference without any step declaring them. Identical to
# tezcatlipoca's `_SHELL_VAR_WHITELIST` (nakon_ops.py) for the same reason: the two scanners
# must classify the same script the same way.
_SHELL_VAR_WHITELIST = {
    "PATH", "HOME", "USER", "PWD", "OLDPWD", "SHELL", "TERM", "LANG", "SHLVL",
    "UID", "EUID", "PPID", "IFS", "PS1", "PS4", "HOSTNAME", "RANDOM", "SECONDS",
    "LINENO", "TMPDIR", "MAIL", "OPTARG", "OPTIND", "DEBIAN_FRONTEND",
    "SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND", "LOGNAME",
}

# `${VAR:-x}` `${VAR-x}` `${VAR:=x}` `${VAR= x}` `${VAR:+x}` `${VAR+x}` all let the script
# supply the value itself, so the var is optional. `${VAR:?x}` is NOT in this set: the script
# aborts when it is unset, which is exactly "required".
_SELF_DEFAULT_RE = re.compile(r"\$\{[A-Z_][A-Z0-9_]*:?[-+=][^}]*\}")
_VAR_REF_RE = re.compile(r"\$(?:\{([A-Z_][A-Z0-9_]*)|([A-Z_][A-Z0-9_]*))")

# `[ -z "$VAR" ]` / `[ -n "${VAR}" ]`: the script tests the var itself, so it is optional.
_GUARD_RE = re.compile(r"\[\s+-[zn]\s+\"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

# `VAR=...`, `export VAR=...`, `local VAR=...`, `declare -x VAR=...`: the script provides it.
_ASSIGN_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+|readonly\s+|local\s+|declare\s+-?\w*\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)="
)

_SHELL_TYPES = ("bash", "command")


def _scan(text: str) -> set:
    """Uppercase names `text` reads that it does not provide itself."""
    # Full-line shell comments never execute. (Prose mentioning a var must not count.)
    text = re.sub(r"(?m)^[ \t]*#.*$", "", text)
    # Single-quoted spans never expand ('$TTL 604800' is a DNS directive, not a var). Must not
    # cross a newline: an apostrophe in a comment would otherwise swallow real code.
    text = re.sub(r"'[^'\n]*'", "''", text)

    # Guards first: stripping self-defaults first would delete the `${VAR-}` inside the guard
    # and lose the var it names.
    guarded = {m.group(1) for m in _GUARD_RE.finditer(text)}
    text = _SELF_DEFAULT_RE.sub("", text)
    assigned = {m.group(1) for m in _ASSIGN_RE.finditer(text)}

    refs = set()
    for match in _VAR_REF_RE.finditer(text):
        name = match.group(1) or match.group(2)
        if name.startswith("_"):  # bash specials and $_GET-style globals in heredocs
            continue
        if name in _SHELL_VAR_WHITELIST or name in assigned or name in guarded:
            continue
        refs.add(name)
    return refs


def derive_required_vars(script: str) -> list:
    """Var names a bash/command `script` reads but nothing supplies, sorted.

    Empty for an empty script. Callers use this to decide whether a configuration can be
    planted as a bare name (no `vars` object) at all.
    """
    if not script:
        return []
    return sorted(_scan(script))


def _declared(row: dict) -> set:
    """Names from an optional catalog-side `required_vars`/`requires` field, if present."""
    raw = row.get("required_vars")
    if raw is None:
        raw = row.get("requires")
    if not raw:
        return set()
    if isinstance(raw, dict):
        return {str(name) for name in raw}
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(name) for name in raw}
    return {str(raw)}


def required_vars_for_row(row: dict) -> list:
    """Every var a configuration's step would read undeclared, sorted.

    Declared metadata (if a catalog ever provides it) is unioned with the script-derived set.
    Non-shell payloads (powershell) return only the declared set: the driver's lint does not
    cover them, so deriving is neither possible nor enforced there.
    """
    declared = _declared(row)
    if (row.get("type") or "bash").lower() not in _SHELL_TYPES:
        return sorted(declared)
    return sorted(declared | set(derive_required_vars(row.get("script") or "")))


def unsatisfied_required_vars(row: dict, supplied=None) -> list:
    """Required vars of `row` not present in `supplied` (a vars mapping). Sorted."""
    supplied = supplied or {}
    return sorted(name for name in required_vars_for_row(row) if name not in supplied)
