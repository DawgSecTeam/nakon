"""Randomized config.json generation — picks services and vulns scaled by a difficulty level.

`pick_configurations` is the selection algorithm. It is called two ways:

  * `python3 -m nakon randomize --services N --vulns M --json` — headless, for orchestrators
    (tezcatlipoca's generate_nakon_config). The CLI wiring lives in cli.py:_randomize_select.
  * `main()` here — the interactive standalone flow that prompts for VMs/difficulty and writes
    a config.json, for a human building a one-off range by hand.
"""

import json
import math
import os
import random
import subprocess
from pathlib import Path

from ..machines import os_to_platform  # noqa: F401 — re-exported; callers import it from here
from .source import _parse_depends_on

# Reusable building blocks meant to be pulled in via depends_on, not requested directly
# (install-package, create-user, enable-service).
EXCLUDED_NAMES = {"install-package", "create-user", "enable-service"}

DIFFICULTY_MIN, DIFFICULTY_MAX = 1, 10


def prompt_int(prompt, default=None, min_value=None, max_value=None):
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("  Please enter a whole number.")
            continue
        if min_value is not None and value < min_value:
            value = min_value
        if max_value is not None and value > max_value:
            value = max_value
        return value


def find_terraform_dir():
    candidates = []
    env_dir = os.getenv("TERRAFORM_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates += ["./terraform", "../terraform", os.path.expanduser("~/dev/competition-deployment-tool/terraform")]

    for candidate in candidates:
        if (Path(candidate) / "main.tf").is_file():
            return str(candidate)
    return None


def fetch_terraform_machines(terraform_dir):
    """Run `terraform output -json` and turn agent_context into a list of candidate machines."""
    result = subprocess.run(
        ["terraform", f"-chdir={terraform_dir}", "output", "-json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"  [terraform] failed: {result.stderr.strip()}")
        return []

    raw = json.loads(result.stdout)
    ctx = json.loads(raw["agent_context"]["value"])

    machines = []
    for team_key, identifier in ctx["teams"].items():
        for box in ctx["boxes_per_team"]:
            machines.append({
                "name": f"{box['name']}-team{identifier}",
                "ip": f"192.168.{identifier}.{box['last_octet']}",
                "os": "linux",
                "user": "ubuntu",
                "password": "ubuntu",
            })
    return machines


def prompt_machine(index):
    print(f"  VM {index} connection details:")
    return {
        "name": input("    name: ").strip(),
        "ip": input("    ip: ").strip(),
        "os": input("    os (e.g. ubuntu22): ").strip(),
        "user": input("    user: ").strip(),
        "password": input("    password: ").strip(),
    }


def collect_machines(num_vms):
    terraform_dir = find_terraform_dir()
    prompt = f"Terraform directory to read VMs from [{terraform_dir or ''}] (blank to skip): "
    chosen_dir = input(prompt).strip() or terraform_dir

    machines = []
    if chosen_dir:
        print(f"[terraform] reading machines from {chosen_dir}")
        try:
            machines = fetch_terraform_machines(chosen_dir)
            print(f"[terraform] found {len(machines)} machine(s)")
        except Exception as exc:
            print(f"[terraform] could not read output: {exc}")
            machines = []

    machines = machines[:num_vms]
    for i in range(len(machines) + 1, num_vms + 1):
        machines.append(prompt_machine(i))

    for i, machine in enumerate(machines, start=1):
        machine["id"] = i
    return machines


def load_configurations(cursor):
    cursor.execute("SELECT name, category, platform, depends_on FROM configurations")
    name_to_row = {}
    for name, category, platform, depends_on_raw in cursor.fetchall():
        if name in EXCLUDED_NAMES:
            continue
        name_to_row[name] = {
            "category": category, "platform": platform,
            "depends_on": _parse_depends_on(depends_on_raw),
        }
    return name_to_row


def dep_names(depends_on):
    """depends_on entries are a bare string or {"name": ..., "vars": {...}}."""
    for entry in depends_on:
        yield entry if isinstance(entry, str) else entry["name"]


def service_closure(name, name_to_row, visited=None):
    """All configuration names transitively required by `name` (incl. itself) that are services."""
    if visited is None:
        visited = set()
    if name in visited or name not in name_to_row:
        return set()
    visited.add(name)

    row = name_to_row[name]
    services = {name} if row["category"] == "service" else set()
    for dep in dep_names(row["depends_on"]):
        services |= service_closure(dep, name_to_row, visited)
    return services


def pick_configurations(name_to_row, platform, num_services, num_vulns):
    services_pool = [n for n, r in name_to_row.items() if r["category"] == "service" and r["platform"] == platform]
    vulns_pool = [
        n for n, r in name_to_row.items()
        if r["category"] in ("misconfiguration", "vulnerability") and r["platform"] == platform
    ]
    random.shuffle(services_pool)
    random.shuffle(vulns_pool)

    services_in_use = set()
    accepted_services = []
    for name in services_pool:
        if len(accepted_services) >= num_services:
            break
        closure = service_closure(name, name_to_row)
        if len(services_in_use | closure) <= num_services:
            services_in_use |= closure
            accepted_services.append(name)

    accepted_vulns = []
    for name in vulns_pool:
        if len(accepted_vulns) >= num_vulns:
            break
        closure = service_closure(name, name_to_row)
        if len(services_in_use | closure) <= num_services:
            services_in_use |= closure
            accepted_vulns.append(name)

    return accepted_services, accepted_vulns, services_in_use


def main():
    import mysql.connector  # noqa: PLC0415 — build-side only; deploy hosts lack this
    from dotenv import load_dotenv  # noqa: PLC0415 — build-side only; deploy hosts lack this

    load_dotenv()
    db_config = {
        "host": os.getenv("host"),
        "user": os.getenv("user"),
        "password": os.getenv("password"),
        "database": os.getenv("database"),
    }

    num_vms = prompt_int("How many VMs? ", min_value=1)
    difficulty = prompt_int(
        f"Difficulty ({DIFFICULTY_MIN}-{DIFFICULTY_MAX})? ",
        min_value=DIFFICULTY_MIN, max_value=DIFFICULTY_MAX,
    )

    machines = collect_machines(num_vms)

    mydb = mysql.connector.connect(**db_config)
    cursor = mydb.cursor()
    name_to_row = load_configurations(cursor)
    cursor.close()
    mydb.close()

    for machine in machines:
        platform = os_to_platform(machine["os"])
        services_pool_size = sum(1 for r in name_to_row.values() if r["category"] == "service" and r["platform"] == platform)
        vulns_pool_size = sum(
            1 for r in name_to_row.values()
            if r["category"] in ("misconfiguration", "vulnerability") and r["platform"] == platform
        )
        num_services = min(max(math.ceil(difficulty / 3), 1), services_pool_size) if services_pool_size else 0
        num_vulns = min(max(difficulty, 1), vulns_pool_size) if vulns_pool_size else 0

        services, vulns, services_in_use = pick_configurations(name_to_row, platform, num_services, num_vulns)
        machine["configurations"] = services + vulns

        print(f"\n[{machine['name']}] platform={platform}")
        print(f"  services: {services}")
        print(f"  vulns/misconfigs: {vulns}")
        print(f"  total distinct services after dependency resolution: {len(services_in_use)} (budget {num_services})")

    with open("config.json", "w") as f:
        json.dump({"machines": machines}, f, indent=2)
    print(f"\nWrote config.json with {len(machines)} machine(s).")
