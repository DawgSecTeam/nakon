"""Build reproducible bundles from the vulndb catalog, and apply them.

`nakon build` needs the vulndb; `nakon deploy` needs only paramiko.
A saved bundle deploys byte-identical content however much the catalog moves on.
"""

__version__ = "0.1.8"
