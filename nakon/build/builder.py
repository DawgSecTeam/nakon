"""Turn a config.json into a content-addressed bundle.

Plans are keyed by `request_key` — platform plus ordered request list — never by
machine name, so teams sharing a configuration set share one plan.
"""

import getpass
import json
import os
import platform as _platform
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..catalog.resolve import resolve
from ..machines import load_machines
from ..gen import bash as gen_bash
from ..gen import powershell as gen_ps
from ..gen import step_basename
from ..hashing import (
    SCHEMA,
    bundle_id as compute_bundle_id,
    config_digest,
    normalize_request,
    plan_id as compute_plan_id,
    request_key,
    sha256_file,
    sha256_json,
    sha256_text,
    short,
)
from .fetch import AttachmentFetcher
from .tarball import write_tar_gz, write_zip

MANIFEST_NAME = "manifest.json"
CACHE_DIRNAME = ".cache"
# Length of the hex prefix used for on-disk directory names. Full ids live in the manifest.
DIR_ID_LEN = 16


def collect_requests(machines: list) -> dict:
    """Group machines by request_key."""
    requests = {}
    for machine in machines:
        key = request_key(machine["platform"], machine["configurations"])
        machine["request_key"] = key
        requests.setdefault(key, {
            "platform": machine["platform"],
            "requested": normalize_request(machine["configurations"]),
        })
    return requests


def source_fingerprint(requests: dict, resolved: dict) -> str:
    """Hash what the catalog contributed, without touching attachment bytes."""
    doc = {"schema": SCHEMA, "kind": "source", "requests": {}, "resolved": {}}
    for key, request in requests.items():
        doc["requests"][key] = {"platform": request["platform"], "requested": request["requested"]}
    for key, steps in resolved.items():
        summary = []
        for step in steps:
            summary.append({
                "kind": "config",
                "name": step["name"],
                "config_id": step["config_id"],
                "script_sha256": sha256_text(step["script"]),
                "run_as": step["run_as"],
                "type": step["type"],
                "vars": step["vars"],
                "attachments": [
                    {
                        "id": a["id"],
                        "object_key": a["object_key"],
                        "original_name": a["original_name"],
                        "size_bytes": a["size_bytes"],
                    }
                    for a in step["attachments"]
                ],
            })
        doc["resolved"][key] = summary
    return sha256_json(doc)


def plan_document(platform: str, steps: list, attachment_hashes: dict) -> dict:
    """Canonical hashed identity of a plan. Excludes DB surrogate keys."""
    doc_steps = []
    for step in steps:
        doc_steps.append({
            "kind": "config",
            "name": step["name"],
            "type": step["type"],
            "run_as": step["run_as"],
            "vars": step["vars"],
            "script_sha256": sha256_text(step["script"]),
            "attachments": [
                {
                    "original_name": os.path.basename(a["original_name"]),
                    "sha256": attachment_hashes[a["id"]],
                }
                for a in step["attachments"]
            ],
        })
    return {"schema": SCHEMA, "platform": platform, "steps": doc_steps}


def _lint_attachment_paths(step: dict, warn) -> None:
    """Warn if a script hardcodes /tmp/<its own attachment>; use ./<name> instead."""
    for attachment in step["attachments"]:
        name = os.path.basename(attachment["original_name"])
        if f"/tmp/{name}" in step["script"]:
            warn(
                f"configuration '{step['name']}' references /tmp/{name} directly. "
                f"Attachments now live in the step's working directory — use ./{name}."
            )


def _lint_line_endings(step: dict, platform: str, warn) -> None:
    """Warn about CRLF in a Linux script; bash fails on it."""
    if platform != "windows" and "\r" in step["script"]:
        warn(
            f"configuration '{step['name']}' contains carriage returns (CRLF line endings); "
            f"bash on the target will fail on them. Fix the script in vulndb."
        )


def build(
    source,
    config_path: Path,
    out_dir: Path,
    log=print,
    warn=None,
) -> dict:
    """Build a bundle. Content-addressed: rebuilding the same input rewrites
    the same directory (attachment downloads are still cached under .cache)."""
    out_dir = Path(out_dir)
    warn = warn or (lambda message: log(f"[nakon] WARNING: {message}"))

    machines = load_machines(config_path, warn=warn)
    requests = collect_requests(machines)
    log(f"[nakon] {len(machines)} machine(s), {len(requests)} distinct configuration set(s)")

    # Resolve first: the fingerprint needs its output.
    resolved = {}
    for key, request in requests.items():
        resolved[key] = resolve(source, request["requested"], request["platform"])

    fingerprint = source_fingerprint(requests, resolved)

    # Download attachments once each.
    fetcher = AttachmentFetcher(out_dir / CACHE_DIRNAME / "attachments")
    attachment_hashes = {}
    attachment_paths = {}
    for key, steps in resolved.items():
        for step in steps:
            _lint_attachment_paths(step, warn)
            _lint_line_endings(step, requests[key]["platform"], warn)
            for attachment in step["attachments"]:
                if attachment["id"] in attachment_hashes:
                    continue
                local_path, digest = fetcher.fetch(attachment)
                attachment_hashes[attachment["id"]] = digest
                attachment_paths[attachment["id"]] = local_path
    if attachment_hashes:
        log(f"[nakon] {len(attachment_hashes)} attachment(s): "
            f"{fetcher.hits} cached, {fetcher.misses} downloaded")

    plan_docs = {}
    request_to_plan = {}
    plan_steps = {}
    for key, steps in resolved.items():
        doc = plan_document(requests[key]["platform"], steps, attachment_hashes)
        pid = compute_plan_id(doc)
        plan_docs[pid] = doc
        plan_steps[pid] = steps
        request_to_plan[key] = pid

    bid = compute_bundle_id(plan_docs, request_to_plan)
    bundle_dir = out_dir / short(bid, DIR_ID_LEN)

    if bundle_dir.exists():
        # Rebuild rather than trusting a possibly half-written directory.
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    blobs_dir = bundle_dir / "blobs"
    blobs_dir.mkdir()

    plan_entries = {}
    for pid, steps in plan_steps.items():
        plan_entries[pid] = _write_plan(
            bundle_dir, blobs_dir, pid, plan_docs[pid], steps, attachment_paths, bid
        )

    manifest = {
        "schema": SCHEMA,
        "bundle_id": bid,
        "source_fingerprint": fingerprint,
        "config_digest": config_digest(machines),
        "nakon_version": __version__,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_by": f"{getpass.getuser()}@{socket.gethostname()}",
        "built_on": _platform.platform(),
        "source": {
            "db_host": os.getenv("host"),
            "database": os.getenv("database"),
            "vulndb_ui_url": os.getenv("VULNDB_UI_URL"),
        },
        "requests": {
            key: {
                "plan_id": request_to_plan[key],
                "platform": requests[key]["platform"],
                "requested": requests[key]["requested"],
            }
            for key in sorted(requests)
        },
        "plans": plan_entries,
        "inventory": [
            {
                "name": machine["name"],
                "ip": machine["ip"],
                "os": machine["os"],
                "platform": machine["platform"],
                "request_key": machine["request_key"],
            }
            for machine in machines
        ],
        "provenance": _provenance(resolved, attachment_hashes),
    }

    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    log(f"[nakon] built bundle {short(bid)} at {bundle_dir}")
    log(f"[nakon]   {len(plan_entries)} plan(s), "
        f"{sum(len(p['steps']) for p in plan_entries.values())} step(s) total")
    return {
        "bundle_id": bid,
        "path": str(bundle_dir),
        "plans": len(plan_entries),
        "machines": len(machines),
    }


def _store_blob(blobs_dir: Path, digest: str, data: bytes = None, src: Path = None) -> Path:
    dest = blobs_dir / digest
    if not dest.exists():
        if data is not None:
            dest.write_bytes(data)
        else:
            shutil.copyfile(src, dest)
    return dest


def _write_plan(bundle_dir, blobs_dir, pid, doc, steps, attachment_paths, bid) -> dict:
    is_windows = doc["platform"] == "windows"
    plan_rel = f"plans/{short(pid, DIR_ID_LEN)}"
    plan_dir = bundle_dir / plan_rel
    (plan_dir / "steps").mkdir(parents=True)
    (plan_dir / "vars").mkdir()
    (plan_dir / "files").mkdir()

    for index, step in enumerate(steps):
        idx = f"{index:03d}"
        basename = step_basename(index, step, doc["platform"])
        step_path = plan_dir / "steps" / basename

        if is_windows:
            body = gen_ps.render_step_ps1(step["script"], step["vars"])
        else:
            body = step["script"]

        _store_blob(blobs_dir, sha256_text(body), data=body.encode("utf-8"))
        if is_windows:
            # PowerShell 5.1 misreads BOM-less UTF-8 as ANSI.
            step_path.write_text(body, encoding="utf-8-sig", newline="\r\n")
        else:
            step_path.write_text(body, encoding="utf-8", newline="\n")
        step_path.chmod(0o755)

        if step["vars"] and not is_windows:
            (plan_dir / "vars" / f"{idx}.env").write_text(
                gen_bash.render_vars(step["vars"]), encoding="utf-8", newline="\n"
            )

        work_dir = plan_dir / "files" / idx
        work_dir.mkdir()
        for attachment in step["attachments"]:
            src = attachment_paths[attachment["id"]]
            digest = sha256_file(src)
            blob = _store_blob(blobs_dir, digest, src=src)
            name = os.path.basename(attachment["original_name"])
            shutil.copyfile(blob, work_dir / name)

    renderer = gen_ps.render_run_ps1 if is_windows else gen_bash.render_run_sh
    driver_name = "run.ps1" if is_windows else "run.sh"
    driver = renderer(steps, pid, bid, __version__)
    driver_path = plan_dir / driver_name
    if is_windows:
        driver_path.write_text(driver, encoding="utf-8-sig", newline="\r\n")
    else:
        driver_path.write_text(driver, encoding="utf-8", newline="\n")
    driver_path.chmod(0o755)

    (plan_dir / "plan.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

    # One archive per plan: a box must not receive other boxes' plans.
    archive_rel = f"{plan_rel}.{'zip' if is_windows else 'tar.gz'}"
    archive_path = bundle_dir / archive_rel
    (write_zip if is_windows else write_tar_gz)(plan_dir, archive_path)

    return {
        "platform": doc["platform"],
        "dir": plan_rel,
        "driver": driver_name,
        "tarball": archive_rel,
        "tarball_sha256": sha256_file(archive_path),
        "tarball_bytes": archive_path.stat().st_size,
        "steps": [
            {"index": i, **entry} for i, entry in enumerate(doc["steps"])
        ],
    }


def _provenance(resolved: dict, attachment_hashes: dict) -> dict:
    """Catalog rows that produced this bundle, for `nakon diff`."""
    configurations = {}
    attachments = {}
    for steps in resolved.values():
        for step in steps:
            configurations[str(step["config_id"])] = {
                "name": step["name"],
                "script_sha256": sha256_text(step["script"]),
                "run_as": step["run_as"],
                "type": step["type"],
                "depends_on": normalize_request(step["depends_on"]),
            }
            for attachment in step["attachments"]:
                attachments[str(attachment["id"])] = {
                    "configuration": step["name"],
                    "object_key": attachment["object_key"],
                    "original_name": attachment["original_name"],
                    "size_bytes": attachment["size_bytes"],
                    "sha256": attachment_hashes[attachment["id"]],
                }
    return {"configurations": configurations, "attachments": attachments}
