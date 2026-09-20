"""Generators that turn a resolved plan into flat, straight-line scripts.

The generated driver never interprets a manifest on the target. Python already knows the
resolved order, the vars, the run_as and the interpreter at build time, so it emits plain
bash/powershell. That keeps one implementation of the semantics (this one), needs no jq or
python3 on the box, and leaves an artifact an operator can read at 3am during a competition.
"""

import re

# Filesystem-safe form of a configuration name, for step filenames.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Marker protocol emitted on stdout and parsed by nakon.deploy.report. Kept deliberately
# greppable so a human tailing the log sees the same structure the parser does.
MARKER_BEGIN = "##nakon begin"
MARKER_RC = "##nakon rc"
MARKER_DONE = "##nakon done"


def safe_name(name: str) -> str:
    return _UNSAFE.sub("_", name) or "step"


def step_basename(index: int, step: dict, platform: str = "linux") -> str:
    """Filename for a step's script inside `steps/`.

    Package fallbacks get a generated installer script of their own so that every step —
    config or package — is a separate file run as a separate process. That uniformity is
    what keeps the driver simple and step failures isolated.

    The extension is load-bearing on Windows: `powershell.exe -File` refuses anything that
    isn't `.ps1`.
    """
    if step["kind"] == "package":
        ext = ".ps1" if platform == "windows" else ".sh"
        return f"{index:03d}-pkg-{safe_name(step['package'])}{ext}"
    ext = ".ps1" if step["type"] == "powershell" else ".sh"
    return f"{index:03d}-{safe_name(step['name'])}{ext}"


_WHITESPACE = re.compile(r"\s+")


def step_label(step: dict) -> str:
    """Human/marker-facing name for a step.

    Whitespace runs collapse to one space: the label travels on the `##nakon begin` line and in
    tab-delimited report.tsv / `##nakon rc` records, so a tab in a catalog name would shift the
    rc column and a newline would split the record. Names have no charset restriction in vulndb.
    """
    raw = step["package"] if step["kind"] == "package" else step["name"]
    return _WHITESPACE.sub(" ", str(raw)).strip() or "step"
