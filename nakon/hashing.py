"""Canonical JSON serialization and the hashes bundles are built around.

`request_key` identifies what was asked for; `plan_id` a resolved step list;
`bundle_id` the whole bundle. `config_digest` covers the machine list a bundle
was built from, so deploy can prove it is applying the same build input.
`source_fingerprint` (see build.builder) hashes what the catalog contributed.

Hashes cover canonical JSON, never archive bytes.
"""

import hashlib
import json

# Bump when a hashed document shape changes; it re-keys everything at once.
SCHEMA = 4


def canon(obj) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, ASCII-escaped."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(obj) -> str:
    return sha256_bytes(canon(obj))


def sha256_file(path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_request(requested: list) -> list:
    """Normalize `configurations` to [{name, vars}]. Order is preserved and load-bearing."""
    normalized = []
    for entry in requested:
        if isinstance(entry, str):
            normalized.append({"name": entry, "vars": {}})
        else:
            normalized.append({
                "name": entry["name"],
                # Coerce to str so {"PORT": 8080} and {"PORT": "8080"} hash the same.
                "vars": {k: str(v) for k, v in (entry.get("vars") or {}).items()},
            })
    return normalized


def request_key(platform: str, requested: list) -> str:
    """Hash a request: platform plus normalized, ordered config list."""
    return sha256_json({
        "schema": SCHEMA,
        "kind": "request",
        "platform": platform,
        "requested": normalize_request(requested),
    })


def config_digest(machines: list) -> str:
    """Hash the machine list a bundle was built from.

    Covers name, platform and the normalized, ordered configuration list —
    everything that defines the build input and its human handle. Excludes
    address and credential fields (ip, user, password, identity_file): those
    are reconciled per deploy and may be rewritten after the build.
    """
    return sha256_json({
        "schema": SCHEMA,
        "kind": "config-digest",
        "machines": [
            {
                "name": machine["name"],
                "platform": machine["platform"],
                "configurations": normalize_request(machine["configurations"]),
            }
            for machine in machines
        ],
    })


def plan_id(plan_doc: dict) -> str:
    """Hash a resolved plan document (see build.builder.plan_document)."""
    return sha256_json(plan_doc)


def bundle_id(plans: dict, requests: dict) -> str:
    """Hash bundle identity: every plan document plus the request->plan mapping."""
    return sha256_json({
        "schema": SCHEMA,
        "kind": "bundle",
        "plans": plans,
        "requests": requests,
    })


def short(digest: str, length: int = 12) -> str:
    """Shorten a hex digest for display and for on-disk directory names."""
    return digest[:length]
