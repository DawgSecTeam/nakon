"""Read a built bundle and map machines onto plans by request_key."""

import json
from pathlib import Path

from ..errors import BundleError
from ..hashing import SCHEMA, config_digest, request_key, short

MANIFEST_NAME = "manifest.json"


class Bundle:
    def __init__(self, path: Path, manifest: dict):
        self.path = Path(path)
        self.manifest = manifest
        self.bundle_id = manifest["bundle_id"]

    @classmethod
    def load(cls, path):
        path = Path(path)
        manifest_path = path / MANIFEST_NAME
        if not manifest_path.is_file():
            raise BundleError(
                f"{path} is not a bundle (no {MANIFEST_NAME}). Pass the bundle directory, "
                f"e.g. bundles/<id>/ — run `nakon build` first if you haven't."
            )
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise BundleError(f"{manifest_path} is corrupt: {exc}") from exc

        if manifest.get("schema") != SCHEMA:
            raise BundleError(
                f"bundle {path} has schema {manifest.get('schema')}, this nakon speaks "
                f"{SCHEMA}. Rebuild it with `nakon build`."
            )
        return cls(path, manifest)

    def verify_config(self, machines: list) -> None:
        """Prove the config being deployed is the one this bundle was built from.

        Catches drift that request_key routing cannot: a renamed or re-listed
        machine whose request still matches some plan in the bundle. Checked on
        the full machine list, before --only filtering, so subset deploys pass.
        """
        recorded = self.manifest.get("config_digest")
        if not recorded:
            raise BundleError(
                f"bundle {short(self.bundle_id)} has no config_digest (built by an "
                f"older nakon). Rebuild it with `nakon build`."
            )
        current = config_digest(machines)
        if current != recorded:
            raise BundleError(
                f"the config differs from the one bundle {short(self.bundle_id)} was "
                f"built from (digest {short(current)} != {short(recorded)}). Rebuild "
                f"the bundle, or deploy the one that matches this config."
            )

    def plan_for(self, machine: dict) -> tuple:
        key = request_key(machine["platform"], machine["configurations"])
        entry = self.manifest["requests"].get(key)
        if entry is None:
            raise BundleError(
                f"machine '{machine['name']}' asks for a configuration set this bundle "
                f"doesn't contain.\n"
                f"  request  : {[c if isinstance(c, str) else c.get('name') for c in machine['configurations']]}\n"
                f"  platform : {machine['platform']}\n"
                f"  bundle   : {short(self.bundle_id)} ({len(self.manifest['requests'])} set(s))\n"
                f"The config.json changed since this bundle was built — rebuild it, or "
                f"deploy the bundle that matches."
            )
        plan_id = entry["plan_id"]
        return plan_id, self.manifest["plans"][plan_id]

    def inventory_entry(self, machine_name: str):
        for entry in self.manifest.get("inventory", []):
            if entry["name"] == machine_name:
                return entry
        return None

    def archive_path(self, plan_entry: dict) -> Path:
        path = self.path / plan_entry["tarball"]
        if not path.is_file():
            raise BundleError(f"bundle is missing its plan archive: {path}")
        return path
