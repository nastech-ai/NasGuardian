#!/usr/bin/env python3
"""
NasTech — Dependency Graph Analyzer
Parses Gradle build files and detects circular dependencies between modules.
Writes a Markdown report to --report (default: circular_deps_report.md).
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict, deque

log = logging.getLogger(__name__)

GRADLE_FILES = [
    "app/build.gradle",
    "terminal-view/build.gradle",
    "termux-shared/build.gradle",
    "terminal-emulator/build.gradle",
]

MODULE_NAMES = {
    ":app":               "app",
    ":terminal-view":     "terminal-view",
    ":termux-shared":     "termux-shared",
    ":terminal-emulator": "terminal-emulator",
}


def parse_project_deps(gradle_file: str) -> list:
    """Extract project(":module") dependencies from a Gradle file."""
    deps = []
    if not os.path.isfile(gradle_file):
        return deps
    with open(gradle_file) as f:
        for line in f:
            m = re.search(r'project\("(:[\w\-]+)"\)', line)
            if m and "//" not in line.split("project")[0]:
                deps.append(m.group(1))
    return deps


def build_graph(root: str = ".") -> dict:
    """Build an adjacency list {module: [dep_module]} from all gradle files."""
    graph = defaultdict(list)
    for gf in GRADLE_FILES:
        path = os.path.join(root, gf)
        module = ":" + gf.split("/")[0]
        deps = parse_project_deps(path)
        for d in deps:
            if d not in graph[module]:
                graph[module].append(d)
    return dict(graph)


def detect_cycles(graph: dict) -> list:
    """Detect cycles using DFS. Returns list of cycle paths."""
    visited = set()
    rec_stack = set()
    cycles = []

    def dfs(node, path):
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                dfs(neighbor, path + [neighbor])
            elif neighbor in rec_stack:
                # Found a cycle
                cycle_start = path.index(neighbor) if neighbor in path else 0
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)
        rec_stack.discard(node)

    for node in list(graph.keys()):
        if node not in visited:
            dfs(node, [node])

    # Deduplicate
    seen = set()
    unique = []
    for c in cycles:
        key = frozenset(c)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def generate_report(graph: dict, cycles: list) -> str:
    lines = [
        "# NasTech Dependency Graph Report",
        "",
        "## Module Dependency Graph",
        "",
    ]

    if graph:
        for module, deps in sorted(graph.items()):
            arrow = " → ".join([module] + deps) if deps else f"{module} (no project deps)"
            lines.append(f"- `{arrow}`")
    else:
        lines.append("No inter-module project dependencies found.")

    lines += ["", "## Circular Dependency Detection", ""]

    if cycles:
        lines.append(f"⚠️  **{len(cycles)} circular dependency chain(s) detected:**")
        lines.append("")
        for i, cycle in enumerate(cycles, 1):
            lines.append(f"  {i}. `{' → '.join(cycle)}`")
    else:
        lines.append("✅ No circular dependencies detected.")

    lines += [
        "",
        "## Dependency Statistics",
        "",
        f"- Modules analyzed: {len(GRADLE_FILES)}",
        f"- Modules with dependencies: {sum(1 for v in graph.values() if v)}",
        f"- Total inter-module edges: {sum(len(v) for v in graph.values())}",
        f"- Circular chains: {len(cycles)}",
    ]

    return "\n".join(lines)


def main(args=None):
    parser = argparse.ArgumentParser(description="NasTech Dependency Graph Analyzer")
    parser.add_argument("--root",   default=".",
                        help="Project root directory")
    parser.add_argument("--report", default="circular_deps_report.md",
                        help="Output Markdown report path")
    parser.add_argument("--json",   dest="json_out", action="store_true",
                        help="Also write JSON output")
    opts = parser.parse_args(args)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s:%(name)s:%(message)s")

    graph  = build_graph(opts.root)
    cycles = detect_cycles(graph)
    report = generate_report(graph, cycles)

    with open(opts.report, "w") as f:
        f.write(report)
    log.info("[dependency_graph] report written to %s  cycles=%d",
             opts.report, len(cycles))

    print(report)

    if opts.json_out:
        data = {"graph": graph, "cycles": cycles, "cycle_count": len(cycles)}
        json_path = opts.report.replace(".md", ".json")
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2)

    return 1 if cycles else 0


if __name__ == "__main__":
    sys.exit(main())
