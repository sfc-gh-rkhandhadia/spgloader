"""Topological dependency graph for DDL objects — Kahn's algorithm."""
from __future__ import annotations

from collections import defaultdict, deque


def build_graph(objects: list[dict]) -> tuple[dict, dict]:
    """Build adjacency structures for topological sort (case-insensitive deps)."""
    fqn_set = {obj["fqn"] for obj in objects}
    name_to_fqn = {obj["name"].upper(): obj["fqn"] for obj in objects}

    in_degree = {obj["fqn"]: 0 for obj in objects}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for obj in objects:
        for dep in obj.get("depends_on", []):
            dep_fqn = dep if dep in fqn_set else name_to_fqn.get(dep.upper())
            if dep_fqn and dep_fqn != obj["fqn"]:
                adjacency[dep_fqn].append(obj["fqn"])
                in_degree[obj["fqn"]] += 1

    return in_degree, adjacency


def topological_sort(objects: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Kahn's algorithm.

    Returns:
        ordered: objects in safe deployment order (leaves first)
        cycles: fqns that could not be sorted (circular dependency)
    """
    fqn_to_obj = {obj["fqn"]: obj for obj in objects}
    in_degree, adjacency = build_graph(objects)

    queue = deque(fqn for fqn, deg in in_degree.items() if deg == 0)
    ordered: list[dict] = []

    while queue:
        fqn = queue.popleft()
        ordered.append(fqn_to_obj[fqn])
        for dependent in adjacency[fqn]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    cycles = [fqn for fqn, deg in in_degree.items() if deg > 0]
    return ordered, cycles


def build_dep_graph_result(objects: list[dict]) -> dict:
    """Build the full dep_graph.json structure (ordered + cycles appended)."""
    ordered, cycles = topological_sort(objects)
    fqn_to_obj = {obj["fqn"]: obj for obj in objects}

    ordered_entries = [
        {"fqn": o["fqn"], "type": o["type"], "schema": o.get("schema", ""), "name": o["name"]}
        for o in ordered
    ]
    # Append cyclic objects at end (best effort)
    for fqn in cycles:
        if fqn in fqn_to_obj:
            obj = fqn_to_obj[fqn]
            ordered_entries.append({
                "fqn": fqn, "type": obj["type"],
                "schema": obj.get("schema", ""), "name": obj["name"],
                "cycle_warning": True,
            })

    return {
        "ordered_objects": ordered_entries,
        "cycles": cycles,
        "total": len(objects),
        "sorted": len(ordered),
    }
