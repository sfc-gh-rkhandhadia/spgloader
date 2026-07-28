#!/usr/bin/env python3
"""build_dep_graph.py - Build dependency graph, detect cycles, and partition into deployment waves."""

import argparse
import json
import os
import sys
from collections import defaultdict, deque
from typing import Any


def build_adj(inventory: dict) -> tuple[dict[str, set], dict[str, set]]:
    """
    deps[fqn]  = set of fqns this object directly depends on
    rdeps[fqn] = set of fqns that depend on this object
    """
    objects = {o["fqn"]: o for o in inventory["objects"]}
    deps: dict[str, set] = defaultdict(set)
    rdeps: dict[str, set] = defaultdict(set)

    for fqn, obj in objects.items():
        # FK references from tables
        for fk in obj.get("fk_references", []):
            ref = f"{fk['ref_schema']}.{fk['ref_table']}"
            if ref in objects and ref != fqn:
                deps[fqn].add(ref)
                rdeps[ref].add(fqn)

        # Body dependencies from views/procs/functions
        for dep in obj.get("dependencies", []):
            if dep in objects and dep != fqn:
                deps[fqn].add(dep)
                rdeps[dep].add(fqn)

    return deps, rdeps


def detect_cycles(fqns: list[str], deps: dict[str, set]) -> list[list[str]]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in fqns}
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for nb in deps.get(node, set()):
            if nb not in color:
                continue
            if color[nb] == GRAY:
                start = path.index(nb)
                cycles.append(list(path[start:]))
            elif color[nb] == WHITE:
                dfs(nb, path)
        path.pop()
        color[node] = BLACK

    for node in list(fqns):
        if color.get(node) == WHITE:
            dfs(node, [])

    return cycles


def topological_waves(fqns: list[str], deps: dict[str, set]) -> list[list[str]]:
    """Kahn's algorithm. Returns list of waves; wave 0 has no dependencies."""
    in_degree = {n: len(deps.get(n, set())) for n in fqns}
    queue = deque(n for n, d in in_degree.items() if d == 0)
    processed: set[str] = set()
    waves: list[list[str]] = []

    while queue:
        wave = list(queue)
        queue.clear()
        waves.append(wave)
        for node in wave:
            processed.add(node)
            for candidate, candidate_deps in deps.items():
                if node in candidate_deps and candidate not in processed:
                    in_degree[candidate] -= 1
                    if in_degree[candidate] == 0:
                        queue.append(candidate)

    unplaced = [n for n in fqns if n not in processed]
    if unplaced:
        waves.append(unplaced)  # cycle members land here

    return waves


def witness_paths(
    objects: dict[str, Any], deps: dict[str, set]
) -> dict[str, list[str]]:
    """For each row-producing target, BFS upstream to find all root tables required."""
    tables = {fqn for fqn, o in objects.items() if o["type"] == "TABLE"}
    result: dict[str, list[str]] = {}

    for fqn, obj in objects.items():
        if not obj.get("row_producing"):
            continue
        visited: set[str] = set()
        queue = deque([fqn])
        roots: list[str] = []
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            if node in tables and node != fqn:
                roots.append(node)
            for upstream in deps.get(node, set()):
                queue.append(upstream)
        result[fqn] = sorted(set(roots))

    return result


def missing_references(inventory: dict, known: set[str]) -> list[dict]:
    out = []
    for obj in inventory["objects"]:
        fqn = obj["fqn"]
        for dep in obj.get("dependencies", []):
            if dep not in known:
                out.append({"object": fqn, "missing_dep": dep})
        for fk in obj.get("fk_references", []):
            ref = f"{fk['ref_schema']}.{fk['ref_table']}"
            if ref not in known:
                out.append({"object": fqn, "missing_fk": ref})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build MSSQL dependency graph and deployment waves"
    )
    parser.add_argument(
        "--inventory", required=True, help="inventory.json from parse_ddl.py"
    )
    parser.add_argument("--output", required=True, help="dep_graph.json output path")
    parser.add_argument(
        "--constraints-file",
        default=None,
        help="Optional path to spg_column_constraints.json produced by "
             "mssql-spg-load/scripts/discover_spg_constraints.py. "
             "When absent, column_value_constraints is emitted as {} "
             "(backward-compatible, standalone use).",
    )
    args = parser.parse_args()

    with open(args.inventory) as f:
        inventory = json.load(f)

    objects = {o["fqn"]: o for o in inventory["objects"]}
    fqns = list(objects.keys())

    deps, rdeps = build_adj(inventory)
    cycles = detect_cycles(fqns, deps)
    waves = topological_waves(fqns, deps)
    wp = witness_paths(objects, deps)
    missing = missing_references(inventory, set(fqns))

    # ------------------------------------------------------------------
    # Optional: load column value constraints from SPG discovery output.
    # mssql-spg-load/scripts/discover_spg_constraints.py writes this file
    # when the SPG target is known.  When absent, the seeder uses type-based
    # generation only (standalone realization, no SPG dependency required).
    # ------------------------------------------------------------------
    col_val_constraints: dict = {}
    if args.constraints_file:
        with open(args.constraints_file) as f:
            cdata = json.load(f)
        col_val_constraints = cdata.get("constraints", {})
        total_rules = sum(len(v) for v in col_val_constraints.values())
        print(f"  Column value constraints: {total_rules} rule(s) across "
              f"{len(col_val_constraints)} table(s) "
              f"(from {args.constraints_file})")

    result = {
        "deps": {k: sorted(v) for k, v in deps.items()},
        "rdeps": {k: sorted(v) for k, v in rdeps.items()},
        "waves": waves,
        "wave_count": len(waves),
        "cycles": cycles,
        "missing_references": missing,
        "row_producing_targets": [
            fqn for fqn, o in objects.items() if o.get("row_producing")
        ],
        "witness_paths": wp,
        # Populated when --constraints-file is supplied; {} otherwise.
        "column_value_constraints": col_val_constraints,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Dependency graph built:")
    print(f"  Deployment waves: {len(waves)}")
    for i, wave in enumerate(waves):
        print(f"    Wave {i + 1}: {len(wave)} objects")
    if cycles:
        print(f"  WARNING — Cycles detected ({len(cycles)}):")
        for c in cycles:
            print(f"    {' → '.join(c)}")
    if missing:
        print(f"  Missing references: {len(missing)}")
    print(
        f"  Row-producing targets: {len(result['row_producing_targets'])}"
    )
    print(f"Written to: {args.output}")


if __name__ == "__main__":
    main()
