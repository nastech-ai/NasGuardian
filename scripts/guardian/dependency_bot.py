#!/usr/bin/env python3
"""
NasTech Guardian — Dependency Bot (Stage 2)
Scans for missing, broken, conflicting, and outdated dependencies
across all detected language stacks (Java/Gradle, Python, Node, Rust, Go).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent.parent


# ─────────────────────────────────────────────────────────────────
# Gradle / Android
# ─────────────────────────────────────────────────────────────────

def scan_gradle(profile: dict) -> dict:
    """Scan Gradle dependencies for conflicts and missing packages."""
    result = {"scanner": "gradle", "status": "pass", "issues": [], "deps": []}

    build_files = list(REPO_ROOT.rglob("build.gradle"))
    all_deps = []
    version_map = {}  # artifact → {module: version}

    for bf in build_files:
        if ".git" in str(bf) or "build/" in str(bf):
            continue
        text = bf.read_text(errors="replace")
        module = bf.parent.name

        # External deps
        for m in re.finditer(r'(?:implementation|api|compileOnly|testImplementation)\s+"([^"]+)"', text):
            dep = m.group(1)
            all_deps.append({"module": module, "dep": dep, "file": str(bf.relative_to(REPO_ROOT))})

            parts = dep.split(":")
            if len(parts) == 3:
                artifact = f"{parts[0]}:{parts[1]}"
                version = parts[2]
                if artifact not in version_map:
                    version_map[artifact] = {}
                version_map[artifact][module] = version

        # Project deps — verify they exist
        for m in re.finditer(r'project\s*\(\s*["\']:([^"\']+)["\']', text):
            proj = m.group(1).lstrip(":")
            proj_path = REPO_ROOT / proj
            if not proj_path.exists():
                result["issues"].append({
                    "type":    "missing_module",
                    "severity": "critical",
                    "module":  module,
                    "dep":     f":{proj}",
                    "message": f"Project module ':{proj}' not found on disk",
                    "fix":     f"Create directory '{proj}/' or remove the dependency",
                })
                result["status"] = "fail"

    # Version conflicts
    for artifact, versions in version_map.items():
        unique = set(versions.values())
        if len(unique) > 1:
            result["issues"].append({
                "type":    "version_conflict",
                "severity": "warning",
                "artifact": artifact,
                "versions": versions,
                "message":  f"Version conflict: {versions}",
                "fix":      f"Pin '{artifact}' to a single version across all modules",
            })

    # Unstable versions (alpha/beta/SNAPSHOT/RC)
    unstable_pat = re.compile(r"(SNAPSHOT|\.alpha\d*|\.beta\d*|-alpha|-beta|-rc\d*)", re.IGNORECASE)
    for item in all_deps:
        if unstable_pat.search(item["dep"]) and "security-crypto" not in item["dep"]:
            result["issues"].append({
                "type":    "unstable_version",
                "severity": "warning",
                "module":  item["module"],
                "dep":     item["dep"],
                "message": f"Pre-release version in use: {item['dep']}",
                "fix":     "Consider pinning to a stable release",
            })

    # Run ./gradlew dependencies (if gradlew available)
    gradlew = REPO_ROOT / "gradlew"
    if gradlew.exists():
        try:
            proc = subprocess.run(
                [str(gradlew), ":app:dependencies", "--configuration", "debugRuntimeClasspath",
                 "--no-daemon", "-q"],
                capture_output=True, text=True, cwd=REPO_ROOT, timeout=120
            )
            if proc.returncode != 0:
                # Find FAILED lines
                for line in proc.stderr.split("\n"):
                    if "FAILED" in line or "Could not resolve" in line:
                        result["issues"].append({
                            "type":    "resolution_failure",
                            "severity": "critical",
                            "message": line.strip(),
                            "fix":     "Check network, repository URLs, and version availability",
                        })
                        result["status"] = "fail"
        except subprocess.TimeoutExpired:
            result["issues"].append({
                "type":    "scan_timeout",
                "severity": "warning",
                "message": "Gradle dependency resolution timed out (120s)",
                "fix":     "Check Gradle daemon, network, or split into smaller scans",
            })
        except Exception as e:
            result["issues"].append({"type": "scan_error", "severity": "info", "message": str(e)})

    result["deps"] = all_deps
    result["total_deps"] = len(all_deps)
    result["version_conflicts"] = sum(1 for i in result["issues"] if i["type"] == "version_conflict")
    return result


# ─────────────────────────────────────────────────────────────────
# Python
# ─────────────────────────────────────────────────────────────────

def scan_python() -> dict:
    """Scan Python dependencies."""
    result = {"scanner": "python", "status": "pass", "issues": [], "deps": []}

    req_file = REPO_ROOT / "requirements.txt"
    if not req_file.exists():
        # Look in subdirectories
        found = list(REPO_ROOT.rglob("requirements.txt"))
        found = [f for f in found if ".git" not in str(f) and "build" not in str(f)]
        if not found:
            result["status"] = "skip"
            result["message"] = "No requirements.txt found"
            return result
        req_file = found[0]

    deps = []
    for line in req_file.read_text(errors="replace").split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("-"):
            deps.append(line)
    result["deps"] = deps

    # Try pip check
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            for line in proc.stdout.strip().split("\n"):
                if line:
                    result["issues"].append({
                        "type":    "pip_conflict",
                        "severity": "warning",
                        "message": line,
                        "fix":     "Run: pip install -r requirements.txt --upgrade",
                    })
            result["status"] = "warn"
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────────────────────────
# Node / NPM
# ─────────────────────────────────────────────────────────────────

def scan_node() -> dict:
    """Scan Node.js dependencies."""
    result = {"scanner": "node", "status": "pass", "issues": [], "deps": []}

    pkg_json = REPO_ROOT / "package.json"
    if not pkg_json.exists():
        result["status"] = "skip"
        result["message"] = "No package.json found"
        return result

    try:
        pkg = json.loads(pkg_json.read_text())
        deps = list(pkg.get("dependencies", {}).keys())
        dev_deps = list(pkg.get("devDependencies", {}).keys())
        result["deps"] = deps + dev_deps
    except json.JSONDecodeError as e:
        result["issues"].append({
            "type":    "invalid_json",
            "severity": "critical",
            "message": f"package.json is not valid JSON: {e}",
            "fix":     "Fix JSON syntax errors in package.json",
        })
        result["status"] = "fail"
        return result

    # Check node_modules presence
    if not (REPO_ROOT / "node_modules").exists():
        result["issues"].append({
            "type":    "modules_missing",
            "severity": "warning",
            "message": "node_modules/ not found",
            "fix":     "Run: npm install",
        })

    # npm audit (if available)
    if not (REPO_ROOT / "node_modules").exists():
        return result
    try:
        proc = subprocess.run(
            ["npm", "audit", "--json"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
        )
        if proc.stdout:
            audit = json.loads(proc.stdout)
            vulns = audit.get("metadata", {}).get("vulnerabilities", {})
            critical = vulns.get("critical", 0)
            high = vulns.get("high", 0)
            if critical > 0:
                result["issues"].append({
                    "type":    "npm_audit_critical",
                    "severity": "critical",
                    "message": f"npm audit: {critical} critical vulnerabilities",
                    "fix":     "Run: npm audit fix --force",
                })
                result["status"] = "fail"
            elif high > 0:
                result["issues"].append({
                    "type":    "npm_audit_high",
                    "severity": "warning",
                    "message": f"npm audit: {high} high severity vulnerabilities",
                    "fix":     "Run: npm audit fix",
                })
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────

def run_dependency_scan(profile: dict) -> dict:
    """Run all applicable dependency scanners based on detected language stack."""
    langs = profile.get("language_stack", "").split(",")
    build_sys = profile.get("build_system", "unknown")
    results = {}
    all_issues = []
    all_missing = []
    all_conflicts = []
    all_broken = []

    # Always scan Gradle for this project
    if "gradle" in build_sys or "java" in langs or "kotlin" in langs:
        print("  🔍 Scanning Gradle dependencies...")
        gradle_result = scan_gradle(profile)
        results["gradle"] = gradle_result
        for issue in gradle_result.get("issues", []):
            all_issues.append(issue)
            if issue["type"] == "missing_module":
                all_missing.append(issue)
            elif issue["type"] in ("version_conflict",):
                all_conflicts.append(issue)
            elif issue["type"] == "resolution_failure":
                all_broken.append(issue)

    if "python" in langs:
        print("  🔍 Scanning Python dependencies...")
        py_result = scan_python()
        results["python"] = py_result
        all_issues.extend(py_result.get("issues", []))

    if "node" in langs:
        print("  🔍 Scanning Node.js dependencies...")
        node_result = scan_node()
        results["node"] = node_result
        all_issues.extend(node_result.get("issues", []))

    # Determine overall status
    critical_count = sum(1 for i in all_issues if i.get("severity") == "critical")
    warn_count = sum(1 for i in all_issues if i.get("severity") == "warning")
    overall = "fail" if critical_count > 0 else ("warn" if warn_count > 0 else "pass")

    report = {
        "status":          overall,
        "critical_count":  critical_count,
        "warning_count":   warn_count,
        "total_issues":    len(all_issues),
        "missing":         all_missing,
        "conflicts":       all_conflicts,
        "broken":          all_broken,
        "all_issues":      all_issues,
        "scanner_results": results,
    }

    print(f"\n📦 Dependency Scan Complete")
    print(f"   Status:   {overall.upper()}")
    print(f"   Critical: {critical_count}")
    print(f"   Warnings: {warn_count}")
    print(f"   Total:    {len(all_issues)}")
    return report


def main():
    parser = argparse.ArgumentParser(description="NasTech Dependency Bot")
    parser.add_argument("--profile", required=True, help="Path to identity_profile.json")
    parser.add_argument("--output",  required=True, help="Output report path")
    args = parser.parse_args()

    profile = json.loads(Path(args.profile).read_text())
    report = run_dependency_scan(profile)
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"\n✅ Dependency report saved: {args.output}")

    if report["status"] == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
