"""Fetch attachment bytes from vulndb-ui (302 -> presigned MinIO URL), with a local cache.

`requests` is imported inside the function that needs it: deploy hosts only have paramiko,
and they import this package transitively through the CLI.

The cache lives at <bundles>/.cache/attachments/ and is keyed by the attachment's object_key,
which embeds a per-upload UUID. It is a pure download cache: nothing in any manifest ever
references it, so it can be deleted at any time without invalidating a bundle.
"""

import os
from pathlib import Path

from ..errors import CatalogError
from ..hashing import sha256_bytes, sha256_file, sha256_text


class AttachmentFetcher:
    def __init__(self, cache_dir: Path, base_url: str = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = (base_url if base_url is not None else os.getenv("VULNDB_UI_URL", "")).rstrip("/")
        self.hits = 0
        self.misses = 0

    def _cache_path(self, attachment: dict) -> Path:
        # object_key is stable per upload (it contains a UUID minted at upload time), so it
        # is a sound cache key. Renaming an attachment changes original_name, not the key —
        # which is why original_name is part of the source fingerprint instead.
        return self.cache_dir / sha256_text(attachment["object_key"])

    def fetch(self, attachment: dict) -> tuple:
        """Return (local_path, sha256) for one attachment, downloading only on a cache miss."""
        cached = self._cache_path(attachment)
        expected_size = attachment.get("size_bytes")

        if cached.exists() and (expected_size is None or cached.stat().st_size == expected_size):
            self.hits += 1
            return cached, sha256_file(cached)

        if not self.base_url:
            raise CatalogError(
                f"VULNDB_UI_URL is not set, but '{attachment['original_name']}' has to be "
                f"downloaded. Set it in .env to the vulndb-ui base URL (see README Setup)."
            )

        import requests  # noqa: PLC0415 — deploy hosts don't have this installed

        url = f"{self.base_url}/api/attachments/{attachment['id']}/download"
        try:
            response = requests.get(url, allow_redirects=True, timeout=60)
            response.raise_for_status()
        except Exception as exc:
            raise CatalogError(
                f"could not download attachment {attachment['id']} "
                f"('{attachment['original_name']}') from {url}: {exc}"
            ) from exc

        content = response.content
        if expected_size is not None and len(content) != expected_size:
            raise CatalogError(
                f"attachment {attachment['id']} ('{attachment['original_name']}') is "
                f"{len(content)} bytes but the catalog says {expected_size}. Refusing to "
                f"bundle a file that doesn't match its own metadata."
            )

        # Write via a temp file so a killed build can't leave a truncated entry in the cache.
        tmp = cached.with_suffix(".partial")
        tmp.write_bytes(content)
        tmp.replace(cached)
        self.misses += 1
        return cached, sha256_bytes(content)
