"""Deterministic archives: stable mtimes, ids, ordering, and gzip headers.

Bundle ids come from canonical JSON (see hashing), not these bytes; this is
for diffing and transport checksums.
"""

import gzip
import io
import os
import tarfile
import zipfile
from pathlib import Path


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o755 if (info.mode & 0o100) else 0o644
    if info.isdir():
        info.mode = 0o755
    return info


def _walk_sorted(root: Path):
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            entries.append((full, str(full.relative_to(root))))
    entries.sort(key=lambda pair: pair[1])
    return entries


def write_tar_gz(source_dir: Path, dest: Path) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for full, arcname in _walk_sorted(source_dir):
            info = _normalize(tar.gettarinfo(str(full), arcname=arcname))
            if info.isreg():
                with open(full, "rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(raw.getvalue())


def write_zip(source_dir: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for full, arcname in _walk_sorted(source_dir):
            if full.is_dir():
                continue
            info = zipfile.ZipInfo(arcname.replace(os.sep, "/"), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, full.read_bytes())
