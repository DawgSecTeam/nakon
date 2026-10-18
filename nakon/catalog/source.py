"""Catalog sources. Build-side imports stay inside functions (deploy has only paramiko)."""

import json
import os

from ..errors import CatalogError

# Read the row's authoring metadata too: `required_vars` feeds `catalog list/show` and
# `check`'s missing-vars gate. Required since the vulndb gained the column — it is now
# part of what a selection means (an `Unknown column 'required_vars'` here means the
# vulndb host has not had its schema migrated to nakon's version; migrate, don't soften).
_CONFIG_COLUMNS = ("id, name, description, required_vars, platform, category, script, "
                   "run_as, type, depends_on")


def _parse_json_list(raw):
    """depends_on / required_vars are LONGTEXT holding a JSON array of strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return json.loads(raw)


def _row_to_dict(row, attachments):
    (id_, name, description, required_vars, platform, category,
     script, run_as, cfg_type, depends_on) = row
    return {
        "id": id_,
        "name": name,
        "description": description,
        "required_vars": _parse_json_list(required_vars),
        "platform": platform,
        "category": category,
        "script": script,
        "run_as": run_as,
        "type": cfg_type,
        "depends_on": _parse_json_list(depends_on),
        "attachments": attachments,
    }


class MySQLCatalog:
    """The live vulndb catalog."""

    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.cursor(buffered=True)
        self._cache = {}

    @classmethod
    def from_env(cls):
        import mysql.connector  # noqa: PLC0415

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
        if name in self._cache:
            cached = self._cache[name]
            return dict(cached) if cached is not None else None

        self.cursor.execute(
            f"SELECT {_CONFIG_COLUMNS} FROM configurations WHERE name = %s", (name,)
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


    def all_rows(self):
        self.cursor.execute(f"SELECT {_CONFIG_COLUMNS} FROM configurations ORDER BY name")
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
    """Read-only catalog over vulndb-ui's HTTP API. `nakon build` must not use it."""

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

        import requests  # noqa: PLC0415

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
                "required_vars": _parse_json_list(entry.get("required_vars")),
                "depends_on": _parse_json_list(entry.get("depends_on")),
                "attachments": entry.get("attachments") or [],
            }
        return self._rows

    def fetch(self, name: str):
        row = self._load().get(name)
        return dict(row) if row is not None else None


    def all_rows(self):
        rows = self._load()
        return [dict(rows[name]) for name in sorted(rows)]

    def close(self):
        pass
