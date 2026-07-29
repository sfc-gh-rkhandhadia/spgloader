#!/usr/bin/env python3
"""CLI wrapper for spgloader.conversion.dep_graph — builds topological sort."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from spgloader.conversion.dep_graph import build_dep_graph_result


def main():
    parser = argparse.ArgumentParser(
        description="Build topological dependency graph from ddl_objects.json"
    )
    parser.add_argument("--input", required=True, help="Path to ddl_objects.json")
    parser.add_argument("--output", required=True, help="Output path for dep_graph.json")
    args = parser.parse_args()

    objects = json.loads(Path(args.input).read_text())
    if isinstance(objects, dict):
        objects = objects.get("objects", [])
    result = build_dep_graph_result(objects)
    Path(args.output).write_text(json.dumps(result, indent=2))

    print(f"Dependency graph written to {args.output}")
    print(f"  {result['sorted']} objects sorted in deployment order")
    if result["cycles"]:
        print(f"  WARNING: {len(result['cycles'])} circular dependencies:")
        for fqn in result["cycles"]:
            print(f"    - {fqn} (appended at end)")


if __name__ == "__main__":
    main()
