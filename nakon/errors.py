"""Exception types shared by the build and deploy halves of nakon."""


class NakonError(Exception):
    """Base for every error nakon raises deliberately (as opposed to a bug)."""


class CycleError(NakonError):
    """A configuration's depends_on graph contains a cycle."""


class UnknownConfigurationError(NakonError):
    """A requested name matched no configuration in the catalog."""


class PlatformMismatchError(NakonError):
    """A configuration's script `type` can't run on the machine's platform."""


class BundleError(NakonError):
    """A bundle is missing, malformed, or doesn't cover what config.json asks for."""


class CatalogError(NakonError):
    """The vulndb catalog (MySQL) or attachment store (vulndb-ui/MinIO) failed."""
