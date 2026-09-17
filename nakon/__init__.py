"""nakon — builds reproducible deployment bundles from the vulndb catalog and applies them.

Two halves that deliberately run in different places:

  nakon build   — wherever the vulndb is reachable. Resolves every machine's configuration
                  graph, downloads attachment media, and writes a self-contained,
                  content-addressed bundle. Needs mysql-connector + requests.

  nakon deploy  — wherever the target boxes are reachable. Pushes a bundle and runs it.
                  Needs paramiko and nothing else: no database, no vulndb-ui, no .env.

That split is the point. A saved bundle deploys byte-identical scripts and media however
much the catalog has moved on since, and the deploy host never holds vulndb credentials.
"""

__version__ = "0.1.5"

# Public API for callers embedding nakon rather than shelling out to it.
#
# Resolved lazily (PEP 562) rather than imported at module scope, because the import discipline
# in cli.py applies here too: `nakon deploy` runs on the scoring engine with only paramiko, and
# `import nakon` must not drag in the build half. `from nakon import build` pulls in the builder
# only at the moment it is asked for.
__all__ = ["build", "deploy", "summarize", "Bundle", "load_machines", "__version__"]


def __getattr__(name):
    if name == "build":
        from .build.builder import build

        return build
    if name == "load_machines":
        from .build.builder import load_machines

        return load_machines
    if name in ("deploy", "summarize"):
        from .deploy import runner

        return getattr(runner, name)
    if name == "Bundle":
        from .deploy.bundle import Bundle

        return Bundle
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
