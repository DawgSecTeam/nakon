"""Turn a resolved plan into flat scripts. No manifest is interpreted on the target."""

import re

# Filesystem-safe configuration names for step filenames.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

MARKER_BEGIN = "##nakon begin"
MARKER_RC = "##nakon rc"
MARKER_DONE = "##nakon done"


def safe_name(name: str) -> str:
    return _UNSAFE.sub("_", name) or "step"


def step_basename(index: int, step: dict, platform: str = "linux") -> str:
    """Step filename. The extension matters: `powershell -File` needs `.ps1`."""
    ext = ".ps1" if step["type"] == "powershell" else ".sh"
    return f"{index:03d}-{safe_name(step['name'])}{ext}"


_WHITESPACE = re.compile(r"\s+")


def step_label(step: dict) -> str:
    """Marker-facing name. Whitespace is collapsed so tabs/newlines can't break records."""
    raw = step["name"]
    return _WHITESPACE.sub(" ", str(raw)).strip() or "step"
