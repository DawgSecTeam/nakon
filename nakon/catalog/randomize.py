"""Randomized catalog selection: budgeted services plus vulns/misconfigurations."""

import random

# Building blocks pulled in via depends_on, never requested directly.
EXCLUDED_NAMES = {"install-package", "create-user", "enable-service"}


def dep_names(depends_on):
    for entry in depends_on:
        yield entry if isinstance(entry, str) else entry["name"]


def service_closure(name, name_to_row, visited=None):
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
