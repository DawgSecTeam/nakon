"""Catalog sources — where configuration rows come from.

`MySQLCatalog` is the real vulndb; `DictCatalog` backs the hermetic tests. Everything that
resolves or builds takes a source rather than a cursor, so the whole build path is testable
without a database.

IMPORTANT: `mysql.connector` is imported *inside* the functions that need it, never at module
scope. `nakon deploy` runs on the scoring engine where only paramiko is installed, and it
imports this module transitively; a top-level import would kill it there.
"""

import json
import os

from ..errors import CatalogError

# Column order shared by every configuration query, so row unpacking stays in one place.
#
# `description` rides along for `nakon catalog`, which is the only thing that reads it. It is
# deliberately absent from everything the build hashes: source_fingerprint() and plan_document()
# in build/builder.py assemble their documents from an explicit key list, so adding a column here
# cannot move a bundle id. Keep it that way — prose must never be able to invalidate a bundle.
_CONFIG_COLUMNS = "id, name, description, platform, category, script, run_as, type, depends_on"

# The same query against a catalog that predates the description column. Applying the migration
# (vulndb-ui/migrations/001-add-description.sql) is a separate manual step against a database the
# whole team shares, so the code has to work either side of it rather than hard-failing on a
# `Unknown column 'description'` for anyone who pulls before it is run.
_CONFIG_COLUMNS_LEGACY = "id, name, platform, category, script, run_as, type, depends_on"


def _parse_depends_on(raw):
    """depends_on is a JSON column; the connector may hand back a list, a str, or None."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return json.loads(raw)


def _row_to_dict(row, attachments):
    if len(row) == 8:  # pre-migration catalog: no description column
        id_, name, platform, category, script, run_as, cfg_type, depends_on = row
        description = None
    else:
        id_, name, description, platform, category, script, run_as, cfg_type, depends_on = row
    return {
        "id": id_,
        "name": name,
        "description": description,
        "platform": platform,
        "category": category,
        "script": script,
        "run_as": run_as,
        "type": cfg_type,
        "depends_on": _parse_depends_on(depends_on),
        "attachments": attachments,
    }


class DictCatalog:
    """In-memory catalog for tests and for replaying a bundle's provenance.

    Accepts rows in the same dict shape MySQLCatalog returns; missing keys get sane defaults
    so a fixture can stay terse.
    """

    def __init__(self, rows):
        self.rows = {}
        for row in rows:
            self.rows[row["name"]] = {
                "id": row.get("id"),
                "name": row["name"],
                "description": row.get("description"),
                "platform": row.get("platform", "linux"),
                "category": row.get("category", "misconfiguration"),
                "script": row["script"],
                "run_as": row.get("run_as", "root"),
                "type": row.get("type", "bash"),
                "depends_on": row.get("depends_on") or [],
                "attachments": row.get("attachments") or [],
            }

    def fetch(self, name):
        row = self.rows.get(name)
        return dict(row) if row is not None else None

    def all_names(self):
        return sorted(self.rows)

    def all_rows(self):
        return [dict(self.rows[name]) for name in sorted(self.rows)]

    def close(self):
        pass


class MySQLCatalog:
    """The live vulndb catalog. Build-side only — never imported on the deploy host."""

    def __init__(self, connection):
        self.connection = connection
        # Buffered so a SELECT that returns rows is fully drained before the next query;
        # otherwise mysql.connector raises "Unread result found" (same reason the old
        # deploy.py used a buffered cursor).
        self.cursor = connection.cursor(buffered=True)
        self._cache = {}
        self._columns = self._detect_columns()

    def _detect_columns(self):
        """Pick the column list this catalog actually has. One cheap query at connect time."""
        try:
            self.cursor.execute("SHOW COLUMNS FROM configurations LIKE 'description'")
            has_description = self.cursor.fetchone() is not None
        except Exception:
            has_description = False
        return _CONFIG_COLUMNS if has_description else _CONFIG_COLUMNS_LEGACY

    @classmethod
    def from_env(cls):
        """Connect using the lowercase .env keys the rest of the toolchain already uses."""
        import mysql.connector  # noqa: PLC0415 — deploy hosts don't have this installed

        try:
            connection = mysql.connector.connect(
                host=os.getenv("host"),
                user=os.getenv("user"),
                password=os.getenv("password"),
                database=os.getenv("database"),
            )
        except Exception as exc:
            raise CatalogError(
                f"could not connect to the vulndb MySQL catalog at {os.getenv('host')!r}: {exc}\n"
                f"`nakon build` must run somewhere the vulndb is reachable (check .env)."
            ) from exc
        return cls(connection)

    def fetch(self, name: str):
        """Look up a configuration by name, with its attachments. None if absent."""
        if name in self._cache:
            cached = self._cache[name]
            return dict(cached) if cached is not None else None

        self.cursor.execute(
            f"SELECT {self._columns} FROM configurations WHERE name = %s", (name,)
        )
        row = self.cursor.fetchone()
        if row is None:
            self._cache[name] = None
            return None

        self.cursor.execute(
            "SELECT id, object_key, original_name, mime_type, size_bytes "
            "FROM attachments WHERE configuration_id = %s ORDER BY id",
            (row[0],),
        )
        attachments = [
            {
                "id": a_id,
                "object_key": object_key,
                "original_name": original_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
            }
            for a_id, object_key, original_name, mime_type, size_bytes in self.cursor.fetchall()
        ]

        result = _row_to_dict(row, attachments)
        self._cache[name] = result
        return dict(result)

    def all_names(self):
        self.cursor.execute("SELECT name FROM configurations ORDER BY name")
        return [name for (name,) in self.cursor.fetchall()]

    def all_rows(self):
        """Every configuration with its attachments, in two queries.

        `nakon catalog` wants the whole catalog at once; doing that through fetch() would be one
        round trip per row plus one per row's attachments. Results are folded into the same cache
        fetch() uses, so a later resolve() over the same source costs nothing.
        """
        self.cursor.execute(f"SELECT {self._columns} FROM configurations ORDER BY name")
        rows = self.cursor.fetchall()

        self.cursor.execute(
            "SELECT configuration_id, id, object_key, original_name, mime_type, size_bytes "
            "FROM attachments ORDER BY id"
        )
        by_config = {}
        for config_id, a_id, object_key, original_name, mime_type, size_bytes in self.cursor.fetchall():
            by_config.setdefault(config_id, []).append({
                "id": a_id,
                "object_key": object_key,
                "original_name": original_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
            })

        result = []
        for row in rows:
            entry = _row_to_dict(row, by_config.get(row[0], []))
            self._cache[entry["name"]] = entry
            result.append(dict(entry))
        return result

    def close(self):
        try:
            self.cursor.close()
        finally:
            self.connection.close()


class HttpCatalog:
    """Read-only catalog over vulndb-ui's HTTP API.

    Exists so an agent can query the catalog with nothing but a reachable vulndb-ui — no MySQL
    credentials, no .env, no SSH tunnel of its own. `nakon catalog` uses it; `nakon build` does
    not, and must not: the API omits attachments' `object_key`, which source_fingerprint() hashes,
    and the manifest's provenance records a database host that only the MySQL path can vouch for.

    One GET fetches everything (the API has no filtering), so this is a single round trip.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._rows = None

    @classmethod
    def from_env(cls):
        base = os.getenv("VULNDB_UI_URL", "").strip()
        if not base:
            raise CatalogError(
                "VULNDB_UI_URL is not set, so the HTTP catalog has nowhere to look. "
                "Set it to a running vulndb-ui (e.g. http://127.0.0.1:3000), or pass "
                "--source mysql to read the database directly."
            )
        return cls(base)

    def _load(self):
        if self._rows is not None:
            return self._rows

        import requests  # noqa: PLC0415 — build-side only; deploy hosts lack this

        url = f"{self.base_url}/api/configurations"
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise CatalogError(
                f"could not read the catalog from vulndb-ui at {url}: {exc}\n"
                f"Is vulndb-ui running? (VULNDB_UI_URL={self.base_url})"
            ) from exc

        self._rows = {}
        for entry in payload:
            self._rows[entry["name"]] = {
                "id": entry.get("id"),
                "name": entry["name"],
                "description": entry.get("description"),
                "platform": entry.get("platform", "linux"),
                "category": entry.get("category", "misconfiguration"),
                "script": entry.get("script") or "",
                "run_as": entry.get("run_as") or "root",
                "type": entry.get("type") or "bash",
                "depends_on": _parse_depends_on(entry.get("depends_on")),
                # No object_key here — the API doesn't expose it. Harmless for the catalog
                # commands, which never download bytes, and the reason build stays on MySQL.
                "attachments": entry.get("attachments") or [],
            }
        return self._rows

    def fetch(self, name: str):
        row = self._load().get(name)
        return dict(row) if row is not None else None

    def all_names(self):
        return sorted(self._load())

    def all_rows(self):
        rows = self._load()
        return [dict(rows[name]) for name in sorted(rows)]

    def close(self):
        pass
